"""Unit tests: password change, password reset, and account deactivation.

Tests run on in-memory SQLite through the real service layer, proving:
- password change: verification, policy, same-password rejection,
- password reset: token creation, decode, single-use, expiry,
- account deactivation: password verification, session revocation.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401
from app.core import security
from app.models.auth_session import AuthSession
from app.models.password_reset_token import PasswordResetToken
from app.models.student import Student
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    DeactivateAccountRequest,
    StudentAccountCreate,
)
from app.services import auth_service
from app.services.auth_service import (
    AuthCredentialsError,
    AuthValidationError,
)


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


def _create_student(session: Session, email: str = "kid@example.com") -> User:
    """Helper: create a student user + profile, return the User."""
    from app.repositories import student_repository as student_repo
    from app.repositories import user_repository as user_repo

    user = user_repo.create_student_user(
        session,
        email=email,
        phone=None,
        password_hash=security.hash_password("TestPass123"),
    )
    student_repo.create(
        session,
        user_id=user.id,
        full_name="Test Student",
        date_of_birth=date(2010, 1, 1),
        gender=None,
        country=None,
    )
    session.flush()
    return user


# --- password change tests ---------------------------------------------------


def test_change_password_success(session: Session) -> None:
    user = _create_student(session)
    payload = ChangePasswordRequest(
        current_password="TestPass123",
        new_password="NewPass456!",
    )
    auth_service.change_password(session, user, payload)
    session.flush()

    # Verify the new password works
    refreshed = session.get(User, user.id)
    assert security.verify_password("NewPass456!", refreshed.password_hash)
    assert not security.verify_password("TestPass123", refreshed.password_hash)


def test_change_password_wrong_current(session: Session) -> None:
    user = _create_student(session)
    payload = ChangePasswordRequest(
        current_password="WrongPass!",
        new_password="NewPass456!",
    )
    with pytest.raises(AuthCredentialsError):
        auth_service.change_password(session, user, payload)


def test_change_password_same_password(session: Session) -> None:
    user = _create_student(session)
    payload = ChangePasswordRequest(
        current_password="TestPass123",
        new_password="TestPass123",
    )
    with pytest.raises(AuthValidationError, match="must differ"):
        auth_service.change_password(session, user, payload)


def test_change_password_policy_violation(session: Session) -> None:
    user = _create_student(session)
    with pytest.raises(Exception):  # Pydantic ValidationError
        ChangePasswordRequest(
            current_password="TestPass123",
            new_password="short",
        )


def test_change_password_no_password_set(session: Session) -> None:
    """Admin-created users with no password_hash cannot change password."""
    from app.repositories import user_repository as user_repo

    user = user_repo.create_student_user(
        session, email="nopass@example.com", phone=None, password_hash=None
    )
    session.flush()
    payload = ChangePasswordRequest(
        current_password="anything",
        new_password="NewPass456!",
    )
    with pytest.raises(AuthValidationError, match="no password set"):
        auth_service.change_password(session, user, payload)


# --- password reset token tests ----------------------------------------------


def test_create_reset_token_roundtrip() -> None:
    user_id = uuid.uuid4()
    raw_token = security.create_reset_token(user_id)
    claims = security.decode_token(raw_token, expected_type="reset")
    assert uuid.UUID(claims["sub"]) == user_id
    assert claims["typ"] == "reset"


def test_reset_token_wrong_type_rejected() -> None:
    user_id = uuid.uuid4()
    raw_token = security.create_reset_token(user_id)
    with pytest.raises(Exception):
        security.decode_token(raw_token, expected_type="access")


def test_reset_token_expired_rejected() -> None:
    """Manually create an expired token."""
    import jwt as pyjwt
    from app.core.config import get_settings

    settings = get_settings()
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(uuid.uuid4()),
        "typ": "reset",
        "jti": str(uuid.uuid4()),
        "iat": now - timedelta(hours=2),
        "exp": now - timedelta(hours=1),  # expired 1 hour ago
    }
    raw_token = pyjwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(Exception):
        security.decode_token(raw_token, expected_type="reset")


def test_reset_password_with_valid_token(session: Session) -> None:
    user = _create_student(session)
    raw_token = security.create_reset_token(user.id)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    reset_record = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(reset_record)
    session.flush()

    auth_service.reset_password(session, raw_token, "ResetPass789!")
    session.flush()

    refreshed = session.get(User, user.id)
    assert security.verify_password("ResetPass789!", refreshed.password_hash)
    assert reset_record.used_at is not None


def test_reset_password_revokes_all_sessions(session: Session) -> None:
    user = _create_student(session)

    # Create an existing auth session
    auth_session = AuthSession(
        user_id=user.id,
        token_hash="fake_hash",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(auth_session)
    session.flush()

    raw_token = security.create_reset_token(user.id)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    reset_record = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(reset_record)
    session.flush()

    auth_service.reset_password(session, raw_token, "ResetPass789!")
    session.flush()

    # Expire the cached ORM object and re-fetch from DB
    session.expire_all()
    refreshed = session.get(AuthSession, auth_session.id)
    assert refreshed.revoked_at is not None


def test_reset_password_used_token_rejected(session: Session) -> None:
    user = _create_student(session)
    raw_token = security.create_reset_token(user.id)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    reset_record = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        used_at=datetime.now(timezone.utc),
    )
    session.add(reset_record)
    session.flush()

    with pytest.raises(AuthCredentialsError):
        auth_service.reset_password(session, raw_token, "ResetPass789!")


def test_reset_password_expired_token_rejected(session: Session) -> None:
    user = _create_student(session)
    raw_token = security.create_reset_token(user.id)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    reset_record = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    session.add(reset_record)
    session.flush()

    with pytest.raises(AuthCredentialsError):
        auth_service.reset_password(session, raw_token, "ResetPass789!")


def test_reset_password_invalid_token_rejected(session: Session) -> None:
    with pytest.raises(AuthCredentialsError):
        auth_service.reset_password(session, "garbage.token.value", "ResetPass789!")


# --- account deactivation tests ----------------------------------------------


def test_deactivate_account_success(session: Session) -> None:
    user = _create_student(session)
    payload = DeactivateAccountRequest(password="TestPass123")
    auth_service.deactivate_account(session, user, payload)
    session.flush()

    refreshed = session.get(User, user.id)
    assert refreshed.status == "disabled"


def test_deactivate_account_wrong_password(session: Session) -> None:
    user = _create_student(session)
    payload = DeactivateAccountRequest(password="WrongPass!")
    with pytest.raises(AuthCredentialsError):
        auth_service.deactivate_account(session, user, payload)


def test_deactivate_account_no_password_set(session: Session) -> None:
    from app.repositories import user_repository as user_repo

    user = user_repo.create_student_user(
        session, email="nopass@example.com", phone=None, password_hash=None
    )
    session.flush()
    payload = DeactivateAccountRequest(password="anything")
    with pytest.raises(AuthValidationError, match="no password set"):
        auth_service.deactivate_account(session, user, payload)


def test_deactivate_account_revokes_sessions(session: Session) -> None:
    user = _create_student(session)

    auth_session = AuthSession(
        user_id=user.id,
        token_hash="fake_hash",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(auth_session)
    session.flush()

    payload = DeactivateAccountRequest(password="TestPass123")
    auth_service.deactivate_account(session, user, payload)
    session.flush()

    # Expire the cached ORM object and re-fetch from DB
    session.expire_all()
    refreshed = session.get(AuthSession, auth_session.id)
    assert refreshed.revoked_at is not None
