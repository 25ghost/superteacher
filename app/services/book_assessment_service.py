"""Objective exercise grading and attempt recording (book P4, ADR-006).

The durable half of the assessment surface. One file owns every
attempt-scoped decision so the grading and persistence rules stay
uniform:

- **Access** (`record_attempt`) is exact-match and fail-closed: the
  caller must be an ACTIVE student with a student profile, the revision
  must be the book's CURRENT published revision, the supplied learning
  context must exactly match one the revision is valid for, and the
  unit must be an ``exercise_set`` of that revision. Historical
  published revisions accept no NEW attempts (their recorded attempts
  are immutable history).
- **Grading validity**: every question of the set must be VERIFIED
  (answer key confirmed by an administrator) — an unverified question
  can never be graded. The submission must be COMPLETE: one answer per
  question, no unknown question ids, no duplicates, no omissions; every
  selected option must be an option OF its question. Everything is
  validated BEFORE any row is written, so a refused submission persists
  nothing.
- **Scoring**: equal weighting (one point per question). The pass
  decision compares the EXACT correct-count/total-count fraction
  against the exercise set's threshold (default 70) with integer
  arithmetic — no rounding of the score before the comparison. Retries
  are unlimited: every accepted submission is a NEW immutable attempt.
- **Persistence**: the attempt, its per-question answers and the audit
  event are written in ONE transaction (this module flushes; the
  caller's endpoint owns the commit). An accepted attempt is never
  updated or deleted.
- **Feedback** carries the admin-authored explanation for each wrong
  answer and NEVER the correct option id, option correctness flags or
  any other answer-key field.

House rules, everywhere: unknown and foreign ids answer the SAME 404,
invalid state is 409, invalid content 422, and audit payloads carry
identifiers, counts and fixed codes only — never question text, option
text or filesystem paths. Event names fit the 32-char
``auth_events.event_type`` column.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.book_attempt import BookAttempt
from app.models.book_attempt_answer import BookAttemptAnswer
from app.models.book_question import BookQuestion
from app.models.book_question_option import BookQuestionOption
from app.models.book_revision import BookRevision
from app.models.book_revision_context import BookRevisionContext
from app.models.book_unit import BookUnit
from app.models.enums import (
    BookRevisionStatus,
    BookUnitKind,
    UserRole,
    UserStatus,
)
from app.models.student import Student
from app.repositories import auth_event_repository as auth_event_repo
from app.schemas.book_assessment import (
    AssessmentAttemptRead,
    AssessmentAttemptSubmit,
    AssessmentQuestionFeedback,
)
from app.services.book_question_service import DEFAULT_PASSING_THRESHOLD
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)

logger = logging.getLogger(__name__)

#: Audit ``event_type`` value (fixed, 32-char column).
EVENT_ATTEMPT_RECORDED = "book_attempt_recorded"


def record_attempt(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: AssessmentAttemptSubmit,
) -> AssessmentAttemptRead:
    """Grade and durably record ONE complete attempt (student only).

    The whole submission is validated first (access, completeness,
    option ownership); only then are the attempt, its answers and the
    audit event written in one flush — a refused submission changes
    nothing, an accepted one is immutable history.
    """
    student = _load_student(session, user)
    revision = _load_current_revision(session, book_id, revision_id)
    _require_context(session, revision, payload.learning_context_id)
    unit = _load_exercise_set(session, revision, unit_id)
    questions = _load_verified_questions(session, unit)
    answers = _validate_submission(questions, payload)

    correct = sum(1 for question_id, option in answers if option.is_correct)
    total = len(questions)
    threshold = (
        unit.passing_threshold_percent
        if unit.passing_threshold_percent is not None
        else DEFAULT_PASSING_THRESHOLD
    )
    # Exact fraction comparison — no rounding before the pass decision.
    passed = correct * 100 >= total * threshold
    score_percent = correct / total * 100

    attempt = BookAttempt(
        student_id=student.id,
        book_revision_id=revision.id,
        book_unit_id=unit.id,
        total_questions=total,
        correct_answers=correct,
        score_percent=score_percent,
        passed=passed,
    )
    session.add(attempt)
    session.flush()
    for question_id, option in answers:
        session.add(
            BookAttemptAnswer(
                book_attempt_id=attempt.id,
                book_question_id=question_id,
                selected_option_id=option.id,
                is_correct=option.is_correct,
            )
        )
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type=EVENT_ATTEMPT_RECORDED,
        metadata_json=json.dumps(
            {
                "book_id": str(book_id),
                "book_revision_id": str(revision_id),
                "book_unit_id": str(unit_id),
                "book_attempt_id": str(attempt.id),
                "total_questions": total,
                "correct_answers": correct,
                "passed": passed,
            }
        ),
    )
    session.flush()
    logger.info(
        "book attempt recorded",
        extra={
            "student_id": str(user.id),
            "book_revision_id": str(revision_id),
            "book_attempt_id": str(attempt.id),
        },
    )

    by_question = {question_id: option for question_id, option in answers}
    feedback = [
        AssessmentQuestionFeedback(
            question_id=question.id,
            explanation=question.explanation,
        )
        for question in questions
        if not by_question[question.id].is_correct
    ]
    return AssessmentAttemptRead(
        attempt_id=attempt.id,
        book_revision_id=revision.id,
        book_unit_id=unit.id,
        total_questions=total,
        correct_answers=correct,
        score_percent=score_percent,
        passed=passed,
        submitted_at=attempt.created_at,
        feedback=feedback,
    )


# --- helpers -------------------------------------------------------------------------


def _load_student(session: Session, user) -> Student:
    """The caller's student profile (refuses non-students, 404 if missing).

    An inactive account never reaches grading: the shared auth layer
    already refuses its tokens, and the service re-checks so a stale
    session cannot record an attempt.
    """
    if user.role != UserRole.STUDENT.value:
        raise LearningForbiddenError("student role required for this operation")
    if user.status != UserStatus.ACTIVE.value:
        raise LearningForbiddenError("this account is not active")
    profile = session.scalar(
        select(Student).where(Student.user_id == user.id)
    )
    if profile is None:
        raise LearningNotFoundError("no student profile for this account")
    return profile


def _load_current_revision(
    session: Session, book_id: uuid.UUID, revision_id: uuid.UUID
) -> BookRevision:
    """The book's CURRENT published revision (else 404/409).

    A revision id from another book, an unknown id, or a published
    revision that is no longer the current publication all refuse —
    new attempts only ever target the live edition, while historical
    attempts stay untouched with their own revision.
    """
    revision = session.scalar(
        select(BookRevision).where(
            BookRevision.id == revision_id, BookRevision.book_id == book_id
        )
    )
    if revision is None:
        raise LearningNotFoundError(f"no book revision with id {revision_id}")
    if revision.status != BookRevisionStatus.PUBLISHED.value:
        raise LearningConflictError(
            "this revision is not published "
            f"(current status: {revision.status})"
        )
    if not revision.is_current_publication:
        raise LearningConflictError(
            "this revision is no longer the current publication"
        )
    return revision


def _require_context(
    session: Session, revision: BookRevision, learning_context_id: uuid.UUID
) -> None:
    """Exact canonical context match — never inferred (ADR-002)."""
    linked = session.scalar(
        select(BookRevisionContext.id).where(
            BookRevisionContext.book_revision_id == revision.id,
            BookRevisionContext.learning_context_id == learning_context_id,
        )
    )
    if linked is None:
        raise LearningNotFoundError(
            "this revision is not valid for the supplied learning context"
        )


def _load_exercise_set(
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
    if unit.kind != BookUnitKind.EXERCISE_SET.value:
        raise LearningValidationError(
            "only an exercise_set unit accepts attempts "
            f"(this unit is a {unit.kind})"
        )
    return unit


def _load_verified_questions(
    session: Session, unit: BookUnit
) -> list[BookQuestion]:
    """Every question of the set, answer key confirmed, in sibling order.

    A set without questions, or with ANY unverified question, can never
    be graded — the attempt is refused (422) rather than silently
    skipping the unverified content.
    """
    questions = list(
        session.scalars(
            select(BookQuestion)
            .where(BookQuestion.book_unit_id == unit.id)
            .options(selectinload(BookQuestion.options))
            .order_by(BookQuestion.display_order.asc(), BookQuestion.id.asc())
            .execution_options(populate_existing=True)
        )
    )
    if not questions:
        raise LearningValidationError(
            "this exercise set has no questions to grade"
        )
    unverified = [
        question for question in questions if question.answer_key_verified_at is None
    ]
    if unverified:
        raise LearningValidationError(
            f"{len(unverified)} of {len(questions)} questions are unverified; "
            "every question needs a verified answer key before it can be graded"
        )
    for question in questions:
        if sum(1 for option in question.options if option.is_correct) != 1:
            # A verified question always has exactly one correct option
            # (the verification action set it; the partial unique index
            # refuses two). Treat any other state as ungradeable.
            raise LearningValidationError(
                "a verified question must have exactly one correct option"
            )
    return questions


def _validate_submission(
    questions: list[BookQuestion],
    payload: AssessmentAttemptSubmit,
) -> list[tuple[uuid.UUID, BookQuestionOption]]:
    """Complete submission as (question_id, selected option) pairs.

    No unknown question ids, no duplicates, no omissions, and every
    selected option is an option OF its question — all refused (422)
    BEFORE anything is written.
    """
    by_id = {question.id: question for question in questions}
    seen: set[uuid.UUID] = set()
    pairs: list[tuple[uuid.UUID, BookQuestionOption]] = []
    for answer in payload.answers:
        if answer.question_id not in by_id:
            raise LearningValidationError(
                f"question {answer.question_id} is not part of this exercise set"
            )
        if answer.question_id in seen:
            raise LearningValidationError(
                f"question {answer.question_id} was answered more than once"
            )
        options = {
            option.id: option for option in by_id[answer.question_id].options
        }
        option = options.get(answer.option_id)
        if option is None:
            raise LearningValidationError(
                f"option {answer.option_id} is not an option of question "
                f"{answer.question_id}"
            )
        seen.add(answer.question_id)
        pairs.append((answer.question_id, option))
    omitted = [question.id for question in questions if question.id not in seen]
    if omitted:
        raise LearningValidationError(
            f"{len(omitted)} questions were not answered; the submission "
            "must answer every question exactly once"
        )
    return pairs
