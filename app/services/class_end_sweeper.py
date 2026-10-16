"""Automatic scheduled class ending — the background sweep (slice 3E).

Phase 3A's :func:`online_class_service.ensure_not_overdue` ends an
overdue ``live`` class whenever anything *touches* it (a read, a ticket,
a heartbeat, a message send). That is not enough on its own: when the
scheduled end passes and no participant sends another frame, no teacher
presses End and no client makes another request, nothing would ever run
the transition. This module closes that gap with the smallest mechanism
the application already has — a daemon thread on the application's own
lifecycle (no Celery, no Redis, no external scheduler):

- :meth:`ClassEndSweeper.start` is called from the FastAPI lifespan and
  begins with an IMMEDIATE sweep, so classes left ``live`` by a crash or
  a redeploy are recovered at startup without waiting for a client;
- the loop then re-sweeps every ``CLASS_END_SWEEP_INTERVAL_SECONDS``
  (target: at most five seconds between passes; re-read from settings
  each pass so tests can shrink it live, like the WebSocket watchdog);
- every pass opens ONE fresh, short-lived session, ends a bounded batch
  (:func:`online_class_service.end_due_classes`, ``CLASS_END_SWEEP_BATCH_
  LIMIT``), commits and CLOSES that session. A database session is never
  held open across the sleep between passes;
- the pass sleeps on the stop event, and shutdown (:meth:`ClassEndSweeper.stop`,
  called from the lifespan's shutdown) wakes it immediately and joins the
  thread, so no background task or session outlives the application.

Everything domain-shaped is delegated to ``online_class_service``: the
sweeper contains no ending rules of its own — same state machine, same
``scheduled_end_at`` cutoff, same attendance finalization, same audit
row, same post-commit ``class.ended`` publication as the teacher's End
button. That is also what makes it multi-worker safe: each worker locks
the class row (``SELECT ... FOR UPDATE``) and re-checks the state, so
several workers sweeping the same overdue class still produce ONE
transition, ONE logical audit event and ONE authoritative ``class.ended``
notification. No process-local lock is used or needed — the guarantee is
PostgreSQL's row lock, not this process's memory.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services import online_class_service

logger = logging.getLogger(__name__)

#: Floor for the sleep between passes (settings are re-read every loop,
#: so a zero/negative test value cannot spin the thread).
_MIN_INTERVAL_SECONDS = 0.05


class ClassEndSweeper:
    """One daemon thread per process that ends overdue live classes."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._start_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Is the sweep thread alive right now?"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the sweep thread once (idempotent).

        Called from the FastAPI lifespan: the first pass runs immediately
        inside the new thread, which is what recovers already-overdue
        classes after an application restart — no client request required.
        """
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="class-end-sweeper",
                daemon=True,
            )
            self._thread.start()
        logger.debug("class-end sweeper started")

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the thread and join it (idempotent; called on shutdown)."""
        with self._start_lock:
            thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover — a sweep pass must be short
                logger.warning("class-end sweeper did not stop within %.1fs", timeout)
            else:
                logger.debug("class-end sweeper stopped")
        # The stop flag stays set; ``start`` clears it for the next life.

    def sweep_once(self, *, now: datetime | None = None) -> int:
        """Run ONE batch in a fresh, short-lived session; return classes ended.

        Public so tests (and operators) can trigger the exact code path
        the background thread runs, deterministically. Propagates failures
        to the caller — the background loop catches them so a database
        hiccup delays the next sweep instead of killing the thread.
        """
        settings = get_settings()
        session = SessionLocal()
        try:
            ended = online_class_service.end_due_classes(
                session,
                now=now,
                limit=max(1, int(settings.CLASS_END_SWEEP_BATCH_LIMIT)),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        if ended:
            logger.info("class-end sweep finished %d overdue class(es)", ended)
        return ended

    def _run(self) -> None:
        """Sweep immediately, then once per interval until stopped."""
        while not self._stop.is_set():
            try:
                self.sweep_once()
            except Exception:  # noqa: BLE001 — one bad pass must not kill the loop
                logger.exception("class-end sweep failed; retrying next interval")
            settings = get_settings()
            interval = max(
                _MIN_INTERVAL_SECONDS, float(settings.CLASS_END_SWEEP_INTERVAL_SECONDS)
            )
            self._stop.wait(timeout=interval)


#: The process-wide sweeper (one per worker, matching one application).
class_end_sweeper = ClassEndSweeper()
