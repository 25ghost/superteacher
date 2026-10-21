"""Automatic book extraction — the background sweep (P2).

The retry endpoint only flips a failed revision back to ``uploaded``;
somebody still has to DO the extraction. Like the class-end sweeper
before it, this module is the smallest mechanism the application already
has — a daemon thread on the application's own lifecycle (no Celery, no
Redis, no external scheduler):

- :meth:`BookExtractionSweeper.start` is called from the FastAPI lifespan
  and begins with an IMMEDIATE pass, so revisions stranded in ``uploaded``
  by a crash or a redeploy are extracted at startup without waiting for a
  trigger;
- the loop re-sweeps every ``BOOK_EXTRACT_SWEEP_INTERVAL_SECONDS`` (target:
  at most five seconds between passes; re-read from settings each pass so
  tests can shrink it live, exactly like the class-end sweeper);
- every pass opens ONE fresh, short-lived session, extracts a bounded
  batch (:func:`app.services.book_extraction_service.extract_due_revisions`,
  ``BOOK_EXTRACT_SWEEP_BATCH_LIMIT`` — each revision commits inside the
  service) and CLOSES that session. A database session is never held open
  across the sleep between passes;
- the pass sleeps on the stop event, and shutdown (:meth:`stop`, called
  from the lifespan's shutdown) wakes it immediately and joins the
  thread, so no background task or session outlives the application.

Everything domain-shaped lives in ``book_extraction_service``: the
sweeper contains no extraction rules of its own — same row-lock claim,
same status transitions, same unit write, same audit rows as a manual
retry triggers. That is also what makes it multi-worker safe: each
worker's ``SELECT ... FOR UPDATE`` serialises on the revision row and the
loser re-reads the committed status, so several workers sweeping the
same uploaded revision still produce ONE extraction, ONE tree and ONE
audit pair. No process-local lock is used or needed — the guarantee is
PostgreSQL's row lock, not this process's memory.

Integration tests run with the lifespan never entered (module-scoped
``TestClient`` without ``with``), so the background thread does not start
there — tests call :meth:`sweep_once` directly, and an autouse fixture
keeps the thread idle if a unit test does start the app.
"""
from __future__ import annotations

import logging
import threading

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services import book_extraction_service

logger = logging.getLogger(__name__)

#: Floor for the sleep between passes (settings are re-read every loop,
#: so a zero/negative test value cannot spin the thread).
_MIN_INTERVAL_SECONDS = 0.05


class BookExtractionSweeper:
    """One daemon thread per process that extracts uploaded revisions."""

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
        inside the new thread, which is what recovers already-uploaded
        revisions after an application restart — no request required.
        """
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="book-extraction-sweeper",
                daemon=True,
            )
            self._thread.start()
        logger.debug("book-extraction sweeper started")

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the thread and join it (idempotent; called on shutdown)."""
        with self._start_lock:
            thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover — a pass must be short
                logger.warning(
                    "book-extraction sweeper did not stop within %.1fs", timeout
                )
            else:
                logger.debug("book-extraction sweeper stopped")
        # The stop flag stays set; ``start`` clears it for the next life.

    def sweep_once(self) -> int:
        """Run ONE batch in a fresh, short-lived session; return how many.

        Public so tests (and operators) can trigger the exact code path
        the background thread runs, deterministically. Propagates failures
        to the caller — the background loop catches them so a database
        hiccup delays the next sweep instead of killing the thread.
        """
        settings = get_settings()
        session = SessionLocal()
        try:
            extracted = book_extraction_service.extract_due_revisions(
                session,
                limit=max(1, int(settings.BOOK_EXTRACT_SWEEP_BATCH_LIMIT)),
            )
            session.commit()  # no-op if every revision already committed
        except Exception:
            session.rollback()  # releases any row lock still held
            raise
        finally:
            session.close()
        if extracted:
            logger.info("book-extraction sweep extracted %d revision(s)", extracted)
        return extracted

    def _run(self) -> None:
        """Sweep immediately, then once per interval until stopped."""
        while not self._stop.is_set():
            try:
                self.sweep_once()
            except Exception:  # noqa: BLE001 — one bad pass must not kill the loop
                logger.exception(
                    "book-extraction sweep failed; retrying next interval"
                )
            settings = get_settings()
            interval = max(
                _MIN_INTERVAL_SECONDS,
                float(settings.BOOK_EXTRACT_SWEEP_INTERVAL_SECONDS),
            )
            self._stop.wait(timeout=interval)


#: The process-wide sweeper (one per worker, matching one application).
book_extraction_sweeper = BookExtractionSweeper()
