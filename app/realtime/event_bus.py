"""Realtime event bus — one class, one process, PostgreSQL LISTEN/NOTIFY.

Slice §13/§23: presence, lifecycle and revoke events must reach every
worker holding a connection of the class, without Redis and without a
second source of truth. The bus reuses the database that already owns
the state:

- :meth:`RealtimeEventBus.publish` executes ``SELECT pg_notify(...)`` on
  the CALLER's session, inside the caller's transaction. The
  notification fires **on commit** and is silently discarded on rollback,
  so an event can never describe state that was not persisted — no
  out-of-band queue, no dual-write. On SQLite (unit-test scratch
  databases) it falls through to the same local dispatch, which is a
  no-op while nothing is registered.
- A single listener thread per process connects with psycopg3 in
  autocommit mode, ``LISTEN realtime_events``, and yields notifications
  from ``Connection.notifies(timeout=...)`` (re-checking a stop event
  every second). It is started LAZILY on the first WebSocket
  registration (:meth:`ensure_listening`) — plain HTTP workers and unit
  tests never open the extra connection.
- Dispatch is local-only: events are never re-published by the listener
  (no double delivery). Each target connection gets
  ``handle_event(payload)`` scheduled on the bound event loop via
  ``asyncio.run_coroutine_threadsafe``; the endpoint decides what each
  event means (send, close, cleanup).

Payloads are small JSON envelopes — ``{"type", "class_id", ...}`` — that
carry only presentation-safe data (ids as strings, display names,
reason-free lifecycle facts) and never authoritative state: the database
remains the only truth, events are mere wake-ups (§28: no internal event
names ever reach a client; the endpoint translates).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.realtime.connections import registry

logger = logging.getLogger(__name__)

#: NOTIFY channel — one channel for the whole realtime feature.
CHANNEL = "realtime_events"
#: Hard cap: NOTIFY payloads beyond ~8000 bytes are rejected by
#: PostgreSQL; events are wake-ups and must stay tiny anyway (§28).
MAX_PAYLOAD_BYTES = 8000
#: How long the listener waits per batch before re-checking its stop flag.
_LISTEN_TICK_SECONDS = 1.0


class RealtimeEventBus:
    """Publish-on-commit events + one lazy LISTEN thread per process."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._start_lock = threading.Lock()

    # --- loop binding -----------------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop of the (first) live WebSocket.

        Called on every connection registration: the latest binding wins,
        which is exactly right for tests that rebuild their client loop
        between cases.
        """
        self._loop = loop

    # --- publishing -------------------------------------------------------------------

    def publish(self, session: Session, event: dict) -> None:
        """Queue one event on the caller's transaction (committed by the caller).

        ``event`` must carry a ``"type"`` and a ``"class_id"`` (string or
        ``None`` for offering-scoped events). Nothing here commits or
        flushes — atomicity with the state change that caused the event
        is the whole point.
        """
        if "type" not in event:
            raise ValueError("realtime event requires a 'type'")
        event.setdefault("class_id", None)
        payload = json.dumps(event, separators=(",", ":"))
        if len(payload.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("realtime event payload exceeds 8000 bytes")

        bind = session.get_bind()
        dialect = getattr(bind, "dialect", None)
        dialect_name = dialect.name if dialect is not None else ""
        if dialect_name.startswith("postgres"):
            # Fires with the caller's COMMIT; rolled back with it (§13).
            from sqlalchemy import text

            session.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": CHANNEL, "payload": payload},
            )
            return
        # SQLite / unit tests: same delivery path, nothing registered.
        self._dispatch_local(json.loads(payload))

    # --- lazy listener ----------------------------------------------------------------

    def ensure_listening(self) -> None:
        """Start the LISTEN thread once (no-op outside PostgreSQL).

        Called from the WebSocket handshake via ``asyncio.to_thread``:
        TestClient never runs lifespan hooks, so startup is tied to the
        first real connection instead. Blocks until the thread is ready
        to deliver, so the first publish after registration cannot race
        a missing listener.
        """
        settings = get_settings()
        if not settings.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                self._ready.wait(timeout=5.0)
                return
            self._stop.clear()
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._listen_loop,
                name="realtime-events-listener",
                daemon=True,
            )
            self._thread.start()
        self._ready.wait(timeout=5.0)

    def _dsn(self) -> str:
        return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    def _listen_loop(self) -> None:
        """Connect, LISTEN, yield notifications — forever, reconnecting.

        The connection is kept OPEN across ``notifies()`` batches: only an
        actual error tears it down. Reconnecting on every tick would open
        a window in which PostgreSQL has no listening backend, and a NOTIFY
        fired there is dropped forever — an event that never reaches a
        socket is indistinguishable from an event that never happened.
        """
        import psycopg

        while not self._stop.is_set():
            try:
                with psycopg.connect(self._dsn(), autocommit=True) as connection:
                    connection.execute(f"LISTEN {CHANNEL}")
                    logger.debug("realtime listener connected on %s", CHANNEL)
                    self._ready.set()
                    while not self._stop.is_set():
                        # One batch at a time on the SAME connection: the
                        # timeout only re-checks our stop flag.
                        for notify in connection.notifies(
                            timeout=_LISTEN_TICK_SECONDS
                        ):
                            if self._stop.is_set():
                                break
                            try:
                                payload = json.loads(notify.payload)
                            except (TypeError, ValueError):
                                logger.warning("dropping malformed realtime payload")
                                continue
                            self._dispatch_local(payload)
            except Exception:  # noqa: BLE001 — the listener must survive DB hiccups
                if self._stop.is_set():
                    break
                logger.exception("realtime listener failed; retrying in 1s")
                self._ready.clear()
                self._stop.wait(timeout=1.0)
        self._ready.set()

    # --- local delivery ---------------------------------------------------------------

    def _dispatch_local(self, payload: dict) -> None:
        """Hand one event to every local target connection (never re-publish)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            targets = self._targets(payload)
        except (TypeError, ValueError):
            logger.warning("dropping realtime event with bad routing fields")
            return
        except_ref = payload.get("except_ref")
        for connection in targets:
            if (
                except_ref is not None
                and connection.participant_ref == except_ref
            ):
                continue
            if connection.handle_event is None:
                continue
            try:
                future = asyncio.run_coroutine_threadsafe(
                    connection.handle_event(payload), loop
                )
            except RuntimeError:
                # Loop went away between the check and the schedule.
                logger.debug("realtime dispatch dropped: event loop closed")
                continue
            future.add_done_callback(_log_dispatch_failure)

    @staticmethod
    def _targets(payload: dict) -> list:
        """Resolve routing: class-scoped, or offering+student for a revoke."""
        event_type = payload.get("type")
        if event_type == "connection.revoke":
            offering = payload.get("teaching_offering_id")
            student = payload.get("student_id")
            if not offering or not student:
                return []
            return registry.find_for_revoke(uuid.UUID(offering), uuid.UUID(student))
        class_id = payload.get("class_id")
        if not class_id:
            return []
        return registry.connections_for_class(uuid.UUID(class_id))


def _log_dispatch_failure(future: "asyncio.Future") -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.error("realtime event dispatch failed: %s", exc)


#: One bus per process (matching one event loop and one listener thread).
_bus = RealtimeEventBus()


def get_event_bus() -> RealtimeEventBus:
    """The process-wide realtime bus."""
    return _bus
