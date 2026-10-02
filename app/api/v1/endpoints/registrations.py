"""Registration API — role-split (public / student / administration).

Every endpoint lives under exactly one guard:

Public:

- ``GET /registrations/readiness`` — read-only catalog availability,
  needed pre-login by the portal; creates nothing.

Student Self-Service (role=student, ``/me`` prefix):

- ``POST /me/registrations`` — register *yourself*: the student identity
  comes from the Bearer token; the body has no ``student_id`` field at
  all (``extra="forbid"`` makes a spoofing attempt a 422).
- ``GET /me/registrations`` — your enrollment history (newest academic
  year first), the preferred form for the Student Portal.
- ``GET /me/registrations/{enrollment_id}`` — one of your enrollments;
  someone else's id is a 403 (UUIDs are identifiers, not authorization).

Administration (admin role, ``/admin`` prefix):

- ``GET /admin/registrations`` — paginated list of registrations with
  academic year / status / school / student filters.
- ``POST /admin/registrations`` — register on behalf of a student;
  ``student_id`` is REQUIRED in the body.
- ``GET /admin/registrations/{enrollment_id}`` — any enrollment.
- ``GET /admin/students/{student_id}/registrations`` — any student's
  history (404 for an unknown student).

Cross-role calls are refused with 403 by the route guards before any
handler runs — no endpoint serves both a student and an administrator.

Endpoint bodies stay thin: validate schema → delegate to the service →
map service errors to HTTP → commit on success.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.auth_dependencies import (
    get_current_student,
    require_admin,
)
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.enums import EnrollmentStatus
from app.models.student import Student
from app.models.user import User
from app.schemas.pagination import Page
from app.schemas.registration import (
    RegistrationCreate,
    RegistrationCreateAdmin,
    RegistrationCreateSelf,
    RegistrationRead,
    RegistrationReadiness,
)
from app.services import registration_service
from app.services.registration_service import RegistrationError, RegistrationNotFoundError

_settings = get_settings()

# Public readiness report — the only endpoint left on /registrations.
router = APIRouter(prefix="/registrations", tags=["registrations"])

# Student self-service registration routes (mounted under /me).
me_router = APIRouter(prefix="/me", tags=["Student Self-Service", "registrations"])

# Administrative registration routes (mounted under /admin).
admin_router = APIRouter(prefix="/admin", tags=["Administration", "registrations"])


def _http_error(exc: RegistrationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


# --- public -------------------------------------------------------------------------


@router.get(
    "/readiness",
    response_model=RegistrationReadiness,
    summary="Report whether registration can currently proceed",
    description=(
        "Read-only readiness report: whether an open (planned/active) "
        "academic year exists and whether the pathway, education level, "
        "program, school and TVET catalogs are populated. Public (needed "
        "pre-login by the portal); creates nothing."
    ),
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def read_registration_readiness(
    request: Request,
    session: Session = Depends(get_db),
) -> RegistrationReadiness:
    try:
        return RegistrationReadiness(
            **registration_service.registration_readiness(session)
        )
    except registration_service.RegistrationError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise


# --- student self-service ------------------------------------------------------------


@me_router.post(
    "/registrations",
    response_model=RegistrationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register yourself into an academic year",
    description=(
        "Authenticated students register themselves: the student identity "
        "is derived from the Bearer token and the request carries no "
        "student_id field at all (spoofing another student's UUID is "
        "refused with 422 by the schema). Validates the full registration "
        "context — academic year registrable, pathway/level mapped, "
        "program version in context, school actually offering the program "
        "— then creates the enrollment atomically with derived subjects. "
        "Already enrolled that year → 409. Closed year → 503. "
        "Administrators use POST /admin/registrations."
    ),
    responses={
        201: {"description": "Enrollment created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown academic year, pathway, level, program version or school"},
        409: {"description": "The student already has an enrollment in this academic year"},
        422: {"description": "Invalid request (pathway-level pair, program-version context, school offering, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Registration not possible right now (closed/archived year, missing TVET profile)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_WRITE)
def create_my_registration(
    request: Request,
    payload: RegistrationCreateSelf,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> RegistrationRead:
    full_payload = RegistrationCreate(
        student_id=student.id,
        academic_year_id=payload.academic_year_id,
        pathway=payload.pathway,
        education_level=payload.education_level,
        program_version_id=payload.program_version_id,
        school_id=payload.school_id,
    )
    try:
        registration = registration_service.register_student(session, full_payload)
    except RegistrationError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return registration


@me_router.get(
    "/registrations",
    response_model=list[RegistrationRead],
    summary="List your registrations",
    description=(
        "Your enrollment history (newest academic year first) for the "
        "caller identified by the Bearer token — the preferred form for "
        "the Student Portal. Non-students → 403."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def list_my_registrations(
    request: Request,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[RegistrationRead]:
    try:
        return registration_service.list_student_registrations(session, student.id)
    except RegistrationError as exc:
        raise _http_error(exc) from exc


@me_router.get(
    "/registrations/{enrollment_id}",
    response_model=RegistrationRead,
    summary="Retrieve one of your registrations",
    description=(
        "Full registration summary for one of YOUR enrollment ids "
        "(identity from the Bearer token). An enrollment that exists but "
        "belongs to another student returns the SAME 404 as an unknown id "
        "(L6 existence leak): a foreign UUID must not be distinguishable "
        "from a non-existent one. Administrators read any enrollment via "
        "GET /admin/registrations/{enrollment_id}."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        404: {"description": "Unknown enrollment id, or not one of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def read_my_registration(
    request: Request,
    enrollment_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> RegistrationRead:
    try:
        registration = registration_service.get_registration(session, enrollment_id)
    except RegistrationError as exc:
        raise _http_error(exc) from exc
    if registration.student_id != student.id:
        # Not owned: answer with the exact error the unknown-id path raises,
        # so status, detail and echo of the id are byte-identical and no
        # observer can probe for the existence of another student's row.
        raise _http_error(
            RegistrationNotFoundError(f"no enrollment with id {enrollment_id}")
        )
    return registration


# --- administration ------------------------------------------------------------------


@admin_router.post(
    "/registrations",
    response_model=RegistrationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a student on their behalf (administrative)",
    description=(
        "Trusted administrative workflow (admin role): "
        "``student_id`` is REQUIRED and names the student being "
        "registered. Same service-level validation as the self-service "
        "route — academic year registrable, pathway/level mapped, program "
        "version in context, school actually offering the program. "
        "Already enrolled that year → 409. Closed year → 503. The "
        "creation is recorded in the enrollment history for that student."
    ),
    responses={
        201: {"description": "Enrollment created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown student, academic year, pathway, level, program version or school"},
        409: {"description": "The student already has an enrollment in this academic year"},
        422: {"description": "Invalid request (missing student_id, pathway-level pair, program-version context, school offering)"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Registration not possible right now (closed/archived year, missing TVET profile)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_WRITE)
def create_registration_for_student(
    request: Request,
    payload: RegistrationCreateAdmin,
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> RegistrationRead:
    full_payload = RegistrationCreate(**payload.model_dump())
    try:
        registration = registration_service.register_student(session, full_payload)
    except RegistrationError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return registration


@admin_router.get(
    "/registrations",
    response_model=Page[RegistrationRead],
    summary="List registrations (administrative)",
    description=(
        "One page of registrations in an items/total/limit/offset "
        "envelope, newest first. limit defaults to 20 (max 100), offset "
        "defaults to 0; both are 422 outside their range. Optional "
        "filters: academic_year_id, status (validated against the "
        "enrollment vocabulary — an unknown value is 422), school_id, "
        "school_code and student_id, combinable in any way. The page "
        "eager-loads program version, program, school and subjects, so "
        "the number of queries per request does not depend on the row "
        "count. Administrator-only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        422: {"description": "Invalid query parameters (limit/offset/status/ids)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def list_registrations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    academic_year_id: uuid.UUID | None = Query(default=None),
    status_filter: EnrollmentStatus | None = Query(default=None, alias="status"),
    school_id: uuid.UUID | None = Query(default=None),
    school_code: str | None = Query(default=None, max_length=32),
    student_id: uuid.UUID | None = Query(default=None),
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Page[RegistrationRead]:
    # Read path: translate domain errors only; no commit/rollback (the
    # sibling GETs in this module behave the same way).
    try:
        return registration_service.list_admin_registrations(
            session,
            limit=limit,
            offset=offset,
            academic_year_id=academic_year_id,
            status=status_filter.value if status_filter is not None else None,
            school_id=school_id,
            school_code=school_code,
            student_id=student_id,
        )
    except RegistrationError as exc:
        raise _http_error(exc) from exc


@admin_router.get(
    "/registrations/{enrollment_id}",
    response_model=RegistrationRead,
    summary="Retrieve any registration (administrative)",
    description=(
        "Full registration summary for one enrollment id. "
        "Administrator-only; students read their own via "
        "GET /me/registrations/{enrollment_id}. The enrollment UUID is an "
        "identifier, not authorization."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown enrollment id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def read_registration(
    request: Request,
    enrollment_id: uuid.UUID,
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> RegistrationRead:
    try:
        return registration_service.get_registration(session, enrollment_id)
    except RegistrationError as exc:
        raise _http_error(exc) from exc


@admin_router.get(
    "/students/{student_id}/registrations",
    response_model=list[RegistrationRead],
    summary="List any student's registrations (administrative)",
    description=(
        "The student's enrollment history, newest academic year first. "
        "Administrator-only; students list their own via "
        "GET /me/registrations. 404 for an unknown student id."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown student id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def list_registrations_for_student(
    request: Request,
    student_id: uuid.UUID,
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[RegistrationRead]:
    # The service itself 404s an unknown student id (RegistrationError).
    try:
        return registration_service.list_student_registrations(session, student_id)
    except RegistrationError as exc:
        raise _http_error(exc) from exc
