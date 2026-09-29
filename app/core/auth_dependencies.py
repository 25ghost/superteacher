"""FastAPI identity dependencies (Phase 5G, Steps 11, 12, 18).

The authorization chain enforced for every protected endpoint::

    Authorization: Bearer <access token>
        ↓ decode + validate (signature, expiry, type)
        ↓ user id (sub claim)
        ↓ load user from the database
        ↓ verify current active status          ← token claims are never trusted
        ↓ current user
        ↓ (student endpoints) role + linked profile
        ↓ current student

The database — not the JWT — is the authority for role and status (Step
30): a role change or suspension takes effect as soon as the token is next
presented. No endpoint may accept a ``user_id``/``student_id`` query or
body parameter as identity for authenticated operations.

Error contract (Step 27): missing/invalid/expired credentials → 401 with a
generic message (JWT internals are never exposed); authenticated but
unauthorized (role/profile/ownership) → 403.
"""
from __future__ import annotations

import uuid

import jwt as pyjwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_token
from app.models.enums import UserRole, UserStatus
from app.models.student import Student
from app.models.user import User
from app.services import auth_service

# auto_error=False so a *missing* header produces our own 401 contract
# instead of FastAPI's default 403.
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

_CREDENTIALS_DETAIL = "Not authenticated"


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: Session = Depends(get_db),
) -> User:
    """Resolve the authenticated user from the Bearer access token (Step 11)."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_CREDENTIALS_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_token(credentials.credentials, expected_type="access")
    except pyjwt.PyJWTError:
        # Generic 401: signature/expiry/format internals are never exposed.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_CREDENTIALS_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_CREDENTIALS_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    user = auth_service.get_user_by_id(session, user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        # Unknown, suspended or disabled accounts cannot authenticate
        # (Step 29). The generic detail avoids revealing account state.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_CREDENTIALS_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_current_student(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> Student:
    """The authenticated user's student profile (Step 12).

    Role must be ``student`` and the linked profile must exist — a missing
    profile is never created implicitly by a read; the 403 detail points
    to the explicit self-service creation endpoint (``POST /me/student``).
    """
    try:
        return auth_service.resolve_current_student(session, user)
    except auth_service.AuthForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc


# --- role authorization (Step 18) ---------------------------------------------------

#: The complete role vocabulary (see ``app.models.enums.UserRole``).
ALLOWED_ROLES: frozenset[str] = frozenset(role.value for role in UserRole)


def require_role(*roles: UserRole, detail: str | None = None):
    """Build a dependency that demands one of the given roles (403 otherwise).

    One or more roles may be given — the caller passes exactly the roles
    the endpoint allows::

        Depends(require_role(UserRole.TEACHER))                     # teacher-only
        Depends(require_role(UserRole.ADMIN))                       # admin-only
        Depends(require_role(UserRole.TEACHER, UserRole.ADMIN))     # teacher/admin

    The user is first resolved via :func:`get_current_user`, so role checks
    always operate on the database state of the authenticated account —
    never on a claim, header or body value supplied by the client.
    Missing/invalid/expired credentials therefore surface as 401 from
    ``get_current_user``; an authenticated caller without one of the
    allowed roles gets 403. ``detail`` overrides the generic 403 message
    so role-split endpoints can name the required role.
    """
    if not roles:
        raise ValueError("require_role() needs at least one role")
    for role in roles:
        if not isinstance(role, UserRole):
            raise ValueError(f"role must be a UserRole member, got {role!r}")
    unknown = {role.value for role in roles} - ALLOWED_ROLES
    if unknown:
        raise ValueError(f"unknown role(s): {sorted(unknown)}")
    message = detail or "insufficient role for this operation"

    def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in {role.value for role in roles}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=message,
            )
        return user

    return _dependency


#: One reusable guard per allowed role.
require_student = require_role(UserRole.STUDENT)
require_teacher = require_role(UserRole.TEACHER)
#: Administrators (the ``/admin/*`` routes); the 403 message names the role.
require_admin = require_role(
    UserRole.ADMIN,
    detail="administrator role required for this operation",
)
