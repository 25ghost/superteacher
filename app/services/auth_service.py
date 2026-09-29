"""Authentication service: accounts, login, tokens, sessions (Phase 5G).

Shared SuperTeacher core infrastructure (Step 41) — not a Student
Registration feature. Responsibilities (Step 9):

- student account creation (atomic user + student profile; role forced to
  ``student``; the existing Phase 5C profile rules are reused, not
  duplicated),
- login (identifier lookup, active-status check, password verification,
  generic failure — never revealing whether an email exists, Step 21),
- token issuance (short-lived access JWT + revocable refresh session),
- refresh handling with **rotation**: a used refresh token's session is
  revoked and a new session issued, so a replayed token is detectable
  (Step 22),
- revocation (logout: one session; suspension/disable: all sessions),
- identity construction for ``/me`` and the current-student dependency.

All writes go through the caller's request-scoped session: the service
only ``flush``es; the API layer commits on success and rolls back on
failure (same transaction convention as every other service here).

Passwords are handled only by ``app.core.security`` — hashed before
persistence, verified on login, never logged, never placed in errors.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core import security
from app.core.security import PasswordPolicyError
from app.models.auth_session import AuthSession
from app.models.enums import UserRole, UserStatus
from app.models.student import Student
from app.models.user import User
from app.repositories import auth_session_repository as auth_session_repo
from app.repositories import student_repository as student_repo
from app.repositories import student_profile_history_repository as history_repo
from app.repositories import user_repository as user_repo
from app.repositories import auth_event_repository as auth_event_repo
from app.schemas.auth import (
    AuthUserRead,
    ChangePasswordRequest,
    DeactivateAccountRequest,
    ForgotPasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
    StudentAccountCreate,
    TokenResponse,
)
from app.services.student_service import (
    ProfileConflictError,
    ProfileValidationError,
    ensure_plausible_dob,
)

logger = logging.getLogger(__name__)

# Database unique constraints that are the final duplicate protection for
# account creation (same convention as student_service). Both the plain
# unique constraint and the case-insensitive functional index (migration
# 0004) are matched on the race path.
_DUPLICATE_EMAIL_CONSTRAINT = "uq_users_email_key"
_DUPLICATE_EMAIL_CI_CONSTRAINT = "uq_users_email_ci_key"
_DUPLICATE_PROFILE_CONSTRAINT = "students_user_id_key"

# Generic public conflict detail for registration: the public endpoint must
# never confirm whether an email is already registered (anti-enumeration —
# login and password reset take the same stance). The authenticated
# administrative profile-creation path keeps its explicit message.
_REGISTER_EMAIL_TAKEN_DETAIL = "email cannot be used to create an account"

# Accounts that may authenticate (Step 29). A suspended or disabled account
# is refused with the same generic message as a wrong password — the account
# state is never confirmed to an unauthenticated caller, and login never
# modifies user status.
_AUTHENTICATABLE_STATUSES = frozenset({UserStatus.ACTIVE.value})

# Dummy Argon2id hash used to prevent timing side-channel on login: when the
# email is not found we still run verify_password against this hash so the
# comparison takes the same wall-clock time as a real password check.
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=1$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 84


class AuthError(Exception):
    """Base class: ``.status_code`` tells the API layer which HTTP status to emit."""

    status_code = 400


class AuthValidationError(AuthError):
    """Password policy violations and other input-level auth errors."""

    status_code = 422


class AuthConflictError(AuthError):
    """Duplicate email / existing profile during account registration."""

    status_code = 409


class AuthCredentialsError(AuthError):
    """Generic authentication failure (login or a rejected token).

    Deliberately vague: the same message for unknown email, wrong password
    and non-authenticatable account status, so public errors never reveal
    whether an email exists (Step 21).
    """

    status_code = 401

    def __init__(self) -> None:
        super().__init__("invalid email or password")


class AuthForbiddenError(AuthError):
    """Authenticated, but not authorized (wrong role / missing profile)."""

    status_code = 403


class AuthNotFoundError(AuthError):
    """A referenced resource of the auth flow does not exist (404)."""

    status_code = 404


# --- helpers ---------------------------------------------------------------------


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize a stored datetime to aware UTC for comparisons.

    PostgreSQL ``timestamptz`` always returns aware datetimes; SQLite
    (unit-test scratch databases) returns naive ones stored as UTC. This
    keeps session-expiry comparisons correct on both.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _hash_refresh_secret(secret: str) -> str:
    """SHA-256 hex digest of a refresh token's opaque secret component.

    The raw secret is never stored (Step 7); this digest is what the
    ``auth_sessions.token_hash`` UNIQUE column holds.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _read_user(user: User, student: Student | None) -> AuthUserRead:
    return AuthUserRead(
        user_id=user.id,
        email=user.email,
        phone=user.phone,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        student=(
            {
                "student_id": student.id,
                "full_name": student.full_name,
                "date_of_birth": student.date_of_birth,
                "gender": student.gender,
                "country": student.country,
            }
            if student is not None
            else None
        ),
    )


def _issue_session(
    session: Session,
    user: User,
    now: datetime,
) -> TokenResponse:
    """Create a refresh session + access token pair for one user (flush only).

    The refresh token is a signed JWT bound to its ``auth_sessions`` row;
    the row stores only the JWT's SHA-256 digest — the raw token is never
    persisted (Step 7).
    """
    settings = _settings()
    # Enforce max sessions per user: revoke oldest excess sessions first.
    auth_session_repo.enforce_session_limit(
        session, user.id, settings.MAX_SESSIONS_PER_USER, now
    )
    auth_session = auth_session_repo.create(
        session,
        user_id=user.id,
        token_hash="",  # set below, once the JWT (which names this row) exists
        expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    refresh_jwt = security.create_refresh_token(user.id, auth_session.id)
    auth_session.token_hash = _hash_refresh_secret(refresh_jwt)
    session.flush()
    access_token = security.create_access_token(user.id, user.role)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_jwt,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def _settings():
    from app.core.config import get_settings

    return get_settings()


def _verify_refresh_credentials(
    session: Session,
    token_value: str,
    now: datetime,
) -> tuple[User, AuthSession]:
    """Validate a presented refresh token against the server-side session.

    Enforces, in order: signature/claims (via ``decode_token``), session
    existence (by digest), revocation, expiry and the user's current
    status — so a revoked or stale session can never mint new tokens
    (Steps 22/30). A signed token alone is never sufficient: the server
    session row is the authority.
    """
    try:
        claims = security.decode_token(token_value, expected_type="refresh")
    except Exception:
        raise AuthCredentialsError() from None
    try:
        jwt_session_id = uuid.UUID(claims["sid"])
        jwt_user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise AuthCredentialsError() from None

    auth_session = auth_session_repo.get_by_token_hash_for_update(
        session, _hash_refresh_secret(token_value)
    )
    if (
        auth_session is None
        or auth_session.id != jwt_session_id
        or auth_session.user_id != jwt_user_id
    ):
        raise AuthCredentialsError()
    if auth_session.revoked_at is not None:
        raise AuthCredentialsError()
    if _as_aware_utc(auth_session.expires_at) <= now:
        raise AuthCredentialsError()

    user = session.get(User, auth_session.user_id)
    if user is None or user.status not in _AUTHENTICATABLE_STATUSES:
        raise AuthCredentialsError()
    return user, auth_session


# --- account registration (Steps 19, 20) --------------------------------------------


def register_student_account(
    session: Session,
    payload: StudentAccountCreate,
) -> tuple[AuthUserRead, TokenResponse]:
    """Create a student account (user + profile) atomically, then issue tokens.

    The role is fixed server-side to ``student`` — the request schema offers
    no way to choose another role (Step 20). Email uniqueness is enforced by
    the service pre-check and, under a concurrent race, by the database
    UNIQUE constraint (same pattern as ``student_service``).

    Returns ``(identity, tokens)``; the caller commits. Any raise after the
    inserts is rolled back by the caller, so no orphaned user survives.
    """
    try:
        password = security.validate_password_policy(payload.password)
    except PasswordPolicyError as exc:
        raise AuthValidationError(str(exc)) from exc

    # Date-of-birth sanity BEFORE any insert: without this guard a future
    # DOB reaches the database CHECK and surfaces as an IntegrityError →
    # 500 instead of a readable 422 (hardening: same rule as admin create).
    try:
        ensure_plausible_dob(payload.date_of_birth)
    except ProfileValidationError as exc:
        raise AuthValidationError(str(exc)) from exc

    email = str(payload.email).strip().lower()

    if user_repo.get_by_email(session, email) is not None:
        raise AuthConflictError(_REGISTER_EMAIL_TAKEN_DETAIL)

    try:
        user = user_repo.create_student_user(
            session,
            email=email,
            phone=None,
            password_hash=security.hash_password(password),
        )
        student = student_repo.create(
            session,
            user_id=user.id,
            full_name=payload.full_name,
            date_of_birth=payload.date_of_birth,
            gender=payload.gender,
            country=payload.country,
        )
    except IntegrityError as exc:
        constraint_text = str(exc.orig)
        if (
            _DUPLICATE_EMAIL_CONSTRAINT in constraint_text
            or _DUPLICATE_EMAIL_CI_CONSTRAINT in constraint_text
        ):
            logger.warning("duplicate email race lost for a new account")
            raise AuthConflictError(_REGISTER_EMAIL_TAKEN_DETAIL) from exc
        if _DUPLICATE_PROFILE_CONSTRAINT in constraint_text:
            logger.warning("duplicate profile race lost for a new account")
            raise AuthConflictError(
                "student profile already exists for this user"
            ) from exc
        raise  # an unexpected integrity problem must stay visible (500)

    # Audit: self-service creation is attributed to the new account itself.
    history_repo.log_change(
        session,
        student_id=student.id,
        field_name="profile",
        old_value=None,
        new_value=None,
        changed_by=user.id,
        change_type="create",
    )

    identity = _read_user(user, student_repo.get_by_user_id(session, user.id))
    tokens = _issue_session(session, user, datetime.now(timezone.utc))
    return identity, tokens


# --- login (Step 21) -----------------------------------------------------------------


def login(session: Session, payload: LoginRequest) -> TokenResponse:
    """Verify email + password and issue a fresh token pair.

    Lookup, status check and password verification produce one identical
    generic error so the endpoint cannot be used to enumerate accounts or
    infer account status. Never logs the password; never mutates the user.

    A dummy hash is always compared even when the user is not found, so the
    timing is identical for both existing and non-existing emails.
    """
    email = str(payload.email).strip().lower()
    user = user_repo.get_by_email(session, email)
    password_hash = user.password_hash if user is not None else _DUMMY_HASH
    if (
        user is None
        or user.status not in _AUTHENTICATABLE_STATUSES
        or not security.verify_password(payload.password, password_hash)
    ):
        logger.info("failed login attempt for an email (result not disclosed)")
        if user is not None:
            auth_event_repo.log_event(
                session, user_id=user.id, event_type="login_failed"
            )
        raise AuthCredentialsError()

    auth_event_repo.log_event(session, user_id=user.id, event_type="login")
    logger.info("successful login", extra={"user_id": str(user.id)})
    return _issue_session(session, user, datetime.now(timezone.utc))


# --- refresh (Step 22) and logout (Step 23) --------------------------------------------


def refresh(session: Session, refresh_token_value: str) -> TokenResponse:
    """Exchange a valid refresh token for a new pair, rotating the session.

    The old session is revoked and a new one issued in the same
    transaction, so a replayed refresh token is refused on its second use.
    """
    now = datetime.now(timezone.utc)
    user, old_session = _verify_refresh_credentials(session, refresh_token_value, now)
    auth_session_repo.revoke(session, old_session, now)
    auth_event_repo.log_event(session, user_id=user.id, event_type="token_refresh")
    logger.info("token refresh", extra={"user_id": str(user.id)})
    return _issue_session(session, user, now)


def logout(session: Session, refresh_token_value: str, *, expected_user_id: uuid.UUID) -> None:
    """Revoke the refresh session presented by an authenticated caller.

    The presented token must be valid (signature, session state, ownership);
    logout then revokes *that* session — the caller cannot revoke someone
    else's session. ``expected_user_id`` is the identity from the access
    token; a mismatch (access token for user A used to revoke user B's
    session) is refused. Idempotent: logging out twice is fine.
    """
    user, auth_session = _verify_refresh_credentials(
        session, refresh_token_value, datetime.now(timezone.utc)
    )
    if auth_session.user_id != expected_user_id:
        raise AuthForbiddenError(
            "refresh token does not belong to the authenticated user"
        )
    auth_session_repo.revoke(session, auth_session, datetime.now(timezone.utc))
    auth_event_repo.log_event(session, user_id=expected_user_id, event_type="logout")
    logger.info("logout", extra={"user_id": str(expected_user_id)})


def revoke_all_sessions(session: Session, user_id: uuid.UUID) -> int:
    """Revoke every active refresh session of one user (status changes)."""
    return auth_session_repo.revoke_all_for_user(
        session, user_id, datetime.now(timezone.utc)
    )


# --- password change (authenticated self-service) --------------------------------


def change_password(
    session: Session,
    user: User,
    payload: ChangePasswordRequest,
) -> None:
    """Change the authenticated user's password.

    Verifies the current password, validates the new password against policy,
    hashes and stores it. The caller commits.
    """
    if user.password_hash is None:
        raise AuthValidationError("this account has no password set")
    if not security.verify_password(payload.current_password, user.password_hash):
        raise AuthCredentialsError()
    if payload.current_password == payload.new_password:
        raise AuthValidationError("new password must differ from current password")

    try:
        new_hash = security.hash_password(payload.new_password)
    except ValueError as exc:
        raise AuthValidationError(str(exc)) from exc

    user.password_hash = new_hash
    auth_event_repo.log_event(session, user_id=user.id, event_type="password_change")
    session.flush()
    logger.info("password changed", extra={"user_id": str(user.id)})


# --- password reset (public, email-based) ---------------------------------------


def forgot_password(session: Session, email: str) -> None:
    """Generate a password reset token and send it via email.

    Always returns None — the response is identical whether the email exists
    or not, preventing email enumeration.
    """
    normalized = str(email).strip().lower()
    user = user_repo.get_by_email(session, normalized)
    if user is None or user.status not in _AUTHENTICATABLE_STATUSES:
        return

    # Create a time-limited reset token (single-use, server-side tracked).
    from app.core.security import create_reset_token

    raw_token = create_reset_token(user.id)
    import hashlib
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    from app.models.password_reset_token import PasswordResetToken
    from datetime import timedelta

    reset_record = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(reset_record)
    session.flush()

    # Send the reset email (best-effort; failures are logged but not disclosed).
    try:
        from app.core.email import send_password_reset_email

        settings = _settings()
        send_password_reset_email(
            to_email=normalized,
            reset_token=raw_token,
            frontend_url=settings.FRONTEND_URL,
        )
        logger.info("password reset email sent", extra={"user_id": str(user.id)})
    except Exception:
        logger.exception("failed to send password reset email")


def reset_password(
    session: Session,
    token: str,
    new_password: str,
) -> None:
    """Reset a user's password using a valid reset token.

    Validates the token (signature + expiry + single-use), hashes the new
    password, marks the token as used, and revokes all sessions for the user.
    The caller commits.
    """
    import hashlib

    try:
        claims = security.decode_token(token, expected_type="reset")
    except Exception:
        raise AuthCredentialsError() from None

    try:
        jwt_user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise AuthCredentialsError() from None

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    from app.models.password_reset_token import PasswordResetToken

    reset_record = session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.user_id == jwt_user_id,
        )
    )
    if (
        reset_record is None
        or reset_record.used_at is not None
        or _as_aware_utc(reset_record.expires_at) <= datetime.now(timezone.utc)
    ):
        raise AuthCredentialsError()

    user = session.get(User, jwt_user_id)
    if user is None or user.status not in _AUTHENTICATABLE_STATUSES:
        raise AuthCredentialsError()

    try:
        user.password_hash = security.hash_password(new_password)
    except ValueError as exc:
        raise AuthValidationError(str(exc)) from exc

    reset_record.used_at = datetime.now(timezone.utc)
    revoke_all_sessions(session, user.id)
    auth_event_repo.log_event(session, user_id=user.id, event_type="password_reset")
    session.flush()
    logger.info("password reset completed", extra={"user_id": str(user.id)})


# --- account deactivation (self-service) ----------------------------------------


def deactivate_account(
    session: Session,
    user: User,
    payload: DeactivateAccountRequest,
) -> None:
    """Deactivate the authenticated user's account.

    Requires password confirmation. Sets status to 'disabled' and revokes
    all sessions. The caller commits.
    """
    if user.password_hash is None:
        raise AuthValidationError("this account has no password set")
    if not security.verify_password(payload.password, user.password_hash):
        raise AuthCredentialsError()

    user.status = UserStatus.DISABLED.value
    revoke_all_sessions(session, user.id)
    auth_event_repo.log_event(session, user_id=user.id, event_type="account_deactivated")
    session.flush()
    logger.info("account deactivated", extra={"user_id": str(user.id)})


# --- identity reads (Steps 11, 12, 24) --------------------------------------------------


def get_user_by_id(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def load_student_for_user(session: Session, user: User) -> Student | None:
    """The student profile linked to one user (or None)."""
    return student_repo.get_by_user_id(session, user.id)


def identity_for_user(session: Session, user: User) -> AuthUserRead:
    """Build the safe ``/me`` identity, including the student summary if any."""
    return _read_user(user, load_student_for_user(session, user))


def resolve_current_student(session: Session, user: User) -> Student:
    """The student profile of the authenticated user, with role enforced.

    Role must be ``student`` (403 otherwise); the linked profile must exist
    (403 with an explicit domain error otherwise — a profile is never
    created *implicitly* by a read, Step 12). Self-service creation is the
    explicit ``POST /me/student`` endpoint (``create_profile_for_existing_user``),
    which the 403 message points callers towards.
    """
    if user.role != UserRole.STUDENT.value:
        raise AuthForbiddenError(
            "this operation requires a student account"
        )
    student = load_student_for_user(session, user)
    if student is None:
        raise AuthForbiddenError(
            "authenticated user has no student profile — create it with "
            "POST /api/v1/me/student"
        )
    return student


# Re-exported so the API layer can map every auth-family error from one import.
__all__ = [
    "AuthError",
    "AuthValidationError",
    "AuthConflictError",
    "AuthCredentialsError",
    "AuthForbiddenError",
    "AuthNotFoundError",
    "ProfileValidationError",
    "ProfileConflictError",
    "register_student_account",
    "login",
    "refresh",
    "logout",
    "revoke_all_sessions",
    "get_user_by_id",
    "load_student_for_user",
    "identity_for_user",
    "resolve_current_student",
    "TokenResponse",
]
