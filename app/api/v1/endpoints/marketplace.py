"""Marketplace discovery and enrollment (Phase 1).

Student-only (``require_student``) — no endpoint serves two roles:

- ``GET /marketplace/offerings`` — discover live teaching offerings:
  ``active`` offers by **approved** teachers (suspending a teacher hides
  theirs immediately), optionally filtered by academic year, pathway,
  education level or subject, newest first.
- ``POST /marketplace/offerings/{offering_id}/enroll`` — enroll yourself
  into one offering. The identity comes from the Bearer token, so a body
  cannot name another student. Only active offerings accept enrollments
  (409 otherwise), and a student may hold only ONE active enrollment per
  learning context — joining a second offering of a context you are
  already in is refused until you leave it first (409 with the remedy).

Endpoint bodies stay thin: validate schema → delegate to the service →
map the Phase 1 error family onto HTTP → commit on success.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_MARKETPLACE
from app.core.auth_dependencies import get_current_student
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.student import Student
from app.schemas.learning import LearningEnrollmentRead, TeachingOfferingRead
from app.services import learning_enrollment_service, teaching_offering_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/marketplace", tags=[TAG_MARKETPLACE])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get(
    "/offerings",
    response_model=list[TeachingOfferingRead],
    summary="Discover live teaching offerings",
    description=(
        "One page of discoverable offerings: status ``active`` AND taught "
        "by a teacher whose verification is ``approved`` — so an "
        "administrative suspension takes effect on the next request without "
        "touching the rows. Optional filters: academic_year_id, pathway_id, "
        "education_level_id and subject_id (unknown values simply match "
        "nothing — they are filters, not identity). limit defaults to 20 "
        "(max 100), offset to 0; both are 422 outside their range. Ordering "
        "is deterministic (created_at desc, id) so offset paging stays "
        "stable. Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        422: {"description": "Invalid query parameters (limit/offset/ids)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_marketplace_offerings(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    academic_year_id: uuid.UUID | None = Query(default=None),
    pathway_id: uuid.UUID | None = Query(default=None),
    education_level_id: uuid.UUID | None = Query(default=None),
    subject_id: uuid.UUID | None = Query(default=None),
    session: Session = Depends(get_db),
    _: Student = Depends(get_current_student),
) -> list[TeachingOfferingRead]:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return teaching_offering_service.list_marketplace_offerings(
            session,
            academic_year_id=academic_year_id,
            pathway_id=pathway_id,
            education_level_id=education_level_id,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/offerings/{offering_id}/enroll",
    response_model=LearningEnrollmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Enroll yourself into a teaching offering",
    description=(
        "Creates YOUR active enrollment in the offering (identity from the "
        "Bearer token; the request carries no student_id at all). The "
        "offering must exist (404) and be ``active`` (paused/archived → "
        "409). You may hold only one active enrollment per learning context: "
        "if you are already enrolled in another offering of the same "
        "context, this is 409 until you leave it "
        "(POST /me/learning-enrollments/{id}/leave) — leaving first is the "
        "explicit switch step. Students only."
    ),
    responses={
        201: {"description": "Enrollment created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown offering id"},
        409: {"description": "Offering not accepting enrollments, or you already have an active enrollment in this context"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def enroll_in_offering(
    request: Request,
    offering_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> LearningEnrollmentRead:
    try:
        enrollment = learning_enrollment_service.enroll(
            session, student, offering_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return enrollment
