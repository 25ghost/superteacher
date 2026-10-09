"""Live WebSocket connections of one process — registry and identities.

One process holds at most ONE live connection per participant per class
(slice §15): a second handshake for the same (class, user) is refused
with ``POLICY_VIOLATION`` while the first is still registered. The
registry is in-process state — cross-worker duplicates fall back to the
attendance segment's unique open index, which the service maps to the
same refusal.

Everything here is transport-mechanics only: who is connected, fast
lookups for fan-out, and a send lock so two tasks never interleave a
frame. Business rules (roles, enrollment, class status) never live in
this module — the WebSocket endpoint validates those through
``classroom_service`` before anything is registered.

``participant_ref`` is the opaque, deterministic participant identity
used inside presence events: SHA-256 of ``"{class_id}:{user_id}"``, first
32 hex characters. It is computable from the database snapshot (another
worker's student rows) but reveals nothing about the user id — presence
events never carry raw ids.

Behavior hooks (``handle_event``, ``cleanup``) are plain attributes the
WS endpoint installs on registration: the event bus only ever schedules
``handle_event(payload)`` on the connection's loop, and cleanup stays in
the endpoint module where the database and the bus are available. This
keeps ``app.realtime`` free of service imports (no cycles).
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: Delivered presence/lifecycle payloads after this event's routing keys
#: are stripped by the endpoint (§19/§25 shapes).
EventHandler = Callable[[dict], Awaitable[None]]
CleanupHandler = Callable[..., Awaitable[None]]


def participant_ref(class_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Opaque participant identity for presence events (deterministic, 32 hex)."""
    return hashlib.sha256(f"{class_id}:{user_id}".encode()).hexdigest()[:32]


@dataclass
class LiveConnection:
    """One accepted WebSocket bound to one authorized participant."""

    websocket: Any
    class_id: uuid.UUID
    user_id: uuid.UUID
    role: str  # "teacher" | "student"
    display_name: str
    participant_ref: str
    connection_id: str  # segment connection id (students) / registry key
    teaching_offering_id: uuid.UUID
    student_id: uuid.UUID | None
    #: Behavior hooks installed by the WS endpoint on registration.
    handle_event: EventHandler | None = None
    cleanup: CleanupHandler | None = None
    registered_at: float = field(default_factory=time.monotonic)
    #: Monotonic timestamp of the last received client frame (§32).
    last_activity: float = field(default_factory=time.monotonic)
    cleaned: bool = False
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def send_json(self, payload: dict) -> bool:
        """Send one frame; False when the socket is already gone."""
        if self.cleaned and self.websocket is None:  # pragma: no cover - defensive
            return False
        try:
            async with self._send_lock:
                await self.websocket.send_json(payload)
            return True
        except Exception:  # noqa: BLE001 — a dead socket must never break fan-out
            return False

    @property
    def is_student(self) -> bool:
        return self.student_id is not None


class ConnectionRegistry:
    """Thread-safe index of live connections inside this process.

    Two indexes, one lock: per class (registration order — the snapshot
    order) and per participant (the one-connection rule). Registration is
    a single atomic check-then-insert so two racing handshakes for the
    same participant cannot both win.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_class: dict[uuid.UUID, dict[str, LiveConnection]] = {}
        self._by_participant: dict[tuple[uuid.UUID, uuid.UUID], str] = {}

    def try_register(self, connection: LiveConnection) -> bool:
        """Atomically register; False when this participant is already live."""
        key = (connection.class_id, connection.user_id)
        with self._lock:
            if key in self._by_participant:
                return False
            self._by_participant[key] = connection.connection_id
            self._by_class.setdefault(connection.class_id, {})[
                connection.connection_id
            ] = connection
        return True

    def unregister(self, connection: LiveConnection) -> None:
        """Remove one connection (idempotent — cleanup runs twice by design)."""
        key = (connection.class_id, connection.user_id)
        with self._lock:
            connections = self._by_class.get(connection.class_id)
            if connections is not None:
                connections.pop(connection.connection_id, None)
                if not connections:
                    self._by_class.pop(connection.class_id, None)
            if self._by_participant.get(key) == connection.connection_id:
                self._by_participant.pop(key, None)

    def connections_for_class(self, class_id: uuid.UUID) -> list[LiveConnection]:
        """Live connections of one class, registration order (deterministic)."""
        with self._lock:
            return list(self._by_class.get(class_id, {}).values())

    def find_for_revoke(
        self, teaching_offering_id: uuid.UUID, student_id: uuid.UUID
    ) -> list[LiveConnection]:
        """Every live student connection of that offering for that student.

        A student leaves the enrollment (not one class), so the revoke
        must close every class connection they hold of that offering.
        """
        with self._lock:
            return [
                connection
                for connections in self._by_class.values()
                for connection in connections.values()
                if connection.student_id == student_id
                and connection.teaching_offering_id == teaching_offering_id
            ]

    def __len__(self) -> int:
        with self._lock:
            return sum(len(connections) for connections in self._by_class.values())


#: The process-wide registry (one per worker, matching one event loop).
registry = ConnectionRegistry()
