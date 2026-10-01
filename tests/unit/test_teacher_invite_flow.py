"""Unit tests: teacher invitation acceptance and teacher self-service (Phase B, slice 5).

In-memory SQLite, no HTTP except the explicit route matrix:

- ``POST /auth/accept-invite`` consumes a single-use 72-hour token, sets the
  password (policy enforced) and flips the account ``pending`` → ``active``;
  a second use, an expired row, a wrong token type, an unknown hash and a
  policy violation are all refused without mutating the account;
- a pending account cannot log in; after acceptance the same credentials can;
- ``GET|PATCH /me/teacher`` serve the authenticated teacher's own profile
  only: anonymous → 401, student → 403, admin → 403, teacher → served; the
  update schema forbids unknown keys (including ``school_id``), so a client
  cannot widen its own profile.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base, get_db
import app.models  # noqa: F401
from app.models.enums import UserStatus, UserRole
from app.models.invite_token import InviteToken
from app.models.teacher import Teacher
from app.models.user import User
from app.schemas.auth import AcceptInviteRequest, LoginRequest
from app.schemas.teacher_admin import TeacherCreate, TeacherMeUpdate
from app.services import admin_user_service, auth_service, teacher_service
from app.services.auth_service import (
    AuthConflictError,
    AuthCredentialsError,
    AuthNotFoundError,
    AuthValidationError,
)

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def admin(session: Session) -> User:
    user = User(
        email="admin@example.com",
        role=UserRole.ADMIN.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.commit()
    return user


@pytest.fixture()
def invitation(session: Session, admin: User, monkeypatch) -> tuple[User, str]:
    """A pending teacher plus the raw invitation token from the email."""
    sent: list[str] = []
    monkeypatch.setattr(
        "app.core.email.send_teacher_invite_email",
        lambda *, to_email, invite_token, frontend_url: sent.append(invite_token),
        raising=False,
    )
    payload = TeacherCreate(
        email="invitee@example.com", full_name="Invitee Teacher"
    )
    admin_user_service.create_teacher(session, payload, actor=admin)
    session.commit()
    user = session.scalar(select(User).where(User.email == "invitee@example.com"))
    assert user.status == UserStatus.PENDING.value
    assert len(sent) == 1
    return user, sent[0]


def _row_for(session: Session, user: User) -> InviteToken:
    return session.scalar(select(InviteToken).where(InviteToken.user_id == user.id))


# --- accept invitation ----------------------------------------------------------------


def test_accept_invite_activates_sets_password_and_issues_tokens(
    session: Session, invitation
) -> None:
    user, token = invitation

    identity, tokens = auth_service.accept_invite(session, token, PASSWORD)
    session.commit()

    session.refresh(user)
    assert user.status == UserStatus.ACTIVE.value
    assert security.verify_password(PASSWORD, user.password_hash)
    assert identity.user_id == user.id
    assert tokens.access_token
    assert tokens.refresh_token

    row = _row_for(session, user)
    assert row.used_at is not None

    events = session.scalars(
        select(auth_service.auth_event_repo.AuthEvent).where(
            auth_service.auth_event_repo.AuthEvent.event_type == "invite_accepted"
        )
    ).all()
    assert [event.user_id for event in events] == [user.id]


def test_accept_invite_token_is_single_use(session: Session, invitation) -> None:
    user, token = invitation
    auth_service.accept_invite(session, token, PASSWORD)
    session.commit()

    with pytest.raises(AuthCredentialsError):
        auth_service.accept_invite(session, token, "another password here")
    session.rollback()
    session.refresh(user)
    assert user.status == UserStatus.ACTIVE.value


def test_accept_invite_rejects_an_unknown_token_hash(
    session: Session, invitation
) -> None:
    user, _ = invitation
    forged = security.create_invite_token(user.id)
    with pytest.raises(AuthCredentialsError):
        auth_service.accept_invite(session, forged, PASSWORD)
    session.rollback()
    session.refresh(user)
    assert user.status == UserStatus.PENDING.value


def test_accept_invite_rejects_an_expired_row(session: Session, invitation) -> None:
    user, token = invitation
    row = _row_for(session, user)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()

    with pytest.raises(AuthCredentialsError):
        auth_service.accept_invite(session, token, PASSWORD)
    session.rollback()
    session.refresh(user)
    assert user.status == UserStatus.PENDING.value
    assert _row_for(session, user).used_at is None


def test_accept_invite_rejects_a_reset_token(session: Session, invitation) -> None:
    user, _ = invitation
    reset_token = security.create_reset_token(user.id)
    with pytest.raises(AuthCredentialsError):
        auth_service.accept_invite(session, reset_token, PASSWORD)
    session.rollback()
    session.refresh(user)
    assert user.status == UserStatus.PENDING.value


def test_accept_invite_rejects_a_policy_violating_password(
    session: Session, invitation
) -> None:
    user, token = invitation
    with pytest.raises(AuthValidationError):
        auth_service.accept_invite(session, token, "short")
    session.rollback()
    session.refresh(user)
    assert user.status == UserStatus.PENDING.value
    assert user.password_hash is None
    assert _row_for(session, user).used_at is None


# --- login after acceptance -------------------------------------------------------------


def test_pending_account_cannot_log_in_but_accepted_one_can(
    session: Session, invitation
) -> None:
    user, token = invitation

    with pytest.raises(AuthCredentialsError):
        auth_service.login(
            session, LoginRequest(email="invitee@example.com", password=PASSWORD)
        )

    auth_service.accept_invite(session, token, PASSWORD)
    session.commit()

    tokens = auth_service.login(
        session, LoginRequest(email="invitee@example.com", password=PASSWORD)
    )
    session.commit()
    assert tokens.access_token


# --- teacher self-service ----------------------------------------------------------------


@pytest.fixture()
def active_teacher(session: Session, invitation) -> User:
    user, token = invitation
    auth_service.accept_invite(session, token, PASSWORD)
    session.commit()
    return user


def test_read_teacher_profile_returns_the_callers_own_row(
    session: Session, active_teacher: User
) -> None:
    read = teacher_service.read_teacher_profile(session, active_teacher)
    assert read.user_id == active_teacher.id
    assert read.full_name == "Invitee Teacher"
    assert read.email == "invitee@example.com"


def test_read_teacher_profile_without_a_profile_is_404(
    session: Session, admin: User
) -> None:
    with pytest.raises(AuthNotFoundError):
        teacher_service.read_teacher_profile(session, admin)


def test_update_teacher_profile_changes_only_supplied_fields(
    session: Session, active_teacher: User
) -> None:
    teacher_service.update_teacher_profile(
        session, active_teacher, TeacherMeUpdate(phone="+250780000009")
    )
    session.commit()

    profile = session.scalar(
        select(Teacher).where(Teacher.user_id == active_teacher.id)
    )
    assert profile.phone == "+250780000009"
    assert profile.full_name == "Invitee Teacher"  # untouched
    assert profile.subject is None


def test_update_teacher_profile_ignores_role_and_status(
    session: Session, active_teacher: User
) -> None:
    """The update schema has no role/status field — they cannot be set."""
    with pytest.raises(Exception):
        TeacherMeUpdate(full_name="Renamed", role="admin", status="active")  # type: ignore[call-arg]
    session.refresh(active_teacher)
    assert active_teacher.role == UserRole.TEACHER.value
    assert active_teacher.status == UserStatus.ACTIVE.value


# --- route matrix: /me/teacher ------------------------------------------------------------


@pytest.fixture()
def api_app(session: Session) -> FastAPI:
    from app.api.v1.endpoints.auth import router as auth_router
    from app.api.v1.endpoints.auth import teacher_me_router

    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: session
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(teacher_me_router, prefix="/api/v1")
    return app


@pytest.fixture()
def api_client(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


def _header(user: User) -> dict:
    return {"Authorization": f"Bearer {security.create_access_token(user.id, user.role)}"}


def test_me_teacher_refuses_anonymous(api_client: TestClient) -> None:
    assert api_client.get("/api/v1/me/teacher").status_code == 401
    assert (
        api_client.patch("/api/v1/me/teacher", json={"phone": "+250780000000"}).status_code
        == 401
    )


def test_me_teacher_refuses_students_and_admins(
    session: Session, api_client: TestClient
) -> None:
    for role in (UserRole.STUDENT.value, UserRole.ADMIN.value):
        other = User(
            email=f"{role}-{uuid.uuid4().hex[:6]}@example.com",
            role=role,
            status=UserStatus.ACTIVE.value,
            password_hash=security.hash_password(PASSWORD),
        )
        session.add(other)
        session.commit()
        headers = _header(other)
        assert api_client.get("/api/v1/me/teacher", headers=headers).status_code == 403
        assert (
            api_client.patch(
                "/api/v1/me/teacher", json={"phone": "+250780000000"}, headers=headers
            ).status_code
            == 403
        )


def test_me_teacher_serves_the_teacher(
    session: Session, active_teacher: User, api_client: TestClient
) -> None:
    headers = _header(active_teacher)

    read = api_client.get("/api/v1/me/teacher", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["user_id"] == str(active_teacher.id)

    patched = api_client.patch(
        "/api/v1/me/teacher",
        json={"subject": "Chemistry", "phone": "+250781111111"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["subject"] == "Chemistry"

    # Unknown keys (including school_id and anything role-shaped) are 422.
    rejected = api_client.patch(
        "/api/v1/me/teacher",
        json={"school_id": str(uuid.uuid4()), "role": "admin"},
        headers=headers,
    )
    assert rejected.status_code == 422, rejected.text


# --- accept-invite route ---------------------------------------------------------------------


def test_accept_invite_route_returns_tokens(
    session: Session, invitation, api_client: TestClient
) -> None:
    _, token = invitation
    response = api_client.post(
        "/api/v1/auth/accept-invite",
        json={"token": token, "new_password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]


def test_accept_invite_route_rejects_a_bad_token(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/v1/auth/accept-invite",
        json={"token": "not-a-jwt", "new_password": PASSWORD},
    )
    assert response.status_code == 401, response.text
