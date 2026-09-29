"""Authentication API (Phase 5G).

Role-split routers — every endpoint lives under exactly one guard:

- ``router`` (prefix ``/auth``, tag ``Authentication``) — public/token ops:
  ``POST /auth/register`` (student account creation), ``POST /auth/login``,
  ``POST /auth/refresh``, ``POST /auth/logout``,
  ``POST /auth/forgot-password``, ``POST /auth/reset-password``, plus the
  authenticated ``GET /auth/me`` (same payload as ``GET /me``, kept under
  the auth namespace),
- ``me_router`` (prefix ``/me``, tag ``Self-Service``) — any authenticated
  role: ``GET /me`` (``student`` is ``null`` when the profile is missing),
  ``POST /me/change-password``, ``POST /me/deactivate``,
- ``student_me_router`` (prefix ``/me/student``, tag
  ``Student Self-Service``) — role=student only:
  ``GET /me/student`` (403 + remedy pointer when no profile),
  ``POST /me/student`` (self-service profile attach),
  ``PATCH /me/student`` (self-service profile update).

Administration endpoints live under ``/admin/*`` (``admin_students`` /
``registrations`` modules, tag ``Administration``) and never mix with the
self-service namespace.

Endpoint bodies are deliberately thin: validate schema → delegate to the
auth service → map the service error convention onto HTTP statuses →
commit on success. No JWT, hashing or session logic lives here.

Rate limiting: login, register, refresh, profile writes and other ``/me``
writes are throttled via slowapi to prevent brute-force and account-spam
attacks.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session

from app.core.auth_dependencies import get_current_student, get_current_user, require_student
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.student import Student
from app.models.user import User
from app.schemas.auth import (
    AuthUserRead,
    ChangePasswordRequest,
    DeactivateAccountRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshTokenRequest,
    ResetPasswordRequest,
    StudentAccountCreate,
    TokenResponse,
)
from app.schemas.student_profile import (
    StudentProfileRead,
    StudentProfileSelfCreate,
    StudentProfileUpdate,
)
from app.services import auth_service
from app.services import student_service
from app.services.student_service import read_profile

router = APIRouter(prefix="/auth", tags=["Authentication"])
me_router = APIRouter(prefix="/me", tags=["Self-Service"])
student_me_router = APIRouter(prefix="/me/student", tags=["Student Self-Service"])

# Advertised in OpenAPI so protected endpoints document the Bearer scheme
# (Step 37). auto_error=False keeps our own 401 contract for missing headers.
_bearer = HTTPBearer(auto_error=False, description="JWT access token")

# The generic 401 the whole auth contract uses (Step 27) — identical for
# missing, malformed, expired and revoked credentials.
_UNAUTHORIZED = {
    401: {"description": "Missing, invalid or expired credentials"},
}
_FORBIDDEN = {403: {"description": "Authenticated but not authorized"}}

_settings = get_settings()


def _http_error(exc: auth_service.AuthError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a student account (user + student profile)",
    description=(
        "Creates the user identity (role fixed to 'student' — callers "
        "cannot choose any other role) and the student profile atomically, "
        "then issues an access + refresh token pair. Password policy: "
        "minimum length (see PASSWORD_MIN_LENGTH), never logged or stored "
        "in plaintext (Argon2id). Duplicate email → 409. Policy violation "
        "→ 422. The response contains tokens only — no password material."
    ),
    responses={
        201: {"description": "Account created and tokens issued"},
        409: {"description": "A user with this email already exists"},
        422: {"description": "Validation error (password policy, profile fields)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTER)
def register_student_account(
    request: Request,
    payload: StudentAccountCreate,
    session: Session = Depends(get_db),
) -> TokenResponse:
    try:
        _, tokens = auth_service.register_student_account(session, payload)
    except auth_service.AuthError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return tokens


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in with email and password",
    description=(
        "Verifies the credentials and issues an access + refresh token "
        "pair. Failures return one generic 401 message — unknown email, "
        "wrong password and non-active accounts are deliberately "
        "indistinguishable. Suspended/disabled accounts cannot log in."
    ),
    responses={
        200: {"description": "Token pair issued"},
        401: {"description": "Invalid credentials (generic; never reveals account state)"},
        422: {"description": "Malformed request"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_LOGIN)
def login(
    request: Request,
    payload: LoginRequest,
    session: Session = Depends(get_db),
) -> TokenResponse:
    try:
        tokens = auth_service.login(session, payload)
    except auth_service.AuthError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return tokens


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new token pair",
    description=(
        "Validates the refresh token against its server-side session "
        "(existence, revocation, expiry, account status) and issues a new "
        "pair. The presented session is revoked and replaced (rotation): a "
        "replayed refresh token is refused on its second use. Revoked or "
        "expired sessions can never refresh."
    ),
    responses={
        200: {"description": "New token pair issued"},
        401: {"description": "Invalid, revoked or expired refresh token"},
        422: {"description": "Malformed request"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REFRESH)
def refresh(
    request: Request,
    payload: RefreshTokenRequest,
    session: Session = Depends(get_db),
) -> TokenResponse:
    try:
        tokens = auth_service.refresh(session, payload.refresh_token)
    except auth_service.AuthError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return tokens


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out (revoke the presented refresh session)",
    description=(
        "Requires a valid access token AND the refresh token to revoke; "
        "revokes that session server-side. Idempotent — a revoked session "
        "can never refresh again. (Access tokens simply expire; revocation "
        "is enforced on the refresh path and by the account-status check "
        "in the identity dependency.)"
    ),
    responses={
        204: {"description": "Session revoked"},
        401: {"description": "Missing/invalid credentials or refresh token"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REFRESH)
def logout(
    request: Request,
    payload: RefreshTokenRequest,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    try:
        auth_service.logout(session, payload.refresh_token, expected_user_id=user.id)
    except auth_service.AuthError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()


@router.get(
    "/me",
    response_model=AuthUserRead,
    summary="Current authenticated identity",
    description=(
        "Safe information about the caller: user id, email, role (one of "
        "'student', 'teacher', 'admin'), status and the linked student "
        "profile summary when applicable. The identity is derived entirely "
        "from the verified Bearer token and the database — never from a "
        "user_id, student_id or role supplied by the client (such "
        "parameters are simply not accepted). Missing, malformed, expired "
        "or revoked credentials → 401. No password hash, no secrets."
    ),
    responses=_UNAUTHORIZED,
)
@limiter.limit(_settings.RATE_LIMIT_ME_READ)
def read_auth_me(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> AuthUserRead:
    return auth_service.identity_for_user(session, user)


@me_router.get(
    "",
    response_model=AuthUserRead,
    summary="Current authenticated identity",
    description=(
        "Safe information about the caller: user id, email, role, status "
        "and the linked student profile summary when applicable. Derived "
        "entirely from the Bearer token — never from caller-supplied ids. "
        "No password hash, no secrets, no database internals."
    ),
    responses=_UNAUTHORIZED,
)
@limiter.limit(_settings.RATE_LIMIT_ME_READ)
def read_me(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> AuthUserRead:
    return auth_service.identity_for_user(session, user)


@student_me_router.get(
    "",
    response_model=StudentProfileRead,
    summary="Current authenticated student profile",
    description=(
        "The authenticated student's own profile. The identity comes from "
        "the access token (role must be 'student' and the profile must "
        "exist); a student_id parameter is neither accepted nor needed. "
        "Non-students → 403."
    ),
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_me_student(
    request: Request,
    student: Student = Depends(get_current_student),
    session: Session = Depends(get_db),
) -> StudentProfileRead:
    user = session.get(User, student.user_id)
    return read_profile(user, student)


@student_me_router.post(
    "",
    response_model=StudentProfileRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create the authenticated student's own profile",
    description=(
        "Self-service profile creation for an authenticated student whose "
        "profile does not exist yet — the counterpart of the administrative "
        "POST /admin/students (which mints the account too). The identity comes "
        "from the token; only profile fields (full_name, date_of_birth, "
        "gender, country) are accepted — email/phone stay whatever the "
        "account already has and are rejected if supplied (422). "
        "Role must be 'student' (403); if a profile already exists the "
        "response is 409. The creation is recorded in the profile audit "
        "trail, attributed to the account itself."
    ),
    responses={
        201: {"description": "Profile created for the authenticated account"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        409: {"description": "A student profile already exists for this user"},
        422: {"description": "Validation error (DOB, fields, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def create_me_student(
    request: Request,
    payload: StudentProfileSelfCreate,
    session: Session = Depends(get_db),
    user: User = Depends(require_student),
) -> StudentProfileRead:
    try:
        profile = student_service.create_profile_for_existing_user(
            session, user, payload
        )
    except student_service.ProfileError as exc:
        session.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    session.commit()
    return profile


@student_me_router.patch(
    "",
    response_model=StudentProfileRead,
    summary="Update the authenticated student's own profile",
    description=(
        "Student self-service profile update (full_name / gender / "
        "country) — the preferred form of the legacy ID-based PATCH. "
        "The target profile is the caller's own, derived from the token; "
        "only supplied fields change, an explicit null clears gender or "
        "country (full_name cannot be cleared); email, phone and "
        "date_of_birth remain immutable identity anchors. Every change is "
        "audit-logged with the acting user."
    ),
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        422: {"description": "Validation error"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def patch_me_student(
    request: Request,
    payload: StudentProfileUpdate,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> StudentProfileRead:
    # Only supplied fields change (profile_update_kwargs reads
    # model_fields_set, which distinguishes omission from an explicit null,
    # which clears a nullable column). Shared with PATCH /admin/students/{id}.
    try:
        profile = student_service.update_student_profile(
            session,
            student.id,
            changed_by=student.user_id,
            **student_service.profile_update_kwargs(payload),
        )
    except student_service.ProfileError as exc:
        session.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    session.commit()
    return profile


# --- Password change (authenticated self-service) --------------------------------


@me_router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the authenticated user's password",
    description=(
        "Requires the current password for verification. The new password "
        "must meet the password policy (minimum length). The caller's "
        "existing sessions remain valid."
    ),
    responses={
        **_UNAUTHORIZED,
        401: {"description": "Current password is incorrect"},
        422: {"description": "New password violates policy"},
    },
)
@limiter.limit("10/minute")
def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    try:
        auth_service.change_password(session, user, payload)
    except auth_service.AuthError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()


# --- Password reset (public, email-based) ----------------------------------------


@router.post(
    "/forgot-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Request a password reset email",
    description=(
        "Sends a password reset email to the given address if an active "
        "account exists. Always returns 204 regardless of whether the email "
        "exists (prevents email enumeration). Rate-limited."
    ),
    responses={
        204: {"description": "Email sent (or address not found — no disclosure)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit("5/minute")
def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    session: Session = Depends(get_db),
) -> None:
    auth_service.forgot_password(session, payload.email)
    session.commit()


@router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset password using a valid reset token",
    description=(
        "Accepts a reset token (from the email link) and a new password. "
        "The token is single-use and expires after 1 hour. All existing "
        "sessions for the user are revoked on success."
    ),
    responses={
        204: {"description": "Password reset successfully"},
        401: {"description": "Invalid, expired or already-used token"},
        422: {"description": "New password violates policy"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit("5/minute")
def reset_password(
    request: Request,
    payload: ResetPasswordRequest,
    session: Session = Depends(get_db),
) -> None:
    try:
        auth_service.reset_password(session, payload.token, payload.new_password)
    except auth_service.AuthError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()


# --- Account deactivation (self-service) -----------------------------------------


@me_router.post(
    "/deactivate",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate the authenticated user's account",
    description=(
        "Requires password confirmation. Sets the account status to "
        "'disabled' and revokes all active sessions. The account can be "
        "re-enabled by an administrator."
    ),
    responses={
        **_UNAUTHORIZED,
        401: {"description": "Password is incorrect"},
    },
)
@limiter.limit("5/minute")
def deactivate_account(
    request: Request,
    payload: DeactivateAccountRequest,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    try:
        auth_service.deactivate_account(session, user, payload)
    except auth_service.AuthError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
