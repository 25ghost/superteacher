"""Administrative objective-question authoring (book P4, ADR-006).

One file owns every question-scoped decision so the transaction and
audit rules stay uniform:

- **Listing** (`list_questions`) exposes every question of ONE
  exercise-set unit with its options and answer-key state (admin only).
- **Creation** (`create_question`) authors ONE multiple-choice or
  true-false question on ONE exercise-set unit of a ``pending_review``
  revision. Options are validated completely (count per kind, distinct
  non-blank texts, unique sibling orders) BEFORE anything is written;
  correctness is NOT part of creation — it stays unverified until an
  explicit verification names the correct option.
- **Correction** (`correct_question`) edits the domain fields of ONE
  question while — and only while — the revision is ``pending_review``.
  Supplying ``options`` REPLACES the whole option set (fresh rows);
  ANY correction clears the question's answer key — verification state
  and option flags alike — so a corrected question looks freshly
  authored until it is verified again. Everything is validated before
  any row is written; a refused correction changes nothing.
- **Removal** (`delete_question`) deletes ONE question and its options;
  a published or rejected revision refuses (409) so assessment history
  is never rewritten.
- **Answer-key verification** (`verify_question`) is the explicit,
  recorded action that marks ONE option of ONE question correct —
  correctness is never inferred from text, order or creation.
- **Threshold configuration** (`set_unit_threshold`) stores ONE
  exercise set's passing threshold (1-100; NULL restores the default
  70). Any change resets the unit's review status to ``pending``.

Every question mutation on an approved exercise set also resets that
unit's review status: approval of old assessment content never carries
over to changed content (the same rule P3 applies to unit corrections).

House rules, everywhere: administrator-only (service-level guard, not
just the route guard), the endpoint owns the commit (this module only
flushes), unknown and foreign ids answer the SAME 404, invalid
lifecycle transitions are 409, invalid content 422, and audit payloads
carry identifiers, counts and fixed codes only — never question text,
option text or filesystem paths. Event names fit the 32-char
``auth_events.event_type`` column.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.book import Book
from app.models.book_question import BookQuestion
from app.models.book_question_option import BookQuestionOption
from app.models.book_revision import BookRevision
from app.models.book_unit import BookUnit
from app.models.enums import (
    BookQuestionType,
    BookRevisionStatus,
    BookUnitKind,
    BookUnitReviewStatus,
    UserRole,
)
from app.repositories import auth_event_repository as auth_event_repo
from app.schemas.book import BookUnitRead
from app.schemas.book_assessment import (
    BookQuestionCorrectRequest,
    BookQuestionCreate,
    BookQuestionOptionCreate,
    BookQuestionOptionRead,
    BookQuestionRead,
    BookQuestionVerifyRequest,
    BookUnitThresholdRequest,
)
from app.services.book_review_service import read_unit
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)

logger = logging.getLogger(__name__)

#: Audit ``event_type`` values (fixed, 32-char column).
EVENT_QUESTION_CREATED = "book_question_created"
EVENT_QUESTION_CORRECTED = "book_question_corrected"
EVENT_QUESTION_DELETED = "book_question_deleted"
EVENT_ANSWER_KEY_VERIFIED = "book_answer_key_verified"
EVENT_UNIT_THRESHOLD_SET = "book_unit_threshold_set"

#: The default passing threshold when the unit stores NULL.
DEFAULT_PASSING_THRESHOLD = 70

#: true/false questions require exactly these two options.
_TRUE_FALSE_OPTION_COUNT = 2


# --- reads ----------------------------------------------------------------------------


def list_questions(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
) -> list[BookQuestionRead]:
    """Every question of ONE exercise-set unit, in sibling order (admin only)."""
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id)
    _load_unit(session, revision, unit_id)
    return [
        _read_question(question)
        for question in _load_questions(session, unit_id)
    ]


# --- creation -------------------------------------------------------------------------


def create_question(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: BookQuestionCreate,
) -> BookQuestionRead:
    """Author ONE question on ONE exercise-set unit (admin only).

    Everything is validated against the loaded revision BEFORE any row
    is written, so a refused creation leaves questions, review state
    and audit trail untouched. The owning unit is reset to ``pending``:
    approval of the old assessment surface never covers new content.
    """
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="authored")
    unit = _load_unit(session, revision, unit_id)
    _require_exercise_set(unit)

    prompt = payload.prompt.strip()
    explanation = payload.explanation.strip()
    if not prompt:
        raise LearningValidationError("the question prompt cannot be blank")
    if not explanation:
        raise LearningValidationError("the question explanation cannot be blank")

    options = _validate_options(payload.question_type, payload.options)
    existing = _load_questions(session, unit_id)
    if any(question.display_order == payload.display_order for question in existing):
        raise LearningValidationError(
            f"a question with display_order {payload.display_order} "
            "already exists in this exercise set"
        )

    question = BookQuestion(
        book_revision_id=revision.id,
        book_unit_id=unit.id,
        question_type=payload.question_type.value,
        prompt=prompt,
        explanation=explanation,
        display_order=payload.display_order,
    )
    session.add(question)
    session.flush()
    for order, text in options:
        session.add(
            BookQuestionOption(
                book_question_id=question.id,
                option_text=text,
                display_order=order,
                is_correct=False,
            )
        )
    _reset_review(unit)
    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_QUESTION_CREATED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "book_unit_id": str(unit_id),
            "question_id": str(question.id),
            "question_type": payload.question_type.value,
            "option_count": len(options),
        },
    )
    session.flush()
    logger.info(
        "book question created",
        extra={
            "admin_id": str(user.id),
            "book_revision_id": str(revision_id),
            "question_id": str(question.id),
        },
    )
    return _read_question(question)


# --- correction -----------------------------------------------------------------------


def correct_question(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: BookQuestionCorrectRequest,
) -> BookQuestionRead:
    """Correct ONE question of a ``pending_review`` revision (admin only).

    Only supplied fields are applied (``model_fields_set``); an empty
    body is a 422. Supplying ``options`` replaces the WHOLE option set —
    fresh rows — so a half-edited list can never keep a stale answer
    key. EVERY correction clears the question's answer key (verification
    state and option flags) and resets the owning unit to ``pending``;
    all validation runs BEFORE any row is written, so a refused
    correction changes nothing.
    """
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="corrected")
    question = _load_question(session, revision, question_id)
    unit = _load_unit(session, revision, question.book_unit_id)

    supplied = payload.model_fields_set
    if not supplied:
        raise LearningValidationError("no correction fields supplied")

    if "prompt" in supplied:
        if payload.prompt is None:
            raise LearningValidationError("prompt cannot be null")
        prompt = payload.prompt.strip()
        if not prompt:
            raise LearningValidationError("the question prompt cannot be blank")
        question.prompt = prompt
    if "explanation" in supplied:
        if payload.explanation is None:
            raise LearningValidationError("explanation cannot be null")
        explanation = payload.explanation.strip()
        if not explanation:
            raise LearningValidationError("the question explanation cannot be blank")
        question.explanation = explanation
    if "display_order" in supplied:
        if payload.display_order is None:
            raise LearningValidationError("display_order cannot be null")
        siblings = [
            row
            for row in _load_questions(session, unit.id)
            if row.id != question.id
        ]
        if any(
            row.display_order == payload.display_order for row in siblings
        ):
            raise LearningValidationError(
                f"a question with display_order {payload.display_order} "
                "already exists in this exercise set"
            )
        question.display_order = payload.display_order
    if "options" in supplied:
        if payload.options is None:
            raise LearningValidationError("options cannot be null")
        # Validate BEFORE touching the collection: a refused correction
        # writes nothing at all, not even a transient delete.
        validated = _validate_options(
            BookQuestionType(question.question_type), payload.options
        )
        question.options.clear()
        session.flush()
        for order, text in validated:
            question.options.append(
                BookQuestionOption(
                    option_text=text,
                    display_order=order,
                    is_correct=False,
                )
            )

    # Any correction invalidates the answer key — the recorded verifier
    # must confirm the NEW content again, and until then the question
    # carries no key at all: verification state AND option flags are
    # cleared, so a corrected question looks freshly authored.
    question.answer_key_verified_by_user_id = None
    question.answer_key_verified_at = None
    for option in question.options:
        option.is_correct = False
    _reset_review(unit)
    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_QUESTION_CORRECTED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "book_unit_id": str(unit.id),
            "question_id": str(question.id),
            "fields": sorted(supplied),
        },
    )
    session.flush()
    logger.info(
        "book question corrected",
        extra={
            "admin_id": str(user.id),
            "book_revision_id": str(revision_id),
            "question_id": str(question.id),
        },
    )
    return _read_question(question)


# --- removal --------------------------------------------------------------------------


def delete_question(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    question_id: uuid.UUID,
) -> None:
    """Delete ONE question (and its options) of a pending revision (admin only).

    Published and rejected revisions refuse every mutation (409), so a
    graded question and the attempts that reference it are never
    rewritten. The owning exercise set resets to ``pending``.
    """
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="removed")
    question = _load_question(session, revision, question_id)
    unit = _load_unit(session, revision, question.book_unit_id)
    session.delete(question)
    _reset_review(unit)
    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_QUESTION_DELETED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "book_unit_id": str(unit.id),
            "question_id": str(question_id),
        },
    )
    session.flush()
    logger.info(
        "book question deleted",
        extra={
            "admin_id": str(user.id),
            "book_revision_id": str(revision_id),
            "question_id": str(question_id),
        },
    )


# --- answer-key verification -----------------------------------------------------------


def verify_question(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: BookQuestionVerifyRequest,
) -> BookQuestionRead:
    """Explicitly confirm THE correct option of ONE question (admin only).

    The named option must belong to this question (else the SAME 404 as
    an unknown id); every other option of the question is cleared, so
    exactly one correct option exists (the partial unique index is the
    database backstop). Verification is recorded with the administrator
    identity and timestamp — correctness is never inferred.
    """
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="verified")
    question = _load_question(session, revision, question_id)
    options = {option.id: option for option in question.options}
    chosen = options.get(payload.correct_option_id)
    if chosen is None:
        raise LearningNotFoundError(
            f"no option {payload.correct_option_id} of question {question_id}"
        )
    # Withdraw every other key FIRST and flush, then set the new one:
    # the partial unique index refuses two correct options of the same
    # question at ANY instant, so a single batch that sets the new key
    # before clearing the old one would be a self-inflicted violation.
    for option in options.values():
        if option.id != chosen.id:
            option.is_correct = False
    session.flush()
    chosen.is_correct = True
    question.answer_key_verified_by_user_id = user.id
    question.answer_key_verified_at = _now()
    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_ANSWER_KEY_VERIFIED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "book_unit_id": str(question.book_unit_id),
            "question_id": str(question.id),
            "correct_option_id": str(chosen.id),
        },
    )
    session.flush()
    logger.info(
        "book question answer key verified",
        extra={
            "admin_id": str(user.id),
            "book_revision_id": str(revision_id),
            "question_id": str(question.id),
        },
    )
    return _read_question(question)


# --- passing threshold ------------------------------------------------------------------


def set_unit_threshold(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: BookUnitThresholdRequest,
) -> BookUnitRead:
    """Configure ONE exercise set's passing threshold (admin only).

    NULL restores the default 70. Any effective change resets the unit's
    review status to ``pending``; setting the stored value again is an
    idempotent no-op. Only ``pending_review`` revisions and
    ``exercise_set`` units can be configured (409 / 422).
    """
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="configured")
    unit = _load_unit(session, revision, unit_id)
    _require_exercise_set(unit)

    new_value = payload.passing_threshold_percent
    if unit.passing_threshold_percent == new_value:
        return read_unit(unit)
    unit.passing_threshold_percent = new_value
    _reset_review(unit)
    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_UNIT_THRESHOLD_SET,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "book_unit_id": str(unit.id),
            "passing_threshold_percent": new_value,
        },
    )
    session.flush()
    logger.info(
        "book exercise set threshold set",
        extra={
            "admin_id": str(user.id),
            "book_revision_id": str(revision_id),
            "book_unit_id": str(unit.id),
        },
    )
    return read_unit(unit)


# --- helpers -----------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_admin(user) -> None:
    """Service-level guard (the route guard is not the only guard)."""
    if user.role != UserRole.ADMIN.value:
        raise LearningForbiddenError(
            "administrator role required for this operation"
        )


def _require_pending(revision: BookRevision, *, action: str) -> None:
    if revision.status != BookRevisionStatus.PENDING_REVIEW.value:
        raise LearningConflictError(
            f"questions can only be {action} while the revision is "
            f"pending_review (current status: {revision.status})"
        )


def _require_exercise_set(unit: BookUnit) -> None:
    if unit.kind != BookUnitKind.EXERCISE_SET.value:
        raise LearningValidationError(
            "objective questions can only be authored on an "
            f"exercise_set unit (this unit is a {unit.kind})"
        )


def _validate_options(
    question_type: BookQuestionType,
    options: list[BookQuestionOptionCreate],
) -> list[tuple[int, str]]:
    """Normalized (order, text) pairs, fully validated BEFORE any write."""
    if question_type == BookQuestionType.TRUE_FALSE and len(options) != _TRUE_FALSE_OPTION_COUNT:
        raise LearningValidationError(
            "a true_false question requires exactly two options"
        )
    seen_orders: set[int] = set()
    seen_texts: set[str] = set()
    normalized: list[tuple[int, str]] = []
    for option in options:
        text = option.option_text.strip()
        if not text:
            raise LearningValidationError("option texts cannot be blank")
        if option.display_order in seen_orders:
            raise LearningValidationError(
                f"duplicate option display_order {option.display_order}"
            )
        if text in seen_texts:
            raise LearningValidationError(
                "option texts must be distinct within a question"
            )
        seen_orders.add(option.display_order)
        seen_texts.add(text)
        normalized.append((option.display_order, text))
    normalized.sort()
    return normalized


def _load_book(session: Session, book_id: uuid.UUID) -> Book:
    book = session.scalar(select(Book).where(Book.id == book_id))
    if book is None:
        raise LearningNotFoundError(f"no book with id {book_id}")
    return book


def _load_revision(
    session: Session,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> BookRevision:
    stmt = select(BookRevision).where(
        BookRevision.id == revision_id, BookRevision.book_id == book_id
    )
    if for_update:
        # A locking read must not join other tables (PostgreSQL refuses
        # FOR UPDATE on the nullable side of an outer join).
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    revision = session.scalar(stmt)
    if revision is None:
        # A revision id from ANOTHER book answers exactly the same 404 as
        # an unknown id: the caller learns nothing about foreign revisions.
        raise LearningNotFoundError(f"no book revision with id {revision_id}")
    return revision


def _load_unit(
    session: Session, revision: BookRevision, unit_id: uuid.UUID
) -> BookUnit:
    unit = session.scalar(
        select(BookUnit).where(
            BookUnit.id == unit_id,
            BookUnit.book_revision_id == revision.id,
        )
    )
    if unit is None:
        raise LearningNotFoundError(f"no book unit with id {unit_id}")
    return unit


def _load_question(
    session: Session, revision: BookRevision, question_id: uuid.UUID
) -> BookQuestion:
    question = session.scalar(
        select(BookQuestion)
        .where(
            BookQuestion.id == question_id,
            BookQuestion.book_revision_id == revision.id,
        )
        .execution_options(populate_existing=True)
    )
    if question is None:
        raise LearningNotFoundError(f"no book question with id {question_id}")
    return question


def _load_questions(
    session: Session, unit_id: uuid.UUID
) -> list[BookQuestion]:
    return list(
        session.scalars(
            select(BookQuestion)
            .where(BookQuestion.book_unit_id == unit_id)
            .order_by(BookQuestion.display_order.asc(), BookQuestion.id.asc())
            .execution_options(populate_existing=True)
        )
    )


def _reset_review(unit: BookUnit) -> None:
    unit.review_status = BookUnitReviewStatus.PENDING.value
    unit.reviewed_by_user_id = None
    unit.reviewed_at = None


def _log(
    session: Session,
    *,
    revision: BookRevision,
    user,
    event_type: str,
    payload: dict,
) -> None:
    """One audit row about identifiers and fixed codes — never content."""
    auth_event_repo.log_event(
        session,
        user_id=revision.uploaded_by_user_id,
        event_type=event_type,
        metadata_json=json.dumps(payload),
        actor_user_id=user.id,
    )


def _read_question(question: BookQuestion) -> BookQuestionRead:
    return BookQuestionRead(
        question_id=question.id,
        book_revision_id=question.book_revision_id,
        book_unit_id=question.book_unit_id,
        question_type=BookQuestionType(question.question_type),
        prompt=question.prompt,
        explanation=question.explanation,
        display_order=question.display_order,
        answer_key_verified_by_user_id=question.answer_key_verified_by_user_id,
        answer_key_verified_at=question.answer_key_verified_at,
        options=[
            BookQuestionOptionRead(
                option_id=option.id,
                option_text=option.option_text,
                display_order=option.display_order,
                is_correct=option.is_correct,
                created_at=option.created_at,
                updated_at=option.updated_at,
            )
            for option in sorted(
                question.options, key=lambda option: option.display_order
            )
        ],
        created_at=question.created_at,
        updated_at=question.updated_at,
    )
