"""Student guided learning over administrator-published books (book P5).

All under ``/me/books`` and guarded by ``get_current_student`` — identity
always comes from the Bearer token, the revision is always the book's
CURRENT publication resolved server-side, and every operation names the
canonical ``learning_context`` the publication is valid for:

- ``GET  /`` — books for one explicit context (with progress counts);
- ``GET  /{book_id}`` — one book's identity + progress summary;
- ``GET  /{book_id}/next-unit`` — the single next available unit;
- ``GET  /{book_id}/units/{unit_id}`` — one reached unit (completed,
  current, or an optional exercise set already reached);
- ``POST /{book_id}/units/{unit_id}/complete`` — complete the current
  content unit (exercise sets complete through attempts);
- ``POST /{book_id}/units/{unit_id}/attempts`` — grade one attempt
  through the P4 service (explanation-only feedback, no answer key).

Endpoint bodies stay thin: validate schema -> delegate to the service ->
map the Phase 1 error family onto HTTP -> commit on writes.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_STUDENT
from app.core.auth_dependencies import get_current_student
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.student import Student
from app.schemas.book_assessment import AssessmentAttemptRead, AssessmentAttemptSubmit
from app.schemas.student_book import (
    StudentBookDetailRead,
    StudentBookListItemRead,
    StudentBookNextRead,
    StudentBookUnitRead,
)
from app.services import student_book_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/me/books", tags=[TAG_STUDENT])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get(
    "",
    response_model=list[StudentBookListItemRead],
    summary="Books valid for one learning context",
    description=(
        "The guided-learning library for one explicitly supplied canonical "
        "learning context: every book whose CURRENT published revision is "
        "valid for exactly that context, with your progress counts. An "
        "unknown context simply matches nothing. Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_student_books(
    request: Request,
    learning_context_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[StudentBookListItemRead]:
    try:
        return student_book_service.list_books(
            session, student.user, learning_context_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{book_id}",
    response_model=StudentBookDetailRead,
    summary="Read one book's identity and your progress",
    description=(
        "Identity and revision summary of one book whose CURRENT published "
        "revision is valid for the supplied learning context, with your "
        "completed/total unit counts. Counts only — the unit tree is never "
        "a listing surface. Unknown book, no current publication, and "
        "context mismatch all answer the same 404 (L6). Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {
            "description": "Unknown book, no current publication, or "
            "context mismatch"
        },
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_student_book(
    request: Request,
    book_id: uuid.UUID,
    learning_context_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> StudentBookDetailRead:
    try:
        return student_book_service.get_book(
            session, student.user, book_id, learning_context_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{book_id}/next-unit",
    response_model=StudentBookNextRead,
    summary="Your next available learning unit",
    description=(
        "The single next available unit of the book's current publication "
        "in document order: the first unit you have not completed, past "
        "optional exercise sets, behind any required checkpoint you have "
        "not passed yet. ``finished=true`` once every unit is completed. "
        "Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {
            "description": "Unknown book, no current publication, or "
            "context mismatch"
        },
        409: {
            "description": "The publication carries inconsistent review "
            "state (fail closed)"
        },
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_student_book_next_unit(
    request: Request,
    book_id: uuid.UUID,
    learning_context_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> StudentBookNextRead:
    try:
        return student_book_service.get_next_unit(
            session, student.user, book_id, learning_context_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{book_id}/units/{unit_id}",
    response_model=StudentBookUnitRead,
    summary="Read one reached learning unit",
    description=(
        "One unit you have legitimately reached: a completed unit (with "
        "its completion timestamp), the current available unit, or an "
        "optional exercise set at or before your position. Arbitrary "
        "later units, unknown ids and foreign revisions answer the same "
        "404 (L6). Exercise-set payloads carry questions for attempt "
        "submission — never the answer key. Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {
            "description": "Unknown unit, unit of another revision, or a "
            "unit you have not reached"
        },
        409: {
            "description": "The publication carries inconsistent review "
            "state (fail closed)"
        },
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_student_book_unit(
    request: Request,
    book_id: uuid.UUID,
    unit_id: uuid.UUID,
    learning_context_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> StudentBookUnitRead:
    try:
        return student_book_service.read_unit(
            session, student.user, book_id, unit_id, learning_context_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/{book_id}/units/{unit_id}/complete",
    response_model=StudentBookUnitRead,
    summary="Complete the current content unit",
    description=(
        "Mark the CURRENT sequentially available non-assessment unit as "
        "completed. Exercise sets are never accepted here — required "
        "checkpoints complete through passing attempts, optional practice "
        "never blocks. Duplicate requests answer 409 and write no second "
        "completion event. Students only."
    ),
    responses={
        200: {"description": "Unit completed (or its current completion state)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {
            "description": "Unknown unit, unit of another revision, or a "
            "unit you have not reached"
        },
        409: {
            "description": "Already completed, not the current unit, or "
            "an exercise set"
        },
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def complete_student_book_unit(
    request: Request,
    book_id: uuid.UUID,
    unit_id: uuid.UUID,
    learning_context_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> StudentBookUnitRead:
    try:
        completed = student_book_service.complete_unit(
            session, student.user, book_id, unit_id, learning_context_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return completed


@router.post(
    "/{book_id}/units/{unit_id}/attempts",
    response_model=AssessmentAttemptRead,
    summary="Submit one exercise-set attempt",
    description=(
        "Grade and durably record one complete attempt at an exercise set "
        "you have reached (P4 semantics: every question must be verified, "
        "one answer per question, equal weighting, exact threshold "
        "comparison, unlimited retries as new immutable attempts). A "
        "passing attempt completes the required checkpoint's progress in "
        "the same transaction. Feedback explains wrong answers — never "
        "the correct option id. Students only."
    ),
    responses={
        200: {"description": "Attempt graded and recorded"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {
            "description":             "Unknown book/unit, no current publication, "
            "context mismatch, or an unreachable exercise set"
        },
        409: {
            "description": "Exercise set not reached yet, or the "
            "publication carries inconsistent review state (fail closed)"
        },
        422: {
            "description": "Invalid submission (not an exercise set, "
            "missing/duplicated/unknown answers, unverified questions)"
        },
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def submit_student_book_attempt(
    request: Request,
    book_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: AssessmentAttemptSubmit,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> AssessmentAttemptRead:
    try:
        result = student_book_service.submit_attempt(
            session, student.user, book_id, unit_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return result
