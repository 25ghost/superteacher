"""Unit tests: authentication core (Phase 5G, in-memory SQLite / no HTTP).

Covers:

- password hashing (Argon2id verify, wrong password, no plaintext),
- password policy (minimum length, empty, whitespace),
- JWT utilities (valid, expired, malformed, wrong token type, tampered),
- production secret validation (placeholder refused outside development),
- auth service: registration (role forced, duplicate email, rollback),
  login (generic failure, disabled/suspended accounts), refresh rotation,
  logout/revocation, and the current-student resolution.

No HTTP is involved; the database is an in-memory SQLite scratch database.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import jwt as pyjwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.core import security
from app.core.config import Settings
from app.models.enums import UserRole, UserStatus
from app.models.student import Student
from app.models.user import User
from app.schemas.auth import LoginRequest, StudentAccountCreate
from app.services import auth_service
from app.services import student_service


# --- scaffolding -------------------------------------------------------------------


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _account_payload(**overrides) -> StudentAccountCreate:
    values = dict(
        email="student@example.com",
        password="correct horse battery staple",
        full_name="Test Student",
        date_of_birth=date(2012, 4, 10),
        gender="female",
        country="Rwanda",
    )
    values.update(overrides)
    return StudentAccountCreate(**values)


# --- password hashing (Step 32: passwords) -------------------------------------------


def test_hash_and_verify_password_roundtrip() -> None:
    hashed = security.hash_password("s3cret-passphrase")
    assert hashed != "s3cret-passphrase"
    assert hashed.startswith("$argon2id$")
    assert security.verify_password("s3cret-passphrase", hashed)


def test_wrong_password_fails_verification() -> None:
    hashed = security.hash_password("s3cret-passphrase")
    assert not security.verify_password("wrong-password", hashed)


def test_malformed_hash_never_raises() -> None:
    assert not security.verify_password("x", "not-a-hash")
    assert not security.verify_password("x", "")
    assert not security.verify_password("", security.hash_password("x"))


def test_no_plaintext_storage_in_user_row(session: Session) -> None:
    identity, _ = auth_service.register_student_account(session, _account_payload())
    session.commit()
    stored = session.get(User, identity.user_id)
    assert stored.password_hash.startswith("$argon2id$")
    assert "correct horse battery staple" not in (stored.password_hash or "")


# --- password policy (Step 3) ---------------------------------------------------------


def test_password_minimum_length_enforced(session: Session) -> None:
    """The policy fires at both the schema layer (pydantic) and the service."""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        _account_payload(password="short")  # schema rejects before any DB work
    with pytest.raises(auth_service.AuthValidationError):
        payload = _account_payload()
        auth_service.register_student_account(
            session, payload.model_copy(update={"password": "short"})
        )
    assert session.query(User).count() == 0


def test_password_empty_rejected(session: Session) -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises((PydanticValidationError, auth_service.AuthValidationError)):
        auth_service.register_student_account(
            session, _account_payload(password="")
        )
    with pytest.raises((PydanticValidationError, auth_service.AuthValidationError)):
        auth_service.register_student_account(
            session, _account_payload(password="   ")
        )


def test_policy_rejects_before_any_write(session: Session) -> None:
    """A policy violation must not create the user row at all."""
    with pytest.raises(auth_service.AuthValidationError):
        payload = _account_payload()
        auth_service.register_student_account(
            session, payload.model_copy(update={"password": "tiny"})
        )
    assert session.query(User).count() == 0


# --- JWT utilities (Step 32: tokens) ------------------------------------------------------


def test_access_token_roundtrip() -> None:
    token = security.create_access_token(uuid.uuid4(), UserRole.STUDENT.value)
    claims = security.decode_token(token, expected_type="access")
    assert claims["typ"] == "access"
    assert claims["role"] == UserRole.STUDENT.value
    assert "jti" in claims
    uuid.UUID(claims["jti"])  # valid UUID format


def test_expired_token_rejected() -> None:
    token = security.create_access_token(uuid.uuid4(), "student", expires_minutes=-1)
    with pytest.raises(pyjwt.PyJWTError):
        security.decode_token(token, expected_type="access")


def test_malformed_token_rejected() -> None:
    with pytest.raises(pyjwt.PyJWTError):
        security.decode_token("not.a.jwt", expected_type="access")


def test_wrong_token_type_rejected() -> None:
    token = security.create_access_token(uuid.uuid4(), "student")
    with pytest.raises(pyjwt.PyJWTError):
        security.decode_token(token, expected_type="refresh")


def test_tampered_token_rejected() -> None:
    token = security.create_access_token(uuid.uuid4(), "student")
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-2]}aa.{signature}"
    with pytest.raises(pyjwt.PyJWTError):
        security.decode_token(tampered, expected_type="access")


def test_token_signed_with_other_secret_rejected(monkeypatch) -> None:
    user_id = uuid.uuid4()
    token = security.create_access_token(user_id, "student")  # noqa: F841 (issue side only)
    forged = pyjwt.encode(
        {"sub": str(user_id), "role": "admin", "typ": "access",
         "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        "attacker-secret",
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.PyJWTError):
        security.decode_token(forged, expected_type="access")


# --- production secret validation (Step 8) ----------------------------------------------


def test_placeholder_secret_refused_in_production() -> None:
    settings = Settings(ENVIRONMENT="production", SECRET_KEY="change-me", _env_file=None)
    with pytest.raises(RuntimeError):
        _ = settings.jwt_secret


def test_empty_secret_refused_in_production() -> None:
    settings = Settings(ENVIRONMENT="production", SECRET_KEY="  ", _env_file=None)
    with pytest.raises(RuntimeError):
        _ = settings.jwt_secret


def test_real_secret_accepted_in_production() -> None:
    settings = Settings(
        ENVIRONMENT="production", SECRET_KEY="x" * 64, _env_file=None
    )
    assert settings.jwt_secret == "x" * 64


def test_placeholder_allowed_in_development() -> None:
    settings = Settings(ENVIRONMENT="development", SECRET_KEY="change-me", _env_file=None)
    assert settings.jwt_secret == "change-me"


def test_short_secret_refused_in_production() -> None:
    settings = Settings(ENVIRONMENT="production", SECRET_KEY="short-but-not-placeholder", _env_file=None)
    with pytest.raises(RuntimeError, match="too short"):
        _ = settings.jwt_secret


def test_short_secret_allowed_in_development() -> None:
    settings = Settings(ENVIRONMENT="development", SECRET_KEY="short", _env_file=None)
    assert settings.jwt_secret == "short"


# --- auth service: registration (Step 32: registration) --------------------------------


def test_register_creates_user_and_student_atomically(session: Session) -> None:
    identity, tokens = auth_service.register_student_account(session, _account_payload())
    session.commit()
    user = session.get(User, identity.user_id)
    student = session.query(Student).filter_by(user_id=identity.user_id).one()
    assert user.email == "student@example.com"
    assert user.role == UserRole.STUDENT.value
    assert user.password_hash is not None
    assert student.full_name == "Test Student"
    assert tokens.access_token and tokens.refresh_token


def test_register_role_is_forced_to_student(session: Session) -> None:
    """No request field can change the role — the schema has no role field."""
    assert "role" not in StudentAccountCreate.model_fields
    identity, _ = auth_service.register_student_account(session, _account_payload())
    session.commit()
    assert session.get(User, identity.user_id).role == UserRole.STUDENT.value


def test_register_normalizes_email_to_lowercase(session: Session) -> None:
    """Registration lowercases the email before storing."""
    identity, _ = auth_service.register_student_account(
        session, _account_payload(email="Mixed@School.COM")
    )
    session.commit()
    user = session.get(User, identity.user_id)
    assert user.email == "mixed@school.com"


def test_login_with_mixed_case_email_succeeds(session: Session) -> None:
    """Login normalizes email to match the stored lowercase form."""
    _registered(session, email="Mixed@Example.COM")
    session.commit()
    tokens = auth_service.login(
        session,
        LoginRequest(email="Mixed@Example.COM", password="correct horse battery staple"),
    )
    assert tokens.access_token


def test_register_duplicate_email_conflict(session: Session) -> None:
    auth_service.register_student_account(session, _account_payload())
    session.commit()
    with pytest.raises(auth_service.AuthConflictError):
        auth_service.register_student_account(session, _account_payload(full_name="Other"))
    session.rollback()
    assert session.query(User).count() == 1


def test_register_duplicate_email_message_is_generic(session: Session) -> None:
    """Public registration never confirms an email exists (anti-enumeration)."""
    auth_service.register_student_account(session, _account_payload())
    session.commit()
    with pytest.raises(auth_service.AuthConflictError) as excinfo:
        auth_service.register_student_account(session, _account_payload(full_name="Other"))
    session.rollback()
    detail = str(excinfo.value)
    assert "student@example.com" not in detail
    assert "already exists" not in detail
    assert detail == "email cannot be used to create an account"


def test_register_future_dob_is_422_not_500(session: Session) -> None:
    """The shared DOB guard fires before any insert — no IntegrityError path."""
    with pytest.raises(auth_service.AuthValidationError, match="future"):
        auth_service.register_student_account(
            session, _account_payload(date_of_birth=date.today() + timedelta(days=7))
        )
    assert session.query(User).count() == 0
    assert session.query(Student).count() == 0


def test_register_implausible_old_dob_is_422(session: Session) -> None:
    with pytest.raises(auth_service.AuthValidationError, match="years"):
        auth_service.register_student_account(
            session, _account_payload(date_of_birth=date(1850, 1, 1))
        )
    assert session.query(User).count() == 0


def test_register_too_young_dob_is_422(session: Session) -> None:
    with pytest.raises(auth_service.AuthValidationError, match="at least"):
        auth_service.register_student_account(
            session, _account_payload(date_of_birth=date.today())
        )
    assert session.query(User).count() == 0


def test_register_writes_create_audit_row(session: Session) -> None:
    """Self-service registration is audited, attributed to the new account."""
    from app.repositories import student_profile_history_repository as history_repo

    identity, _ = auth_service.register_student_account(session, _account_payload())
    session.commit()
    student = session.query(Student).filter_by(user_id=identity.user_id).one()
    records = history_repo.list_for_student(session, student.id)
    creates = [r for r in records if r.change_type == "create"]
    assert len(creates) == 1
    assert creates[0].field_name == "profile"
    assert creates[0].changed_by == identity.user_id


def test_register_rejects_markup_in_name(session: Session) -> None:
    """StudentAccountCreate shares the hardened full_name validator."""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError, match="full_name"):
        _account_payload(full_name="Bad <script> Name")


def test_register_password_never_logged(session: Session, caplog) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        auth_service.register_student_account(session, _account_payload())
    session.commit()
    for record in caplog.records:
        assert "correct horse battery staple" not in record.getMessage()


# --- auth service: login (Step 32: login) --------------------------------------------------


def _registered(session: Session, **overrides):
    return auth_service.register_student_account(session, _account_payload(**overrides))


def test_login_correct_credentials_issue_tokens(session: Session) -> None:
    _registered(session)
    session.commit()
    tokens = auth_service.login(
        session, LoginRequest(email="student@example.com", password="correct horse battery staple")
    )
    assert tokens.access_token and tokens.refresh_token


def test_login_wrong_password_generic_401(session: Session) -> None:
    _registered(session)
    session.commit()
    with pytest.raises(auth_service.AuthCredentialsError) as excinfo:
        auth_service.login(
            session, LoginRequest(email="student@example.com", password="wrong")
        )
    assert excinfo.value.status_code == 401
    assert "invalid email or password" in str(excinfo.value)


def test_login_unknown_email_same_generic_401(session: Session) -> None:
    """Wrong password vs unknown email: identical generic 401 message."""
    with pytest.raises(auth_service.AuthCredentialsError) as wrong_pw:
        auth_service.login(
            session, LoginRequest(email="student@example.com", password="wrong")
        )
    with pytest.raises(auth_service.AuthCredentialsError) as unknown:
        auth_service.login(
            session, LoginRequest(email="who@example.com", password="whatever")
        )
    assert str(unknown.value) == str(wrong_pw.value) == "invalid email or password"


def test_login_disabled_account_rejected(session: Session) -> None:
    identity, _ = _registered(session)
    session.commit()
    user = session.get(User, identity.user_id)
    user.status = UserStatus.DISABLED.value
    session.commit()
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.login(
            session, LoginRequest(email="student@example.com", password="correct horse battery staple")
        )


def test_login_suspended_account_rejected(session: Session) -> None:
    identity, _ = _registered(session)
    session.commit()
    user = session.get(User, identity.user_id)
    user.status = UserStatus.SUSPENDED.value
    session.commit()
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.login(
            session, LoginRequest(email="student@example.com", password="correct horse battery staple")
        )


def test_login_does_not_modify_status(session: Session) -> None:
    identity, _ = _registered(session)
    session.commit()
    session.get(User, identity.user_id).status = UserStatus.SUSPENDED.value
    session.commit()
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.login(
            session, LoginRequest(email="student@example.com", password="correct horse battery staple")
        )
    assert session.get(User, identity.user_id).status == UserStatus.SUSPENDED.value


# --- refresh / logout (Steps 22, 23) ----------------------------------------------------------


def test_refresh_rotates_and_old_token_dies(session: Session) -> None:
    _, tokens = _registered(session)
    session.commit()
    fresh = auth_service.refresh(session, tokens.refresh_token)
    session.commit()
    assert fresh.refresh_token != tokens.refresh_token
    # Verify jti claim is present on refresh tokens
    new_claims = security.decode_token(fresh.refresh_token, expected_type="refresh")
    assert "jti" in new_claims
    uuid.UUID(new_claims["jti"])
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.refresh(session, tokens.refresh_token)  # replay refused


def test_logout_revokes_refresh(session: Session) -> None:
    identity, tokens = _registered(session)
    session.commit()
    auth_service.logout(
        session, tokens.refresh_token, expected_user_id=identity.user_id
    )
    session.commit()
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.refresh(session, tokens.refresh_token)


def test_logout_rejects_other_users_refresh_token(session: Session) -> None:
    """User A's access token cannot revoke User B's refresh session."""
    identity_a, tokens_a = _registered(session)
    _, tokens_b = _registered(session, email="other@example.com")
    session.commit()
    with pytest.raises(auth_service.AuthForbiddenError):
        auth_service.logout(
            session,
            tokens_b.refresh_token,
            expected_user_id=identity_a.user_id,
        )


def test_revoke_all_sessions(session: Session) -> None:
    identity, tokens = _registered(session)
    session.commit()
    second = auth_service.login(
        session, LoginRequest(email="student@example.com", password="correct horse battery staple")
    )
    session.commit()
    revoked = auth_service.revoke_all_sessions(session, identity.user_id)
    session.commit()
    assert revoked == 2
    for token_pair in (tokens, second):
        with pytest.raises(auth_service.AuthCredentialsError):
            auth_service.refresh(session, token_pair.refresh_token)


def test_garbage_refresh_token_refused(session: Session) -> None:
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.refresh(session, "garbage")
    # A well-formed JWT for a session that does not exist server-side.
    ghost = security.create_refresh_token(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.refresh(session, ghost)


# --- current student resolution (Step 12) ---------------------------------------------------


def test_resolve_current_student_ok(session: Session) -> None:
    identity, _ = _registered(session)
    session.commit()
    user = session.get(User, identity.user_id)
    student = auth_service.resolve_current_student(session, user)
    assert student.user_id == identity.user_id


def test_resolve_current_student_non_student_403(session: Session) -> None:
    teacher = User(email="t@example.com", role=UserRole.TEACHER.value, password_hash="x")
    session.add(teacher)
    session.commit()
    with pytest.raises(auth_service.AuthForbiddenError) as excinfo:
        auth_service.resolve_current_student(session, teacher)
    assert excinfo.value.status_code == 403


def test_resolve_current_student_missing_profile_403(session: Session) -> None:
    user = User(
        email="noprofile@example.com",
        role=UserRole.STUDENT.value,
        password_hash=security.hash_password("whatever-pass"),
    )
    session.add(user)
    session.commit()
    with pytest.raises(auth_service.AuthForbiddenError) as excinfo:
        auth_service.resolve_current_student(session, user)
    assert excinfo.value.status_code == 403
    # The 403 points the caller at the explicit self-service remedy.
    assert "POST /api/v1/me/student" in str(excinfo.value)


def test_identity_for_user_tolerates_missing_profile(session: Session) -> None:
    """GET /me stays 200 with student: null — the discovery signal that a
    profile-less student can (and now must) create it via POST /me/student."""
    user = User(
        email="profileless-id@example.com",
        role=UserRole.STUDENT.value,
        password_hash=security.hash_password("whatever-pass"),
    )
    session.add(user)
    session.commit()
    identity = auth_service.identity_for_user(session, user)
    assert identity.student is None
    assert identity.role == "student"


# --- expired refresh session (server-side expiry) -------------------------------------------


def test_expired_refresh_session_refused(session: Session) -> None:
    _, tokens = _registered(session)
    session.commit()
    from app.repositories import auth_session_repository as repo

    row = repo.get_by_token_hash(
        session,
        hashlib.sha256(tokens.refresh_token.encode()).hexdigest(),
    )
    row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.commit()
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.refresh(session, tokens.refresh_token)


# --- dev guard retained as infrastructure (Step 25) -------------------------------------------


def test_dev_guard_still_refuses_production(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.core.dev_guard import require_development_stage

    monkeypatch.setattr(
        "app.core.dev_guard.get_settings",
        lambda: type("S", (), {"ENVIRONMENT": "production"})(),
    )
    with pytest.raises(HTTPException) as excinfo:
        require_development_stage()
    assert excinfo.value.status_code == 503


# --- security hardening tests -------------------------------------------------------


def test_email_normalization_in_admin_profile_creation(session: Session) -> None:
    """Admin-created profiles with mixed-case email resolve to lowercase."""
    from datetime import date

    from app.schemas.student_profile import StudentProfileCreate

    payload = StudentProfileCreate(
        email="Mixed@School.COM",
        full_name="Test Student",
        date_of_birth=date(2000, 1, 15),
    )
    profile = student_service.create_student_profile(session, payload)
    session.flush()
    user = session.get(User, profile.user_id)
    assert user.email == "mixed@school.com"


def test_access_token_rejected_after_account_suspension(session: Session) -> None:
    """A valid access token is rejected once the user is suspended."""
    from datetime import date

    from app.schemas.student_profile import StudentProfileCreate

    payload = StudentAccountCreate(
        email="suspend-test@example.com",
        password="secure-password-123",
        full_name="Suspend Me",
        date_of_birth=date(2000, 6, 15),
    )
    _, tokens = auth_service.register_student_account(session, payload)
    session.commit()
    # Token works before suspension
    claims = security.decode_token(tokens.access_token, expected_type="access")
    user_id = uuid.UUID(claims["sub"])
    user = session.get(User, user_id)
    assert user is not None
    assert user.status == UserStatus.ACTIVE.value
    # Suspend the user
    user.status = UserStatus.SUSPENDED.value
    session.flush()
    # Access token should now be rejected by the dependency
    from app.core.auth_dependencies import get_current_user
    from fastapi import Request
    from fastapi.security import HTTPAuthorizationCredentials

    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=tokens.access_token
    )
    fake_request = type("R", (), {"headers": {}})()
    with pytest.raises(Exception):
        # get_current_user checks status against the database
        get_current_user(credentials=credentials, session=session)


def test_revoke_all_sessions_does_not_miss_newly_created_session(
    session: Session,
) -> None:
    """Atomic UPDATE in revoke_all_for_user catches all active sessions."""
    identity, tokens = _registered(session)
    session.commit()
    # Create a second session via refresh (first is revoked by rotation)
    second = auth_service.refresh(session, tokens.refresh_token)
    session.commit()
    # Revoke all — the refresh-created session must be caught
    count = auth_service.revoke_all_sessions(session, identity.user_id)
    session.flush()
    assert count >= 1
    # Both old tokens should be rejected
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.refresh(session, tokens.refresh_token)
    with pytest.raises(auth_service.AuthCredentialsError):
        auth_service.refresh(session, second.refresh_token)
