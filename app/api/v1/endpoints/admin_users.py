"""Administrative account API — ``/admin/teachers`` and ``/admin/users`` (role-split).

Every route requires an administrator (``require_admin``); no handler here
serves a student or teacher token — cross-role calls are refused with 403
before any logic runs, and anonymous callers get 401.

- ``POST /admin/teachers`` — create a ``pending`` teacher account with a
  profile row and a single-use invitation email (201). The role, status and
  the absence of a password are fixed server-side; the raw invitation token
  is never in the response.
- ``GET /admin/teachers`` — list teacher accounts (paginated).
- ``POST /admin/teachers/{user_id}/invite`` — re-send the invitation
  (rotates the token; the previous link stops working). 409 when the
  account is no longer awaiting acceptance.
- ``POST /admin/teachers/{user_id}/activate`` — pending/suspended → active.
- ``POST /admin/teachers/{user_id}/deactivate`` — → suspended, revoking
  every refresh session.
- ``PATCH /admin/users/{user_id}/role`` — change any *other* account's
  role (self-change → 403); sessions are revoked so an old token cannot
  keep the previous privileges.
- ``POST /admin/users/{user_id}/unlock`` — clear a login lockout early
  (slice 7). 409 when the account is not locked.

Audit: each mutation writes an ``auth_events`` row naming the acting
administrator (``actor_user_id``), so the trail answers "who did this".
Error contract: unknown ids → 404, duplicates / invalid transitions → 409,
validation → 422 (shared ``AuthError`` family).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_ADMIN_TEACHERS, TAG_ADMIN_USERS
from app.core.auth_dependencies import require_admin
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.teacher_admin import (
    AdminUserRead,
    RoleChangeRequest,
    TeacherCreate,
    TeacherRead,
)
from app.services import admin_user_service
from app.services.auth_service import AuthError

_settings = get_settings()

# No router-level tags: this router mixes two groups, so every route below
# declares its own single tag (Admin - Teachers vs Admin - Users).
router = APIRouter(prefix="/admin")


def _error(exc: AuthError) -> HTTPException:
    """Shared error mapping (identical to the auth endpoints)."""
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post(
    "/teachers",
    tags=[TAG_ADMIN_TEACHERS],
    response_model=TeacherRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a teacher account and send its invitation (administrative)",
    description=(
        "Creates the user identity (role fixed to 'teacher', status "
        "'pending', no password yet) plus the teacher profile, then emails "
        "a single-use invitation link valid for 72 hours. The invitation "
        "token is never returned by the API or stored in plaintext. "
        "Duplicate email → 409. Administrator-only."
    ),
    responses={
        201: {"description": "Teacher account created and invitation sent"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        409: {"description": "Email already in use"},
        422: {"description": "Validation error"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def create_teacher(
    request: Request,
    payload: TeacherCreate,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TeacherRead:
    try:
        created = admin_user_service.create_teacher(session, payload, actor=admin)
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return created


@router.get(
    "/teachers",
    tags=[TAG_ADMIN_TEACHERS],
    response_model=list[TeacherRead],
    summary="List teacher accounts (administrative)",
    description=(
        "Returns teacher accounts, newest first, paginated via limit "
        "(1-200, default 50) and offset. Administrator-only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_teachers(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[TeacherRead]:
    return admin_user_service.list_teachers(session, limit=limit, offset=offset)


@router.post(
    "/teachers/{user_id}/invite",
    tags=[TAG_ADMIN_TEACHERS],
    response_model=TeacherRead,
    summary="Re-send a teacher invitation (administrative)",
    description=(
        "Mints a fresh single-use invitation token and emails it; the "
        "previous link stops working. Only an account still awaiting "
        "acceptance can be re-invited (otherwise 409). 404 unknown id."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id"},
        409: {"description": "Account is not awaiting invitation"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def resend_invite(
    request: Request,
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TeacherRead:
    try:
        updated = admin_user_service.resend_invite(session, user_id, actor=admin)
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return updated


@router.post(
    "/teachers/{user_id}/activate",
    tags=[TAG_ADMIN_TEACHERS],
    response_model=TeacherRead,
    summary="Activate a teacher account (administrative)",
    description=(
        "pending/suspended → active: the account may now log in. 409 when "
        "already active; 404 unknown id. Administrator-only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id"},
        409: {"description": "Account is already active"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def activate_teacher(
    request: Request,
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TeacherRead:
    try:
        updated = admin_user_service.activate_teacher(session, user_id, actor=admin)
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return updated


@router.post(
    "/teachers/{user_id}/deactivate",
    tags=[TAG_ADMIN_TEACHERS],
    response_model=TeacherRead,
    summary="Deactivate a teacher account (administrative)",
    description=(
        "→ suspended, revoking every refresh session so no outstanding "
        "token survives the decision. 409 when already suspended; 404 "
        "unknown id. Administrator-only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id"},
        409: {"description": "Account is already suspended"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def deactivate_teacher(
    request: Request,
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TeacherRead:
    try:
        updated = admin_user_service.deactivate_teacher(session, user_id, actor=admin)
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return updated


@router.patch(
    "/users/{user_id}/role",
    tags=[TAG_ADMIN_USERS],
    response_model=AdminUserRead,
    summary="Change an account's role (administrative)",
    description=(
        "Moves an account between student / teacher / admin. Every session "
        "of the target is revoked, so an old access/refresh token cannot "
        "keep the previous privileges. An administrator cannot change "
        "their own role (403). 404 unknown id; 409 when the role is "
        "unchanged. Administrator-only — this is the sole endpoint that "
        "accepts a role from the client."
    ),
    responses={
        200: {"description": "Role changed, sessions revoked"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not an administrator, or self-change refused"},
        404: {"description": "Unknown user id"},
        409: {"description": "Role already has that value"},
        422: {"description": "Role outside the three-value vocabulary"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def change_role(
    request: Request,
    user_id: uuid.UUID,
    payload: RoleChangeRequest,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminUserRead:
    try:
        updated = admin_user_service.change_user_role(
            session, user_id, role=payload.role.value, actor=admin
        )
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return updated


@router.post(
    "/users/{user_id}/unlock",
    tags=[TAG_ADMIN_USERS],
    response_model=AdminUserRead,
    summary="Clear a login lockout (administrative)",
    description=(
        "After LOGIN_LOCKOUT_THRESHOLD refused login attempts the account "
        "is locked for LOGIN_LOCKOUT_MINUTES minutes (slice 7). This "
        "endpoint clears the lock early: the failed-attempt counter resets "
        "and the account can authenticate again immediately. 409 when the "
        "account is not locked; 404 unknown id. Administrator-only — "
        "students and teachers get 403 before any logic runs."
    ),
    responses={
        200: {"description": "Lockout cleared"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id"},
        409: {"description": "Account is not locked"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def unlock_user(
    request: Request,
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminUserRead:
    try:
        updated = admin_user_service.unlock_user(session, user_id, actor=admin)
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return updated
