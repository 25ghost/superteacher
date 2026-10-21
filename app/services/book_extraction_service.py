"""Book content extraction orchestration — claim, parse, persist, retry (P2).

The parser (:mod:`app.services.book_extraction`) is a pure function from
PDF bytes to an in-memory unit tree; this module is the durable half of
the pipeline that turns a freshly uploaded revision (``uploaded``) into a
reviewable one (``pending_review``) or an honestly failed one
(``extraction_failed``):

- **Claiming is a row lock, not a queue table.** Each pass takes the
  oldest ``uploaded`` revision with ``SELECT ... FOR UPDATE`` (see
  ``book_repository.claim_next_uploaded_revision``): two workers sweeping
  the same batch serialize on that row, the loser re-reads the committed
  state and skips. A crash mid-extraction rolls back, releases the lock
  and leaves the revision ``uploaded`` — recovered by the next sweep with
  no lease timeout to tune, because the lease IS the transaction.
- **One short-lived transaction per revision.** Within it: re-check the
  status under the lock, flush ``extracting`` + the ``started`` audit
  event, read the stored PDF bytes through the ``StorageBackend``, parse,
  delete-then-rewrite the unit tree, move to ``pending_review`` with the
  extraction summary (or to ``extraction_failed`` with the fixed-code
  error), write the matching audit event, and commit. The intermediate
  ``extracting`` state is flushed but never committed on its own, so an
  observer only ever sees ``uploaded`` (nothing ran / rolled back),
  ``pending_review`` (done) or ``extraction_failed`` (done, badly).
- **Nothing from the document ever reaches the logs or the audit row.**
  Only counts, fixed codes and identifiers: ``extraction_error`` stores
  the parser's ``code: message`` (message contains no document text), the
  audit payload stores ``reason`` = the code alone.
- **Retry is idempotent and admin-only.** ``retry_book_extraction`` flips
  a ``extraction_failed`` revision back to ``uploaded`` (the sweeper does
  the actual re-run) and audits who asked; anything else is 409, an
  unknown id 404, a non-admin 403. Re-running a pass deletes the units it
  wrote before rewriting them, so a double claim can never double a tree.

Unexpected exceptions (storage outage, database hiccup) deliberately
propagate without committing: the transaction rolls back to ``uploaded``
and the next sweep retries — only deterministic document problems
(``ExtractionError``: encrypted, unreadable, no extractable text) become
``extraction_failed``, because re-reading the same bytes cannot help.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy.orm import Session

from app.models.book_revision import BookRevision
from app.models.book_unit import BookUnit
from app.models.enums import BookRevisionStatus, UserRole
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import book_repository as book_repo
from app.schemas.book import BookRead
from app.services import book_service
from app.services.book_extraction import (
    ExtractionError,
    ExtractionResult,
    extract_book_pdf,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)
from app.services.storage import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

#: Audit ``event_type`` values (fixed, 32-char column).
EVENT_EXTRACT_STARTED = "book_extract_started"
EVENT_EXTRACT_COMPLETED = "book_extract_completed"
EVENT_EXTRACT_FAILED = "book_extract_failed"
EVENT_EXTRACT_RETRY = "book_extract_retry"


def extract_due_revisions(
    session: Session,
    *,
    limit: int,
    storage: StorageBackend | None = None,
) -> int:
    """Extract up to ``limit`` oldest ``uploaded`` revisions; return how many.

    Each revision gets its OWN transaction (committed inside
    :func:`_extract_one`); a batch therefore survives a failure halfway
    through — the already-committed revisions stay extracted and the
    failing one either lands in ``extraction_failed`` (document problem)
    or rolls back to ``uploaded`` (transient problem) for the next pass.
    On an unexpected exception the whole call propagates WITHOUT
    committing the current revision; the caller (the sweeper) rolls back,
    which is exactly what releases its row lock.
    """
    backend = storage or get_storage_backend()
    processed = 0
    while processed < max(0, int(limit)):
        revision = book_repo.claim_next_uploaded_revision(session)
        if revision is None:
            break
        _extract_one(session, revision, backend)
        processed += 1
    return processed


def _extract_one(
    session: Session,
    revision: BookRevision,
    backend: StorageBackend,
) -> None:
    """Run ONE extraction to completion; always ends with a commit.

    The status re-check under the lock is belt-and-braces: the claim
    already filtered ``uploaded`` inside this same transaction, but the
    invariant ("a revision not marked ``uploaded`` is never extracted")
    is asserted where the lock is actually held.
    """
    if revision.status != BookRevisionStatus.UPLOADED.value:
        session.commit()  # release the lock; another worker won the race
        return

    book_id = revision.book_id
    user_id = revision.uploaded_by_user_id

    revision.status = BookRevisionStatus.EXTRACTING.value
    revision.extraction_error = None
    session.flush()
    _log(
        session,
        user_id=user_id,
        event_type=EVENT_EXTRACT_STARTED,
        payload={"book_id": str(book_id), "book_revision_id": str(revision.id)},
    )
    session.flush()

    try:
        pdf_bytes = backend.read(revision.file_asset.storage_key)
        result = extract_book_pdf(pdf_bytes)
    except ExtractionError as exc:
        # Deterministic document problem: record it and commit so the
        # administrator sees an honest ``extraction_failed`` with the
        # fixed code, plus the audit trail of both phases.
        revision.status = BookRevisionStatus.EXTRACTION_FAILED.value
        revision.extraction_error = exc.stored()
        revision.extraction_summary = None
        _log(
            session,
            user_id=user_id,
            event_type=EVENT_EXTRACT_FAILED,
            payload={
                "book_id": str(book_id),
                "book_revision_id": str(revision.id),
                "reason": exc.code,
            },
        )
        session.commit()
        logger.info(
            "book extraction failed revision=%s reason=%s",
            revision.id,
            exc.code,
        )
        return

    _write_units(session, revision, result)
    revision.status = BookRevisionStatus.PENDING_REVIEW.value
    revision.extraction_summary = result.summary_json()
    revision.extraction_error = None
    _log(
        session,
        user_id=user_id,
        event_type=EVENT_EXTRACT_COMPLETED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision.id),
            "unit_count": result.unit_count,
            "page_count": result.page_count,
            "warnings": list(result.warnings),
        },
    )
    session.commit()
    logger.info(
        "book extraction completed revision=%s units=%d pages=%d",
        revision.id,
        result.unit_count,
        result.page_count,
    )


def _write_units(
    session: Session,
    revision: BookRevision,
    result: ExtractionResult,
) -> None:
    """Replace this revision's unit tree with the freshly parsed one.

    Delete-then-write keeps a re-run idempotent (retry, or a crash that
    somehow reached this point twice): the partial unique indexes on
    ``(revision, parent, display_order)`` would reject a second copy of
    the tree, so wiping first is also what lets the indexes stay honest.
    Rows are created in depth-first document order with pre-assigned
    primary keys, so every child's composite self-FK points at a parent
    row inserted by the same flush, in the same statement, before it.
    """
    book_repo.delete_revision_units(session, revision.id)
    session.flush()

    def _insert(units, parent_unit_id: uuid.UUID | None) -> None:
        for unit in units:
            row = BookUnit(
                id=uuid.uuid4(),
                book_revision_id=revision.id,
                parent_unit_id=parent_unit_id,
                kind=unit.kind,
                title=unit.title,
                content=unit.content,
                display_order=unit.display_order,
                source_page_start=unit.source_page_start,
                source_page_end=unit.source_page_end,
                extraction_warnings=unit.warnings_text,
                assessment_requirement=unit.assessment_requirement,
            )
            session.add(row)
            _insert(unit.children, row.id)

    _insert(result.units, None)
    session.flush()


def retry_book_extraction(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
) -> BookRead:
    """Requeue the newest revision of ``book_id`` after a failed extraction.

    The endpoint owns the commit (house pattern): everything here is
    flushed only, so a failed response can still be rolled back whole.
    Only ``extraction_failed`` may be retried — any other status is a
    409 (an in-flight or completed extraction is not the administrator's
    to restart), an unknown book a 404, a non-administrator a 403.
    """
    _require_admin(user)
    book = book_repo.get_book_by_id(session, book_id)
    if book is None:
        raise LearningNotFoundError(f"no book with id {book_id}")
    revision = book.revisions[0]  # relationship is newest-first
    if revision.status != BookRevisionStatus.EXTRACTION_FAILED.value:
        raise LearningConflictError(
            "only a failed extraction can be retried "
            f"(current status: {revision.status})"
        )
    revision.status = BookRevisionStatus.UPLOADED.value
    revision.extraction_error = None
    auth_event_repo.log_event(
        session,
        user_id=revision.uploaded_by_user_id,
        event_type=EVENT_EXTRACT_RETRY,
        metadata_json=json.dumps(
            {
                "book_id": str(book_id),
                "book_revision_id": str(revision.id),
                "revision_number": revision.revision_number,
            }
        ),
        actor_user_id=user.id,
    )
    session.flush()
    return book_service.get_book(session, user, book_id)


def _log(
    session: Session,
    *,
    user_id: uuid.UUID,
    event_type: str,
    payload: dict,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """One audit row about identifiers and fixed codes — never content."""
    auth_event_repo.log_event(
        session,
        user_id=user_id,
        event_type=event_type,
        metadata_json=json.dumps(payload),
        actor_user_id=actor_user_id,
    )


def _require_admin(user) -> None:
    """Service-level guard (the route guard is not the only guard)."""
    if user.role != UserRole.ADMIN.value:
        raise LearningForbiddenError(
            "administrator role required for this operation"
        )
