"""Unit tests: teacher verification — self-service registration + admin vetting.

Phase 1 slice 2 — two services, one axis:

- ``POST /auth/register-teacher`` (``register_teacher_account``): a public
  signup that creates user + profile atomically, forces the role server-side
  to ``teacher``, issues tokens, and starts the profile ``pending``. The
  *account* is ``active`` immediately; vetting is what gates publishing, and
  the two axes stay independent;
- ``PATCH /admin/teachers/{id}/verification`` (``set_teacher_verification``):
  an administrator moves ``verification_status`` through
  pending/approved/rejected/suspended. A repeat of the current value is a
  409; an accepted change writes exactly one audit event naming the actor;
  the account status is deliberately untouched.

In-memory SQLite, no HTTP except where noted.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.auth_event import AuthEvent
from app.models.enums import TeacherVerificationStatus, UserRole, UserStatus
from app.models.student import Student
from app.models.teacher import Teacher
from app.models.user import User
from app.schemas.auth import TeacherAccountCreate
from app.schemas.teacher_admin import TeacherVerificationUpdate
from app.services import admin_user_service, auth_service
from app.services.auth_service import (
    AuthConflictError,
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
    session.commit()
    return user


def _teacher(session: Session, *, verification: str, email: str | None = None) -> User:
    user = User(
        email=email or f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(
            user_id=user.id,
            full_name="Test Teacher",
            verification_status=verification,
        )
    )
    session.commit()
    return user


def _profile(session: Session, user: User) -> Teacher:
    return session.scalar(select(Teacher).where(Teacher.user_id == user.id))


def _payload(**overrides) -> TeacherAccountCreate:
    values = {
        "email": f"new-teacher-{uuid.uuid4().hex[:8]}@example.com",
        "password": PASSWORD,
        "full_name": "New Teacher",
    }
    values.update(overrides)
    return TeacherAccountCreate(**values)


# --- register_teacher_account -----------------------------------------------------------


def test_register_teacher_creates_active_account_pending_profile_and_tokens(
    session: Session,
) -> None:
    tokens = auth_service.register_teacher_account(session, _payload())
    session.commit()

    user = session.scalar(
        select(User).where(User.email.like("new-teacher-%@example.com"))
    )
    assert user is not None
    assert user.role == UserRole.TEACHER.value
    # account can log in right away; vetting is a separate axis
    assert user.status == UserStatus.ACTIVE.value

    profile = _profile(session, user)
    assert profile is not None
    assert profile.full_name == "New Teacher"
    assert profile.verification_status == TeacherVerificationStatus.PENDING.value

    assert tokens.access_token
    assert tokens.refresh_token

    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == user.id)
    ).all()
    assert [e.event_type for e in events] == ["teacher_verification_submitted"]


def test_register_teacher_keeps_users_phone_none_even_when_profile_carries_one(
    session: Session,
) -> None:
    """The phone lives on the profile; ``users.phone`` stays the legacy
    identity-column null (it never carried teacher profile data)."""
    payload = _payload(phone="+256700000001", subject="Mathematics")
    auth_service.register_teacher_account(session, payload)
    session.commit()

    user = session.scalar(
        select(User).where(User.email.like("new-teacher-%@example.com"))
    )
    profile = _profile(session, user)
    assert user.phone is None
    assert profile.phone == "+256700000001"
    assert profile.subject == "Mathematics"


def test_register_teacher_rejects_duplicate_email_and_policy_violating_password(
    session: Session,
) -> None:
    taken = _payload(email="taken@example.com")
    auth_service.register_teacher_account(session, taken)
    session.commit()

    with pytest.raises(AuthConflictError) as duplicate:
        auth_service.register_teacher_account(
            session, _payload(email="TAKEN@example.com")
        )
    assert duplicate.value.status_code == 409

    # The schema rejects a short password at construction; the service
    # re-validates the same policy for anything that slipped past a
    # client. Mutate the attribute after construction to reach that path.
    weak = _payload(email="weak@example.com")
    weak.password = "short"
    with pytest.raises(AuthValidationError) as weak_exc:
        auth_service.register_teacher_account(session, weak)
    assert weak_exc.value.status_code == 422
    # nothing was left behind by either refusal
    assert (
        session.scalar(
            select(User).where(User.email == "weak@example.com")
        )
        is None
    )


def test_register_teacher_role_is_forced_server_side(session: Session) -> None:
    """A role smuggled into the payload must not survive — the schema
    forbids the key, and the service hardcodes ``teacher`` regardless."""
    with pytest.raises(Exception) as excinfo:
        TeacherAccountCreate(**{
            "email": "sneaky@example.com",
            "password": PASSWORD,
            "full_name": "Sneaky",
            "role": "admin",
        })
    assert "extra" in str(excinfo.value).lower() or "forbidden" in str(
        excinfo.value
    ).lower()

    auth_service.register_teacher_account(
        session,
        _payload(email="forced@example.com"),
    )
    session.commit()
    user = session.scalar(select(User).where(User.email == "forced@example.com"))
    assert user.role == UserRole.TEACHER.value


# --- set_teacher_verification -----------------------------------------------------------


def test_admin_moves_pending_to_approved_and_audit_names_the_actor(
    session: Session, admin: User
) -> None:
    teacher = _teacher(session, verification=TeacherVerificationStatus.PENDING.value)

    read = admin_user_service.set_teacher_verification(
        session,
        teacher.id,
        TeacherVerificationUpdate(status=TeacherVerificationStatus.APPROVED),
        actor=admin,
    )
    session.commit()

    assert read.verification_status == TeacherVerificationStatus.APPROVED.value
    assert read.email == teacher.email
    assert _profile(session, teacher).verification_status == (
        TeacherVerificationStatus.APPROVED.value
    )
    # vetting does not touch the account axis
    session.refresh(teacher)
    assert teacher.status == UserStatus.ACTIVE.value

    event = session.scalar(
        select(AuthEvent).where(AuthEvent.user_id == teacher.id)
    )
    assert event is not None
    assert event.event_type == "teacher_verification_approved"
    assert event.actor_user_id == admin.id


def test_every_status_value_is_reachable_including_reopen_to_pending(
    session: Session, admin: User
) -> None:
    """One linear walk proves each value is a legal *move* from the last:
    approved → rejected → suspended → pending (reopen) → approved."""
    teacher = _teacher(session, verification=TeacherVerificationStatus.PENDING.value)

    for status in (
        TeacherVerificationStatus.APPROVED,
        TeacherVerificationStatus.REJECTED,
        TeacherVerificationStatus.SUSPENDED,
        TeacherVerificationStatus.PENDING,
        TeacherVerificationStatus.APPROVED,
    ):
        read = admin_user_service.set_teacher_verification(
            session,
            teacher.id,
            TeacherVerificationUpdate(status=status),
            actor=admin,
        )
        session.commit()
        assert read.verification_status == status.value


def test_repeating_the_current_verification_value_is_a_conflict(
    session: Session, admin: User
) -> None:
    teacher = _teacher(session, verification=TeacherVerificationStatus.APPROVED.value)

    with pytest.raises(AuthConflictError) as excinfo:
        admin_user_service.set_teacher_verification(
            session,
            teacher.id,
            TeacherVerificationUpdate(status=TeacherVerificationStatus.APPROVED),
            actor=admin,
        )
    assert excinfo.value.status_code == 409
    assert "already" in str(excinfo.value)

    session.rollback()
    assert (
        _profile(session, teacher).verification_status
        == TeacherVerificationStatus.APPROVED.value
    )


def test_verification_refuses_unknown_and_non_teacher_targets(
    session: Session, admin: User
) -> None:
    student = User(
        email=f"student-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(student)
    session.flush()
    session.add(Student(user_id=student.id, full_name="Test Student"))
    session.commit()

    for target in (uuid.uuid4(), student.id):
        with pytest.raises(AuthNotFoundError) as excinfo:
            admin_user_service.set_teacher_verification(
                session,
                target,
                TeacherVerificationUpdate(status=TeacherVerificationStatus.APPROVED),
                actor=admin,
            )
        assert excinfo.value.status_code == 404


def test_verification_schema_rejects_unknown_values_and_extra_keys(
    session: Session,
) -> None:
    with pytest.raises(Exception):
        TeacherVerificationUpdate(status="definitely-not-a-status")
    with pytest.raises(Exception):
        TeacherVerificationUpdate(status=TeacherVerificationStatus.APPROVED, role="admin")
