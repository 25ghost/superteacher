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
- ``PATCH /admin/teachers/{user_id}/school`` — assign or clear the
  teacher's school (404 unknown teacher *or* school; the audit event is
  written even when the value does not change).
- ``GET /admin/users`` — list every account (paginated ``Page[T]``
  envelope) with role/status filters and an email search.
- ``GET /admin/users/{user_id}`` — one account with its profiles, live
  session count and newest audit events (404 unknown id).
- ``POST /admin/users/{user_id}/deactivate`` — any role → suspended,
  revoking every refresh session; self, last-admin and
  already-deactivated (suspended or disabled) targets → 409.
- ``POST /admin/users/{user_id}/activate`` — suspended → active; a
  teacher that has never accepted its invitation is refused (the
  invitation flow owns that).
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
from app.schemas.pagination import Page
from app.schemas.teacher_admin import (
    AdminUserDetailRead,
    AdminUserListRead,
    AdminUserRead,
    RoleChangeRequest,
    TeacherCreate,
    TeacherRead,
    TeacherSchoolUpdate,
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


@router.get(
    "/users",
    tags=[TAG_ADMIN_USERS],
    response_model=Page[AdminUserListRead],
    summary="List user accounts (administrative)",
    description=(
        "One page of every account in an items/total/limit/offset "
        "envelope. limit defaults to 20 (max 100), offset defaults to 0; "
        "both are 422 outside their range. ?role= and ?status= filter on "
        "the account vocabularies (an unknown value is 422, never a "
        "silently empty page) and ?q= case-insensitively matches the "
        "email, treating %, _ and \\ as literal characters. Ordering is "
        "deterministic (created_at desc, id) so offset paging stays "
        "stable. One count query plus one page query, whatever the page "
        "size. Administrator-only."
    ),
    responses={
        200: {"description": "One page of user accounts"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        422: {"description": "Invalid query parameters (limit/offset/role/status)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_users(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    role: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=255),
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Page[AdminUserListRead]:
    # Read path: nothing to commit; errors are translated, never swallowed.
    try:
        return admin_user_service.list_users(
            session, limit=limit, offset=offset, role=role, status=status, q=q
        )
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    except Exception:
        session.rollback()
        raise


@router.get(
    "/users/{user_id}",
    tags=[TAG_ADMIN_USERS],
    response_model=AdminUserDetailRead,
    summary="Retrieve one user account with its profiles and recent activity (administrative)",
    description=(
        "The list fields for one account plus its student profile id (when "
        "the account has a student profile), its teacher profile id and "
        "assigned school (id, code and name), the number of refresh "
        "sessions that can still authenticate, and the newest 10 audit "
        "events for the account (event type, timestamp, acting "
        "administrator's email, ip address). Three bounded queries "
        "whatever the account's data, so the cost never grows with row "
        "count. No password hash, no token digests, no event metadata "
        "blobs. 404 unknown id. Administrator-only."
    ),
    responses={
        200: {"description": "The account, its profiles and recent audit events"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def get_user(
    request: Request,
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> AdminUserDetailRead:
    # Read path: nothing to commit; errors are translated, never swallowed.
    try:
        return admin_user_service.get_user_detail(session, user_id)
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    except Exception:
        session.rollback()
        raise


@router.post(
    "/users/{user_id}/deactivate",
    tags=[TAG_ADMIN_USERS],
    response_model=AdminUserListRead,
    summary="Deactivate any user account, revoking its sessions (administrative)",
    description=(
        "Any role and any current status (except an already-deactivated "
        "account: suspended by an administrator or disabled by the "
        "account's own request) → suspended, with every refresh session "
        "revoked so live tokens stop authenticating immediately; audited "
        "as user_deactivated with the number of revoked sessions. 409 "
        "when the account is already suspended or disabled, when an "
        "administrator tries to deactivate their own account, and for the "
        "last active administrator — two administrators deactivating each "
        "other at the same moment contend on one row lock, so exactly one "
        "commit survives. 404 unknown id. Administrator-only."
    ),
    responses={
        200: {"description": "The account is now suspended"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id"},
        409: {"description": "Already suspended/disabled / self / last active administrator"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def deactivate_user(
    request: Request,
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminUserListRead:
    try:
        updated = admin_user_service.deactivate_user(session, user_id, actor=admin)
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return updated


@router.post(
    "/users/{user_id}/activate",
    tags=[TAG_ADMIN_USERS],
    response_model=AdminUserListRead,
    summary="Reactivate a suspended user account (administrative)",
    description=(
        "suspended/pending → active, audited as user_activated. 409 when "
        "the account is already active, and for a teacher that has never "
        "accepted its invitation (users.password_hash IS NULL, whatever "
        "the status): teachers are activated by accepting their "
        "invitation, so the generic route refuses to bypass it (use "
        "POST /admin/teachers/{user_id}/invite or "
        "POST /auth/accept-invite instead). 404 unknown id. "
        "Administrator-only."
    ),
    responses={
        200: {"description": "The account is now active"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id"},
        409: {"description": "Already active / never-accepted teacher (invitation flow)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def activate_user(
    request: Request,
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminUserListRead:
    try:
        updated = admin_user_service.activate_user(session, user_id, actor=admin)
    except AuthError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return updated


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
        "token survives the decision. 409 when the account is already "
        "deactivated (suspended or disabled); 404 unknown id. "
        "Administrator-only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id"},
        409: {"description": "Account is already deactivated (suspended/disabled)"},
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
    "/teachers/{user_id}/school",
    tags=[TAG_ADMIN_TEACHERS],
    response_model=TeacherRead,
    summary="Assign or clear a teacher's school (administrative)",
    description=(
        "Body {school_id}: a UUID assigns that school, an explicit null "
        "clears the assignment (the field is required, so an empty body "
        "cannot clear anything: 422). Unknown teacher id → 404; unknown "
        "school id → 404, checked before the write so a bogus foreign "
        "key is a domain error, never a database error; extra fields → "
        "422. Assigning the school a teacher already has still succeeds "
        "and still writes the teacher_school_changed audit event — the "
        "trail records the administrative action, not just the delta. "
        "The response is the teacher with school_id, school_code and "
        "school_name resolved. Administrator-only."
    ),
    responses={
        200: {"description": "The teacher account with its school resolved"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown user id or unknown school id"},
        422: {"description": "Missing/unknown fields in the body"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def assign_teacher_school(
    request: Request,
    user_id: uuid.UUID,
    payload: TeacherSchoolUpdate,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TeacherRead:
    try:
        updated = admin_user_service.set_teacher_school(
            session, user_id, payload, actor=admin
        )
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
