"""Unit tests: registration status transitions (no PostgreSQL).

The transition map is pure data derived from the model's existing
``EnrollmentStatus`` vocabulary — no invented state. These tests pin its
shape (the whole vocabulary covered, exactly the six approved ordered
pairs, four terminal states) and prove the service behavior end to end
on in-memory SQLite: allowed moves succeed and write exactly one audit
row, terminal moves stamp ``ended_at`` within the date-order CHECK,
everything else is refused without touching the row.

Concurrency (the ``FOR UPDATE`` serialization) is proven against real
PostgreSQL in ``tests/integration/test_registration_status_api.py`` —
SQLite ignores locking clauses by design.
"""
from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.education_level import EducationLevel
from app.models.enrollment import StudentEnrollment
from app.models.enums import AcademicYearStatus, EnrollmentStatus
from app.models.pathway import Pathway
from app.models.school import School
from app.models.student import Student
from app.models.user import User
from app.services.registration_service import (
    ALLOWED_STATUS_TRANSITIONS,
    ENROLLMENT_STATUS_VALUES,
    TERMINAL_ENROLLMENT_STATUSES,
    RegistrationConflictError,
    RegistrationNotFoundError,
    RegistrationValidationError,
    update_registration_status,
)

APPROVED_PAIRS = [
    ("pending", "active"),
    ("pending", "cancelled"),
    ("active", "completed"),
    ("active", "transferred"),
    ("active", "withdrawn"),
    ("active", "cancelled"),
]

DISALLOWED_SAMPLE = [
    ("pending", "completed"),
    ("pending", "transferred"),
    ("pending", "withdrawn"),
    ("active", "pending"),
    ("completed", "active"),
    ("completed", "cancelled"),
    ("transferred", "active"),
    ("withdrawn", "active"),
    ("cancelled", "pending"),
    ("cancelled", "active"),
]


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


def _make_admin(session: Session) -> uuid.UUID:
    admin = User(
        email=f"status-admin-{uuid.uuid4().hex[:8]}@example.com", role="admin"
    )
    session.add(admin)
    session.flush()
    return admin.id


def _seed_enrollment(
    session: Session, *, status: str
) -> tuple[StudentEnrollment, uuid.UUID]:
    """Disposable student + enrollment in ``status``; returns (row, subject user id)."""
    user = User(email=f"status-kid-{uuid.uuid4().hex[:8]}@example.com", role="student")
    session.add(user)
    session.flush()
    student = Student(user_id=user.id, full_name="Status Kid")
    session.add(student)
    session.flush()
    year = AcademicYear(
        name=f"{uuid.uuid4().hex[:6]}/27",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 7, 31),
        status=AcademicYearStatus.PLANNED.value,
    )
    pathway = Pathway(code=f"P_{uuid.uuid4().hex[:6]}", name="Path")
    level = EducationLevel(
        code=f"L_{uuid.uuid4().hex[:6]}", name="Level", level_number=7
    )
    school = School(
        name="Status School", school_code=f"SS_{uuid.uuid4().hex[:6]}"
    )
    session.add_all([year, pathway, level, school])
    session.flush()
    enrollment = StudentEnrollment(
        student_id=student.id,
        academic_year_id=year.id,
        school_id=school.id,
        pathway_id=pathway.id,
        education_level_id=level.id,
        status=status,
    )
    session.add(enrollment)
    session.flush()
    return enrollment, user.id


def _audit_events(session: Session, subject_user_id: uuid.UUID) -> list[AuthEvent]:
    return list(
        session.scalars(
            select(AuthEvent).where(AuthEvent.user_id == subject_user_id)
        )
    )


# --- transition map invariants -------------------------------------------------------


def test_transition_map_covers_the_whole_vocabulary() -> None:
    assert set(ALLOWED_STATUS_TRANSITIONS) == {
        status.value for status in EnrollmentStatus
    }
    assert ENROLLMENT_STATUS_VALUES == frozenset(
        status.value for status in EnrollmentStatus
    )
    allowed_pairs = {
        (source, target)
        for source, targets in ALLOWED_STATUS_TRANSITIONS.items()
        for target in targets
    }
    assert allowed_pairs == set(APPROVED_PAIRS)
    assert len(allowed_pairs) == 6  # of 30 ordered pairs (6 states x 5 others + same)


def test_terminal_statuses_have_no_outgoing_transitions() -> None:
    assert TERMINAL_ENROLLMENT_STATUSES == {
        "completed",
        "transferred",
        "withdrawn",
        "cancelled",
    }
    for terminal in TERMINAL_ENROLLMENT_STATUSES:
        assert ALLOWED_STATUS_TRANSITIONS[terminal] == frozenset()
    assert "pending" not in TERMINAL_ENROLLMENT_STATUSES
    assert "active" not in TERMINAL_ENROLLMENT_STATUSES


# --- allowed transitions -------------------------------------------------------------


@pytest.mark.parametrize(("source", "target"), APPROVED_PAIRS)
def test_allowed_transition_succeeds_audits_and_stamps_ended_at(
    session: Session, source: str, target: str
) -> None:
    actor_id = _make_admin(session)
    enrollment, subject_user_id = _seed_enrollment(session, status=source)
    session.commit()

    updated = update_registration_status(
        session, enrollment.id, target, actor_id=actor_id
    )
    session.commit()
    session.expire_all()

    assert updated.status == target
    row = session.get(StudentEnrollment, enrollment.id)
    assert row.status == target
    if target in TERMINAL_ENROLLMENT_STATUSES:
        assert row.ended_at is not None
        assert row.ended_at >= row.started_at  # DB date-order CHECK holds
    else:
        assert row.ended_at is None

    events = _audit_events(session, subject_user_id)
    assert len(events) == 1
    assert events[0].event_type == "registration_status_changed"
    assert events[0].actor_user_id == actor_id
    assert events[0].user_id == subject_user_id
    assert json.loads(events[0].metadata_json) == {"from": source, "to": target}


# --- refusals ------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "target"), DISALLOWED_SAMPLE)
def test_disallowed_transition_is_409_naming_both_statuses(
    session: Session, source: str, target: str
) -> None:
    actor_id = _make_admin(session)
    enrollment, subject_user_id = _seed_enrollment(session, status=source)
    session.commit()

    with pytest.raises(RegistrationConflictError) as excinfo:
        update_registration_status(
            session, enrollment.id, target, actor_id=actor_id
        )
    session.rollback()
    session.expire_all()

    detail = str(excinfo.value)
    assert source in detail and target in detail, detail
    row = session.get(StudentEnrollment, enrollment.id)
    assert row.status == source
    assert row.ended_at is None
    assert _audit_events(session, subject_user_id) == []


def test_same_status_is_refused(session: Session) -> None:
    actor_id = _make_admin(session)
    enrollment, subject_user_id = _seed_enrollment(session, status="active")
    session.commit()

    with pytest.raises(RegistrationConflictError) as excinfo:
        update_registration_status(
            session, enrollment.id, "active", actor_id=actor_id
        )
    session.rollback()
    session.expire_all()

    assert "already" in str(excinfo.value)
    assert session.get(StudentEnrollment, enrollment.id).status == "active"
    assert _audit_events(session, subject_user_id) == []


def test_unknown_enrollment_is_404(session: Session) -> None:
    actor_id = _make_admin(session)
    session.commit()

    with pytest.raises(RegistrationNotFoundError):
        update_registration_status(session, uuid.uuid4(), "active", actor_id=actor_id)
    session.rollback()


def test_status_outside_the_vocabulary_is_422(session: Session) -> None:
    actor_id = _make_admin(session)
    enrollment, _ = _seed_enrollment(session, status="pending")
    session.commit()

    with pytest.raises(RegistrationValidationError):
        update_registration_status(
            session, enrollment.id, "exploded", actor_id=actor_id
        )
    session.rollback()
    session.expire_all()
    assert session.get(StudentEnrollment, enrollment.id).status == "pending"
