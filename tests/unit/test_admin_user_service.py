"""Unit tests: administrator user management (Phase B, slice 4).

Service-level behaviour on in-memory SQLite, plus the cross-role matrix for
the real ``/admin/teachers`` and ``/admin/users/{id}/role`` routes:

- creating a teacher yields a ``pending`` user (no password), a profile row
  and a single-use invite token — the raw token is never persisted, only its
  SHA-256 digest;
- every administrative action writes an ``auth_events`` row carrying the
  acting administrator (``actor_user_id``) — the audit trail answers "who
  did this", not just "what happened";
- activate/deactivate/role-change revoke outstanding refresh sessions, so a
  demoted or suspended account cannot keep using an old token;
- anonymous → 401, student → 403, teacher → 403, admin → served, for every
  new route.
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
from app.models.auth_event import AuthEvent
from app.models.auth_session import AuthSession
from app.models.enums import UserStatus, UserRole
from app.models.invite_token import InviteToken
from app.models.teacher import Teacher
from app.models.user import User
from app.schemas.teacher_admin import RoleChangeRequest, TeacherCreate
from app.services import admin_user_service
from app.services.auth_service import (
    AuthConflictError,
    AuthForbiddenError,
    AuthNotFoundError,
)

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = security.hash_password(PASSWORD)


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


def _make_user(
    session: Session,
    *,
    role: str,
    status: str = UserStatus.ACTIVE.value,
    email: str | None = None,
) -> User:
    user = User(
        email=email or f"{uuid.uuid4().hex[:10]}@example.com",
        role=role,
        status=status,
        password_hash=PASSWORD_HASH,
    )
    session.add(user)
    session.flush()
    return user


@pytest.fixture()
def admin(session: Session) -> User:
    user = _make_user(session, role=UserRole.ADMIN.value)
    session.commit()
    return user


def _seed_session(session: Session, user: User) -> AuthSession:
    row = AuthSession(
        user_id=user.id,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(row)
    session.flush()
    return row


def _events(session: Session, event_type: str | None = None) -> list[AuthEvent]:
    stmt = select(AuthEvent).order_by(AuthEvent.created_at)
    if event_type is not None:
        stmt = stmt.where(AuthEvent.event_type == event_type)
    return list(session.scalars(stmt))


def _create_teacher(session: Session, actor: User, email: str | None = None) -> tuple[User, Teacher]:
    payload = TeacherCreate(
        email=email or f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Grace Teacher",
        phone="+250780000001",
        subject="Mathematics",
    )
    admin_user_service.create_teacher(session, payload, actor=actor)
    session.commit()
    created = session.scalar(select(User).where(User.email == payload.email))
    profile = session.scalar(select(Teacher).where(Teacher.user_id == created.id))
    return created, profile


# --- create ----------------------------------------------------------------------


def test_create_teacher_is_pending_and_audited(session: Session, admin: User) -> None:
    payload = TeacherCreate(
        email="  Grace.Example@School.RW  ",
        full_name="Grace Teacher",
        phone="+250780000001",
        subject="Mathematics",
    )
    read = admin_user_service.create_teacher(session, payload, actor=admin)
    session.commit()

    user = session.get(User, read.user_id)
    assert user.role == UserRole.TEACHER.value
    assert user.status == UserStatus.PENDING.value
    assert user.password_hash is None  # no password until the invite is accepted
    assert user.email == "grace.example@school.rw"  # normalized

    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    assert profile.full_name == "Grace Teacher"
    assert profile.phone == "+250780000001"
    assert profile.subject == "Mathematics"
    assert profile.school_id is None

    # Audit: the acting administrator is on the event.
    [event] = _events(session, "teacher_created")
    assert event.user_id == user.id
    assert event.actor_user_id == admin.id

    assert read.status == UserStatus.PENDING.value
    assert read.role == UserRole.TEACHER.value


def test_create_teacher_invite_token_is_stored_hashed_only(
    session: Session, admin: User, monkeypatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "app.core.email.send_teacher_invite_email",
        lambda *, to_email, invite_token, frontend_url: sent.append(invite_token),
        raising=False,
    )
    payload = TeacherCreate(email="hash@example.com", full_name="Hash Teacher")
    admin_user_service.create_teacher(session, payload, actor=admin)
    session.commit()

    [token_row] = session.scalars(select(InviteToken)).all()
    assert token_row.used_at is None
    # SQLite hands tz-aware columns back naive; normalise before comparing.
    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    assert expires_at > datetime.now(timezone.utc)
    # The raw token went to the email only; the row keeps its digest.
    assert len(sent) == 1
    assert sent[0] != token_row.token_hash
    assert len(token_row.token_hash) == 64  # sha256 hex digest
    assert sent[0] not in token_row.token_hash


def test_create_teacher_duplicate_email_conflicts(session: Session, admin: User) -> None:
    _create_teacher(session, admin, email="dupe@example.com")
    with pytest.raises(AuthConflictError):
        admin_user_service.create_teacher(
            session, TeacherCreate(email="DUPE@example.com", full_name="Another"),
            actor=admin,
        )
    session.rollback()
    remaining = session.scalars(select(User).where(User.role == "teacher")).all()
    assert len(remaining) == 1


def test_create_teacher_does_not_touch_other_roles(session: Session, admin: User) -> None:
    student = _make_user(session, role=UserRole.STUDENT.value)
    session.commit()
    _create_teacher(session, admin, email="new@example.com")
    session.commit()
    assert session.get(User, student.id).role == UserRole.STUDENT.value


# --- list ------------------------------------------------------------------------


def test_list_teachers_returns_only_teacher_accounts(session: Session, admin: User) -> None:
    _create_teacher(session, admin, email="one@example.com")
    _create_teacher(session, admin, email="two@example.com")
    _make_user(session, role=UserRole.STUDENT.value, email="not-a-teacher@example.com")
    session.commit()

    listed = admin_user_service.list_teachers(session)
    emails = {row.email for row in listed}
    assert emails == {"one@example.com", "two@example.com"}
    assert all(row.role == UserRole.TEACHER.value for row in listed)


# --- resend invite ---------------------------------------------------------------


def test_resend_invite_rotates_token_and_invalidates_the_old_one(
    session: Session, admin: User, monkeypatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "app.core.email.send_teacher_invite_email",
        lambda *, to_email, invite_token, frontend_url: sent.append(invite_token),
        raising=False,
    )
    user, _ = _create_teacher(session, admin, email="rotate@example.com")
    first_hash = session.scalar(select(InviteToken)).token_hash

    admin_user_service.resend_invite(session, user.id, actor=admin)
    session.commit()

    rows = list(session.scalars(select(InviteToken).where(InviteToken.user_id == user.id)))
    assert len(rows) == 2
    hashes = {row.token_hash for row in rows}
    assert len(hashes) == 2  # a fresh token was minted
    assert {row.used_at is not None for row in rows} == {True, False}
    assert first_hash in hashes
    assert len(sent) == 2
    # One event per invitation sent (create + resend), each naming the actor.
    events = _events(session, "teacher_invite_sent")
    assert len(events) == 2
    assert {event.actor_user_id for event in events} == {admin.id}


def test_resend_invite_unknown_user_is_404(session: Session, admin: User) -> None:
    with pytest.raises(AuthNotFoundError):
        admin_user_service.resend_invite(session, uuid.uuid4(), actor=admin)


def test_resend_invite_for_an_active_teacher_conflicts(session: Session, admin: User) -> None:
    user, _ = _create_teacher(session, admin, email="active@example.com")
    user.status = UserStatus.ACTIVE.value
    session.commit()
    with pytest.raises(AuthConflictError):
        admin_user_service.resend_invite(session, user.id, actor=admin)


# --- activate / deactivate -------------------------------------------------------


def test_activate_pending_teacher_becomes_active(session: Session, admin: User) -> None:
    user, _ = _create_teacher(session, admin, email="pending@example.com")
    assert user.status == UserStatus.PENDING.value

    read = admin_user_service.activate_teacher(session, user.id, actor=admin)
    session.commit()

    assert read.status == UserStatus.ACTIVE.value
    assert session.get(User, user.id).status == UserStatus.ACTIVE.value
    [event] = _events(session, "teacher_activated")
    assert event.user_id == user.id
    assert event.actor_user_id == admin.id


def test_activate_unknown_user_is_404(session: Session, admin: User) -> None:
    with pytest.raises(AuthNotFoundError):
        admin_user_service.activate_teacher(session, uuid.uuid4(), actor=admin)


def test_activate_already_active_teacher_conflicts(session: Session, admin: User) -> None:
    user, _ = _create_teacher(session, admin, email="twice@example.com")
    user.status = UserStatus.ACTIVE.value
    session.commit()
    with pytest.raises(AuthConflictError):
        admin_user_service.activate_teacher(session, user.id, actor=admin)


def test_deactivate_suspends_and_revokes_sessions(session: Session, admin: User) -> None:
    user, _ = _create_teacher(session, admin, email="suspend@example.com")
    user.status = UserStatus.ACTIVE.value
    live = _seed_session(session, user)
    session.commit()

    read = admin_user_service.deactivate_teacher(session, user.id, actor=admin)
    session.commit()
    session.expire_all()  # the bulk UPDATE in revoke_all_for_user bypasses the identity map

    assert read.status == UserStatus.SUSPENDED.value
    assert session.get(User, user.id).status == UserStatus.SUSPENDED.value
    assert session.get(AuthSession, live.id).revoked_at is not None
    [event] = _events(session, "teacher_deactivated")
    assert event.actor_user_id == admin.id


def test_deactivate_pending_teacher_cancels_the_invitation(
    session: Session, admin: User
) -> None:
    user, _ = _create_teacher(session, admin, email="cancel@example.com")
    read = admin_user_service.deactivate_teacher(session, user.id, actor=admin)
    session.commit()
    assert read.status == UserStatus.SUSPENDED.value


def test_deactivate_unknown_user_is_404(session: Session, admin: User) -> None:
    with pytest.raises(AuthNotFoundError):
        admin_user_service.deactivate_teacher(session, uuid.uuid4(), actor=admin)


# --- generic activate: never-accepted teachers ---------------------------------


def test_generic_activate_refuses_a_teacher_suspended_before_accepting(
    session: Session, admin: User
) -> None:
    """password_hash IS NULL decides, not the status: pending → suspended → 409."""
    user, _ = _create_teacher(session, admin, email="never-accepted@example.com")
    assert user.password_hash is None
    admin_user_service.deactivate_user(session, user.id, actor=admin)
    session.commit()
    assert session.get(User, user.id).status == UserStatus.SUSPENDED.value

    with pytest.raises(AuthConflictError) as excinfo:
        admin_user_service.activate_user(session, user.id, actor=admin)
    session.rollback()

    assert "invitation" in str(excinfo.value)
    assert session.get(User, user.id).status == UserStatus.SUSPENDED.value
    assert not _events(session, "user_activated")


def test_generic_activate_refuses_a_pending_teacher(session: Session, admin: User) -> None:
    user, _ = _create_teacher(session, admin, email="generic-pending@example.com")
    with pytest.raises(AuthConflictError) as excinfo:
        admin_user_service.activate_user(session, user.id, actor=admin)
    session.rollback()
    assert "invitation" in str(excinfo.value)
    assert session.get(User, user.id).status == UserStatus.PENDING.value


def test_generic_activate_accepts_a_suspended_teacher_that_has_a_password(
    session: Session, admin: User
) -> None:
    """The refusal is about the missing password, not about the role alone."""
    user, _ = _create_teacher(session, admin, email="has-password@example.com")
    user.password_hash = PASSWORD_HASH
    user.status = UserStatus.SUSPENDED.value
    session.commit()

    read = admin_user_service.activate_user(session, user.id, actor=admin)
    session.commit()

    assert read.status == UserStatus.ACTIVE.value
    assert session.get(User, user.id).status == UserStatus.ACTIVE.value
    [event] = _events(session, "user_activated")
    assert event.actor_user_id == admin.id


# --- role change -----------------------------------------------------------------


def test_change_role_updates_and_revokes_sessions(session: Session, admin: User) -> None:
    target = _make_user(session, role=UserRole.STUDENT.value, email="promote@example.com")
    live = _seed_session(session, target)
    session.commit()

    read = admin_user_service.change_user_role(
        session, target.id, role=UserRole.ADMIN.value, actor=admin
    )
    session.commit()
    session.expire_all()  # the bulk UPDATE in revoke_all_for_user bypasses the identity map

    assert read.role == UserRole.ADMIN.value
    assert session.get(User, target.id).role == UserRole.ADMIN.value
    assert session.get(AuthSession, live.id).revoked_at is not None
    [event] = _events(session, "user_role_changed")
    assert event.user_id == target.id
    assert event.actor_user_id == admin.id
    assert event.metadata_json is not None and "admin" in event.metadata_json


def test_change_role_refuses_self_demotion(session: Session, admin: User) -> None:
    session.commit()
    with pytest.raises(AuthForbiddenError):
        admin_user_service.change_user_role(
            session, admin.id, role=UserRole.STUDENT.value, actor=admin
        )
    session.rollback()
    assert session.get(User, admin.id).role == UserRole.ADMIN.value


def test_change_role_unknown_user_is_404(session: Session, admin: User) -> None:
    with pytest.raises(AuthNotFoundError):
        admin_user_service.change_user_role(
            session, uuid.uuid4(), role=UserRole.TEACHER.value, actor=admin
        )


def test_change_role_to_the_current_role_conflicts(
    session: Session, admin: User
) -> None:
    target = _make_user(session, role=UserRole.TEACHER.value, email="same@example.com")
    session.commit()
    with pytest.raises(AuthConflictError):
        admin_user_service.change_user_role(
            session, target.id, role=UserRole.TEACHER.value, actor=admin
        )
    session.rollback()
    assert session.get(User, target.id).role == UserRole.TEACHER.value


def test_change_role_accepts_every_vocabulary_value(session: Session, admin: User) -> None:
    roles = list(UserRole)
    for index, role in enumerate(roles):
        # Start from a *different* role so the change is a real transition.
        starting = roles[(index + 1) % len(roles)]
        target = _make_user(
            session, role=starting.value, email=f"{role.value}-x@example.com"
        )
        session.flush()
        read = admin_user_service.change_user_role(
            session, target.id, role=role.value, actor=admin
        )
        session.flush()
        assert read.role == role.value
    session.rollback()


# --- cross-role matrix on the real routes ----------------------------------------


@pytest.fixture()
def api_app(session: Session) -> FastAPI:
    """The real admin routers, mounted on a probe app with the test session."""
    from app.api.v1.endpoints.admin_users import router as admin_users_router

    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: session
    app.include_router(admin_users_router, prefix="/api/v1")
    return app


@pytest.fixture()
def api_client(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


def _token(user: User) -> dict:
    return {"Authorization": f"Bearer {security.create_access_token(user.id, user.role)}"}


def _routes(target_user_id: uuid.UUID, teacher_email: str) -> list[tuple[str, str, dict | None]]:
    return [
        ("POST", "/api/v1/admin/teachers",
         {"email": teacher_email, "full_name": "Route Teacher"}),
        ("GET", "/api/v1/admin/teachers", None),
        ("POST", f"/api/v1/admin/teachers/{target_user_id}/invite", {}),
        ("POST", f"/api/v1/admin/teachers/{target_user_id}/activate", {}),
        ("POST", f"/api/v1/admin/teachers/{target_user_id}/deactivate", {}),
        ("PATCH", f"/api/v1/admin/teachers/{target_user_id}/school", {"school_id": None}),
        ("GET", "/api/v1/admin/users", None),
        ("GET", f"/api/v1/admin/users/{target_user_id}", None),
        ("POST", f"/api/v1/admin/users/{target_user_id}/deactivate", None),
        ("POST", f"/api/v1/admin/users/{target_user_id}/activate", None),
        ("PATCH", f"/api/v1/admin/users/{target_user_id}/role", {"role": "teacher"}),
    ]


def test_every_admin_route_refuses_anonymous_callers(
    session: Session, admin: User, api_client: TestClient
) -> None:
    target, _ = _create_teacher(session, admin)
    session.commit()
    for method, path, body in _routes(target.id, f"anon-{uuid.uuid4().hex[:8]}@example.com"):
        response = api_client.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


def test_every_admin_route_refuses_students(
    session: Session, admin: User, api_client: TestClient
) -> None:
    student = _make_user(session, role=UserRole.STUDENT.value)
    target, _ = _create_teacher(session, admin)
    session.commit()
    headers = _token(student)
    for method, path, body in _routes(target.id, f"stu-{uuid.uuid4().hex[:8]}@example.com"):
        response = api_client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"


def test_every_admin_route_refuses_teachers(
    session: Session, admin: User, api_client: TestClient
) -> None:
    teacher, _ = _create_teacher(session, admin)
    teacher.status = UserStatus.ACTIVE.value
    target, _ = _create_teacher(session, admin, email="target@example.com")
    session.commit()
    headers = _token(teacher)
    for method, path, body in _routes(target.id, f"tch-{uuid.uuid4().hex[:8]}@example.com"):
        response = api_client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"


def test_admin_can_use_every_route(
    session: Session, admin: User, api_client: TestClient
) -> None:
    target, _ = _create_teacher(session, admin)
    session.commit()
    headers = _token(admin)
    for method, path, body in _routes(target.id, f"ok-{uuid.uuid4().hex[:8]}@example.com"):
        response = api_client.request(method, path, json=body, headers=headers)
        assert response.status_code not in (401, 403), (
            f"{method} {path} -> {response.status_code}: {response.text}"
        )
    session.rollback()
