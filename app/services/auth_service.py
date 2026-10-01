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
import json
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


def _register_failed_login(
    session: Session,
    user: User,
    now: datetime,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Count one refused attempt; lock the account at the threshold (flush).

    Reaching LOGIN_LOCKOUT_THRESHOLD sets ``locked_until`` to
    now + LOGIN_LOCKOUT_MINUTES and writes a ``login_locked`` audit event
    naming the source. The counter is capped at the threshold.
    """
    settings = _settings()
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= settings.LOGIN_LOCKOUT_THRESHOLD:
        user.failed_login_count = settings.LOGIN_LOCKOUT_THRESHOLD
        user.locked_until = now + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
        auth_event_repo.log_event(
            session,
            user_id=user.id,
            event_type="login_locked",
            ip_address=ip_address,
            user_agent=user_agent,
            metadata_json=json.dumps(
                {"failed_attempts": user.failed_login_count}
            ),
        )
        logger.warning(
            "account locked after repeated failed logins",
            extra={"user_id": str(user.id)},
        )
    session.flush()


def login(
    session: Session,
    payload: LoginRequest,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> TokenResponse:
    """Verify email + password and issue a fresh token pair.

    Lookup, status check and password verification produce one identical
    generic error so the endpoint cannot be used to enumerate accounts,
    infer account state, or even detect a lockout. Never logs the
    password; never mutates the user except for the lockout bookkeeping
    (Phase B, slice 7).

    A dummy hash is always compared even when the user is not found, so the
    timing is identical for both existing and non-existing emails.

    Lockout: the ``LOGIN_LOCKOUT_THRESHOLD``-th refused attempt locks the
    account for ``LOGIN_LOCKOUT_MINUTES`` minutes; while locked (even with
    the correct password) the same generic 401 is returned without
    verifying the password. An expired lock grants a fresh budget; a
    successful login clears it.
    """
    email = str(payload.email).strip().lower()
    user = user_repo.get_by_email(session, email)
    now = datetime.now(timezone.utc)

    if user is not None and user.locked_until is not None:
        locked_until = _as_aware_utc(user.locked_until)
        if locked_until > now:
            # Refused without even comparing the password: identical to a
            # wrong-password answer, and a correct guess cannot reset it.
            auth_event_repo.log_event(
                session,
                user_id=user.id,
                event_type="login_failed",
                ip_address=ip_address,
                user_agent=user_agent,
            )
            logger.info(
                "login refused: account is temporarily locked",
                extra={"user_id": str(user.id)},
            )
            raise AuthCredentialsError()
        # The window elapsed: fresh budget, exactly as after a success.
        user.failed_login_count = 0
        user.locked_until = None

    authenticatable = user is not None and user.status in _AUTHENTICATABLE_STATUSES
    password_hash = user.password_hash if user is not None else _DUMMY_HASH
    password_ok = authenticatable and security.verify_password(
        payload.password, password_hash
    )
    if not password_ok:
        logger.info("failed login attempt for an email (result not disclosed)")
        if user is not None:
            auth_event_repo.log_event(
                session,
                user_id=user.id,
                event_type="login_failed",
                ip_address=ip_address,
                user_agent=user_agent,
            )
            if authenticatable:
                # Only a wrong password on a usable account counts toward
                # the lockout budget (a status gate is not password guessing).
                _register_failed_login(
                    session,
                    user,
                    now,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
        raise AuthCredentialsError()

    user.failed_login_count = 0
    user.locked_until = None
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="login",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    logger.info("successful login", extra={"user_id": str(user.id)})
    return _issue_session(session, user, now)


# --- refresh (Step 22) and logout (Step 23) --------------------------------------------


def refresh(
    session: Session,
    refresh_token_value: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> TokenResponse:
    """Exchange a valid refresh token for a new pair, rotating the session.

    The old session is revoked and a new one issued in the same
    transaction, so a replayed refresh token is refused on its second use.
    """
    now = datetime.now(timezone.utc)
    user, old_session = _verify_refresh_credentials(session, refresh_token_value, now)
    auth_session_repo.revoke(session, old_session, now)
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="token_refresh",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    logger.info("token refresh", extra={"user_id": str(user.id)})
    return _issue_session(session, user, now)


def logout(
    session: Session,
    refresh_token_value: str,
    *,
    expected_user_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Revoke the refresh session presented by an authenticated caller.

    The presented token must be valid (signature, session state, ownership);
    logout then revokes *that* session �?" the caller cannot revoke someone
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
    auth_event_repo.log_event(
        session,
        user_id=expected_user_id,
        event_type="logout",
        ip_address=ip_address,
        user_agent=user_agent,
    )
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
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Change the authenticated user's password.

    Verifies the current password, validates the new password against policy,
    hashes and stores it. The caller commits.
    """
    if user.password_hash is None:
        raise AuthValidationError("this account has no password set")
    if not security.verify_password(payload.current_password, user.password_hash):
        raise AuthCredentialsError()

    # M12: the Pydantic schema enforces the policy for HTTP callers, but the
    # service is callable directly (scripts, tests, future RPC layers), so the
    # policy is enforced here too — before the password is ever hashed.
    try:
        new_password = security.validate_password_policy(payload.new_password)
    except security.PasswordPolicyError as exc:
        raise AuthValidationError(str(exc)) from exc

    if payload.current_password == new_password:
        raise AuthValidationError("new password must differ from current password")

    try:
        new_hash = security.hash_password(new_password)
    except ValueError as exc:
        raise AuthValidationError(str(exc)) from exc

    user.password_hash = new_hash
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="password_change",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.flush()
    logger.info("password changed", extra={"user_id": str(user.id)})


# --- password reset (public, email-based) ---------------------------------------


def forgot_password(session: Session, email: str) -> dict[str, str] | None:
    """Stage a password-reset token row for an active account (L14).

    The row is added and flushed — never committed — so the caller owns the
    transaction exactly as elsewhere: the endpoint commits first and only
    then schedules :func:`app.core.email.deliver_password_reset_email` on
    the request's ``BackgroundTasks``, so a token can never be emailed for a
    row that later rolls back.

    Returns the delivery payload (recipient, raw token, frontend URL), or
    ``None`` when no active account matches — the caller must treat both
    identically to keep enumeration impossible. The raw token is returned
    exactly once, never logged, never stored in plaintext (only its SHA-256
    digest reaches the database).
    """
    normalized = str(email).strip().lower()
    user = user_repo.get_by_email(session, normalized)
    if user is None or user.status not in _AUTHENTICATABLE_STATUSES:
        return None

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
    logger.info(
        "password reset token staged",
        extra={"user_id": str(user.id)},
    )
    return {
        "to_email": normalized,
        "reset_token": raw_token,
        "frontend_url": _settings().FRONTEND_URL,
    }


def reset_password(
    session: Session,
    token: str,
    new_password: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
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

    # M12: reset_password takes a plain string (the token comes from an email
    # link, not from a request schema), so the policy has to be enforced here
    # — a weak password must fail before the token is consumed.
    try:
        candidate = security.validate_password_policy(new_password)
    except security.PasswordPolicyError as exc:
        raise AuthValidationError(str(exc)) from exc

    try:
        user.password_hash = security.hash_password(candidate)
    except ValueError as exc:
        raise AuthValidationError(str(exc)) from exc

    reset_record.used_at = datetime.now(timezone.utc)
    revoke_all_sessions(session, user.id)
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="password_reset",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.flush()
    logger.info("password reset completed", extra={"user_id": str(user.id)})


# --- invitation acceptance (public, teacher onboarding) -------------------------------


def accept_invite(
    session: Session,
    token: str,
    new_password: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[AuthUserRead, TokenResponse]:
    """Consume a single-use invitation token: set the password, activate.

    Mirrors :func:`reset_password`'s token discipline (signature + type +
    stored digest + expiry + single use), with two differences: the account
    must be a ``pending`` teacher, and success activates it and issues a
    token pair so the invitee is logged in immediately after choosing a
    password.

    Every check runs before any mutation, and a rejected password leaves
    the account exactly as it was (still pending, token still unused). The
    caller commits.
    """
    try:
        claims = security.decode_token(token, expected_type="invite")
    except Exception:
        raise AuthCredentialsError() from None

    try:
        jwt_user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise AuthCredentialsError() from None

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    from app.models.invite_token import InviteToken

    invite_row = session.scalar(
        select(InviteToken).where(
            InviteToken.token_hash == token_hash,
            InviteToken.user_id == jwt_user_id,
        )
    )
    if (
        invite_row is None
        or invite_row.used_at is not None
        or _as_aware_utc(invite_row.expires_at) <= datetime.now(timezone.utc)
    ):
        raise AuthCredentialsError()

    user = session.get(User, jwt_user_id)
    if (
        user is None
        or user.role != UserRole.TEACHER.value
        or user.status != UserStatus.PENDING.value
    ):
        raise AuthCredentialsError()

    # Policy first: a rejected password must leave the account untouched.
    try:
        password_hash = security.hash_password(
            security.validate_password_policy(new_password)
        )
    except (PasswordPolicyError, ValueError) as exc:
        raise AuthValidationError(str(exc)) from exc

    now = datetime.now(timezone.utc)
    user.password_hash = password_hash
    user.status = UserStatus.ACTIVE.value
    invite_row.used_at = now
    revoke_all_sessions(session, user.id)
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="invite_accepted",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.flush()
    logger.info("teacher invitation accepted", extra={"user_id": str(user.id)})

    identity = _read_user(user, None)
    tokens = _issue_session(session, user, now)
    return identity, tokens


# --- account deactivation (self-service) --------------------------------------------


def deactivate_account(
    session: Session,
    user: User,
    payload: DeactivateAccountRequest,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
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
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="account_deactivated",
        ip_address=ip_address,
        user_agent=user_agent,
    )
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
