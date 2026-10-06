"""Student self-service: learning enrollments (Phase 1).

All under ``/me/learning-enrollments`` and guarded by
``get_current_student`` — the identity always comes from the Bearer token:

- ``GET /me/learning-enrollments`` — your learning history (active and
  ended), newest first, with the offering and its context flattened on.
- ``GET /me/learning-enrollments/{enrollment_id}`` — one of yours; a
  foreign id answers the same 404 as an unknown one (L6 existence leak).
- ``POST /me/learning-enrollments/{enrollment_id}/leave`` — end one active
  enrollment. This is the explicit "leave before you switch" step: the
  partial unique index allows only ONE active enrollment per learning
  context, so leaving is what frees the slot for another offering of that
  context. Already ended → 409; unknown or foreign id → 404.

Enrollment itself happens in the marketplace
(``POST /marketplace/offerings/{offering_id}/enroll``).

Endpoint bodies stay thin: validate schema → delegate to the service →
map the Phase 1 error family onto HTTP → commit on success.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_STUDENT
from app.core.auth_dependencies import get_current_student
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.student import Student
from app.schemas.learning import LearningEnrollmentRead
from app.services import learning_enrollment_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/me/learning-enrollments", tags=[TAG_STUDENT])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get(
    "",
    response_model=list[LearningEnrollmentRead],
    summary="List your learning enrollments",
    description=(
        "Your learning history — active and ended enrollments, newest "
        "first — each row carrying the offering (status, description, "
        "teacher) and its context. Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_my_learning_enrollments(
    request: Request,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[LearningEnrollmentRead]:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return learning_enrollment_service.list_my_enrollments(session, student)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{enrollment_id}",
    response_model=LearningEnrollmentRead,
    summary="Retrieve one of your learning enrollments",
    description=(
        "Full summary of one of YOUR enrollment ids (identity from the "
        "Bearer token). An enrollment that exists but belongs to another "
        "student returns the SAME 404 as an unknown id (L6 existence leak). "
        "Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown enrollment id, or not one of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_learning_enrollment(
    request: Request,
    enrollment_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> LearningEnrollmentRead:
    try:
        return learning_enrollment_service.get_my_enrollment(
            session, student, enrollment_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/{enrollment_id}/leave",
    response_model=LearningEnrollmentRead,
    summary="Leave one of your learning enrollments",
    description=(
        "Ends one ACTIVE enrollment (status → ``ended``, ``ended_at`` "
        "stamped). Leaving is the explicit step that frees the one-active-"
        "enrollment-per-context slot, so you can then join another "
        "offering of the same context from the marketplace. The offering "
        "itself is untouched — leaving is a property of your own membership. "
        "Already ended → 409; unknown or foreign id → 404. Exactly one "
        "audit event (learning_enrollment_ended) is written. Students only."
    ),
    responses={
        200: {"description": "Enrollment ended"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown enrollment id, or not one of yours"},
        409: {"description": "The enrollment is already ended"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def leave_learning_enrollment(
    request: Request,
    enrollment_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> LearningEnrollmentRead:
    try:
        ended = learning_enrollment_service.leave(session, student, enrollment_id)
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return ended
