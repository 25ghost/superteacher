"""Administrative student-profile API — ``/admin/students`` (role-split).

Every endpoint requires an administrator (the ``admin`` role via the
reusable ``require_admin`` guard); the self-service counterpart is
``/me/student`` (role=student, same JWT):

- ``POST /admin/students`` — mints the user identity (role fixed to
  ``student``) and the student profile atomically, with a REQUIRED email.
  Self-service alternatives: ``POST /auth/register`` (public — account +
  profile + tokens) and ``POST /me/student`` (authenticated student —
  profile for an existing account that has none).
- ``GET /admin/students`` — paginated list of student profiles
  (``items``/``total``/``limit``/``offset`` envelope) with a
  case-insensitive ``q`` search over email and full name, plus
  ``gender``/``country`` filters.
- ``GET /admin/students/{student_id}`` — read any profile.
- ``PATCH /admin/students/{student_id}`` — update any profile (audited
  with the acting administrator). Student-owned editing is
  ``PATCH /me/student``.
- ``GET /admin/students/{student_id}/history`` — the profile audit trail
  (paginated).

No endpoint in this module serves a student token: cross-role calls are
refused with 403 before any handler runs. The acting identity always
comes from the Bearer token; a student cannot reach these routes by
changing the path UUID.

Boundary unchanged: personal profile data only (name, gender, country).
email/phone/date_of_birth are identity anchors and stay immutable.

Error contract: unknown student ids surface as 404 via the application-
wide ``ProfileError`` handler (registered in ``app.main``).

PATCH semantics: only fields present in the request body change (the
endpoint passes exactly ``model_fields_set`` through to the service —
shared ``profile_update_kwargs`` helper). An explicit ``null`` on
``gender``/``country`` clears that column; ``full_name`` cannot be cleared
(schema-level 422). Unknown keys — including the identity anchors — are
rejected with 422 (``extra="forbid"``).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_ADMIN_STUDENTS
from app.core.auth_dependencies import require_admin
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.repositories import student_profile_history_repository as history_repo
from app.schemas.pagination import Page
from app.schemas.student_profile import (
    StudentProfileCreate,
    StudentProfileHistoryRead,
    StudentProfileRead,
    StudentProfileUpdate,
)
from app.services import student_service
from app.services.student_service import ProfileError

_settings = get_settings()

router = APIRouter(prefix="/admin/students", tags=[TAG_ADMIN_STUDENTS])


def _http_error(exc: ProfileError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post(
    "",
    response_model=StudentProfileRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a bare student profile (administrative)",
    description=(
        "Administrative profile creation for admin callers "
        "(e.g. a school enrolling a student without self-service "
        "signup). Ordinary student self-registration is POST /auth/register. "
        "Creates the user identity (role fixed to 'student') and the "
        "student profile atomically; an email is required (login and "
        "password reset are email-based). The creation is recorded in the "
        "profile audit trail, attributed to the acting administrator. "
        "Duplicate email/phone or an existing profile for the same "
        "identity yields 409."
    ),
    responses={
        201: {"description": "Profile pair created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        409: {"description": "Duplicate identity or existing profile"},
        422: {"description": "Validation error (email required, DOB, fields)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def create_student_profile(
    request: Request,
    payload: StudentProfileCreate,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> StudentProfileRead:
    try:
        profile = student_service.create_student_profile(
            session, payload, changed_by=admin.id
        )
    except ProfileError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return profile


@router.get(
    "",
    response_model=Page[StudentProfileRead],
    summary="List student profiles (administrative)",
    description=(
        "One page of student profiles, newest first, in an "
        "items/total/limit/offset envelope. limit defaults to 20 (max "
        "100), offset defaults to 0; both are 422 outside their range. "
        "?q= case-insensitively matches the account email and the "
        "profile full name, treating %, _ and \\ as literal characters. "
        "?gender= and ?country= filter on the profile fields (an unknown "
        "gender value is 422). Ordering is deterministic "
        "(created_at desc, id) so offset paging stays stable. "
        "Administrator-only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        422: {"description": "Invalid query parameters (limit/offset/gender)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_student_profiles(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=255),
    gender: str | None = Query(default=None, max_length=32),
    country: str | None = Query(default=None, max_length=80),
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Page[StudentProfileRead]:
    # Read path: translate domain errors only; no commit/rollback (the
    # sibling GETs in this module behave the same way).
    try:
        return student_service.list_students(
            session, limit=limit, offset=offset, q=q, gender=gender, country=country
        )
    except ProfileError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{student_id}",
    response_model=StudentProfileRead,
    summary="Retrieve any student profile (administrative)",
    description=(
        "Returns the profile pair for one student id. Administrator-only "
        "(admin role); students read their own profile via "
        "GET /me/student. 404 unknown id."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown student id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_student_profile(
    request: Request,
    student_id: uuid.UUID,
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> StudentProfileRead:
    student = student_service.load_student_row(session, student_id)
    profile = student_service.read_profile(session.get(User, student.user_id), student)
    return profile


@router.patch(
    "/{student_id}",
    response_model=StudentProfileRead,
    summary="Update mutable profile fields (administrative)",
    description=(
        "Updates full_name / gender / country for one student id. "
        "Administrator-only; students edit their own profile via "
        "PATCH /me/student. Only supplied fields change; an explicit null "
        "clears gender or country (full_name cannot be cleared). email, "
        "phone and date_of_birth are identity anchors and are deliberately "
        "immutable here. Every change is audit-logged with the acting "
        "administrator."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown student id"},
        422: {"description": "Validation error"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def patch_student_profile(
    request: Request,
    student_id: uuid.UUID,
    payload: StudentProfileUpdate,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> StudentProfileRead:
    student = student_service.load_student_row(session, student_id)
    try:
        profile = student_service.update_student_profile(
            session,
            student_id,
            changed_by=admin.id,
            **student_service.profile_update_kwargs(payload),
        )
    except ProfileError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return profile


@router.get(
    "/{student_id}/history",
    response_model=list[StudentProfileHistoryRead],
    summary="View profile change history (administrative)",
    description=(
        "Administrative audit trail for one student's profile changes: "
        "creation events (change_type='create') and field modifications "
        "(old/new values, who changed, when — resolved to the actor's "
        "email). Paginated via limit (1-200, default 50) and offset. "
        "Only accessible by the admin role."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown student id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_HISTORY)
def get_profile_history(
    request: Request,
    student_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[StudentProfileHistoryRead]:
    try:
        student = student_service.load_student_row(session, student_id)
    except ProfileError as exc:
        raise _http_error(exc) from exc

    records = history_repo.list_for_student(
        session, student.id, limit=limit, offset=offset
    )

    # Resolve changed_by → email in one extra query so administrators see
    # a human actor, not a bare UUID (deleted users stay None).
    actor_ids = {r.changed_by for r in records if r.changed_by is not None}
    emails: dict[uuid.UUID, str | None] = {}
    if actor_ids:
        actors = session.scalars(select(User).where(User.id.in_(actor_ids)))
        emails = {actor.id: actor.email for actor in actors}

    return [
        StudentProfileHistoryRead(
            id=record.id,
            student_id=record.student_id,
            field_name=record.field_name,
            old_value=record.old_value,
            new_value=record.new_value,
            changed_by=record.changed_by,
            changed_by_email=emails.get(record.changed_by),
            change_type=record.change_type,
            created_at=record.created_at,
        )
        for record in records
    ]
