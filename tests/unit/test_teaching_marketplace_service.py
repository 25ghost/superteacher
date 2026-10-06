"""Unit tests: teaching offerings, marketplace discovery, learning enrollments.

In-memory SQLite, no HTTP — the service layer's own contract:

- only an **approved** teacher may publish (verification is a real gate,
  not decoration), and the context is validated against the catalog
  (pathway spans the level, program version belongs to the tuple, the
  subject is taught by it) before anything is written;
- one *active* offering per (teacher, context): a duplicate is a 409;
- the marketplace shows only ``active`` offers by ``approved`` teachers —
  pausing an offer or suspending a teacher hides it immediately;
- one *active* enrollment per (student, learning context): the second
  offering of the context you are already in is refused until you leave;
- leaving stamps ``ended_at`` and frees the slot for another offering;
- a foreign id 404s with the *same* message as an unknown id (L6).
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    LearningEnrollmentStatus,
    TeacherVerificationStatus,
    TeachingOfferingStatus,
    UserRole,
    UserStatus,
)
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.student import Student
from app.models.subject import Subject
from app.models.teacher import Teacher
from app.models.user import User
from app.schemas.learning import (
    TeachingOfferingCreate,
    TeachingOfferingUpdate,
)
from app.services import (
    learning_enrollment_service,
    teaching_offering_service,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
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
def catalog(session: Session) -> dict:
    """A minimal coherent catalog: year + pathway + level + program + subject."""
    year = AcademicYear(
        name="2026/2027",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 6, 30),
        status=AcademicYearStatus.ACTIVE.value,
    )
    pathway = Pathway(code="O_LEVEL", name="O Level")
    level = EducationLevel(code="S3", name="Senior 3", level_number=3)
    session.add_all([year, pathway, level])
    session.flush()
    session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

    program = Program(code="COMB", name="Combination", program_type="combination")
    session.add(program)
    session.flush()
    program_version = ProgramVersion(
        program_id=program.id,
        academic_year_id=year.id,
        pathway_id=pathway.id,
        education_level_id=level.id,
        code="COMB-2026",
        name="Combination 2026",
    )
    session.add(program_version)

    maths = Subject(code="MATH", name="Mathematics")
    chem = Subject(code="CHEM", name="Chemistry")
    session.add_all([maths, chem])
    session.flush()
    session.add(
        ProgramSubject(
            program_version_id=program_version.id,
            subject_id=maths.id,
        )
    )
    session.commit()
    return {
        "year": year,
        "pathway": pathway,
        "level": level,
        "program_version": program_version,
        "maths": maths,
        "chem": chem,
    }


def _teacher_user(
    session: Session, *, verification: str, email: str | None = None
) -> User:
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


@pytest.fixture()
def approved_teacher(session: Session) -> User:
    return _teacher_user(session, verification=TeacherVerificationStatus.APPROVED.value)


@pytest.fixture()
def pending_teacher(session: Session) -> User:
    return _teacher_user(session, verification=TeacherVerificationStatus.PENDING.value)


@pytest.fixture()
def student(session: Session) -> User:
    user = User(
        email=f"student-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(Student(user_id=user.id, full_name="Test Student"))
    session.commit()
    return user


def _student_profile(session: Session, user: User) -> Student:
    return session.scalar(select(Student).where(Student.user_id == user.id))


def _teacher_profile(session: Session, user: User) -> Teacher:
    return session.scalar(select(Teacher).where(Teacher.user_id == user.id))


def _payload(catalog: dict, *, subject_key: str = "maths", **overrides) -> TeachingOfferingCreate:
    values = {
        "academic_year_id": catalog["year"].id,
        "pathway_id": catalog["pathway"].id,
        "education_level_id": catalog["level"].id,
        "program_version_id": catalog["program_version"].id,
        "subject_id": catalog[subject_key].id,
        "description": "Algebra and geometry",
    }
    values.update(overrides)
    return TeachingOfferingCreate(**values)


# --- publishing -------------------------------------------------------------------------


def test_unverified_teacher_cannot_publish(
    session: Session, catalog: dict, pending_teacher: User
) -> None:
    with pytest.raises(LearningForbiddenError) as excinfo:
        teaching_offering_service.create_offering(
            session, pending_teacher, _payload(catalog)
        )
    assert excinfo.value.status_code == 403
    assert "verification" in str(excinfo.value)


def test_approved_teacher_publishes_and_context_is_shared(
    session: Session, catalog: dict, approved_teacher: User
) -> None:
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()

    assert read.status == TeachingOfferingStatus.ACTIVE.value
    assert read.teacher_name == "Test Teacher"
    assert read.subject_code == "MATH"
    assert read.program_code == "COMB"
    assert read.description == "Algebra and geometry"
    assert read.academic_year == "2026/2027"

    from app.models.learning_context import LearningContext

    contexts = session.scalars(select(LearningContext)).all()
    assert len(contexts) == 1

    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
    ).all()
    assert [e.event_type for e in events] == ["teaching_offering_created"]


def test_duplicate_active_offering_for_same_context_is_refused(
    session: Session, catalog: dict, approved_teacher: User
) -> None:
    teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        teaching_offering_service.create_offering(
            session, approved_teacher, _payload(catalog)
        )
    assert "active offering" in str(excinfo.value)

    # ...but a different teacher may offer the same context.
    other = _teacher_user(session, verification=TeacherVerificationStatus.APPROVED.value)
    read = teaching_offering_service.create_offering(session, other, _payload(catalog))
    assert read.learning_context_id is not None
    session.commit()


def test_incoherent_context_is_refused_before_any_write(
    session: Session, catalog: dict, approved_teacher: User
) -> None:
    with pytest.raises(LearningValidationError) as excinfo:
        teaching_offering_service.create_offering(
            session,
            approved_teacher,
            _payload(catalog, subject_key="chem"),  # not taught by COMB
        )
    assert "not part of program version" in str(excinfo.value)

    with pytest.raises(LearningNotFoundError):
        teaching_offering_service.create_offering(
            session,
            approved_teacher,
            _payload(catalog, education_level_id=uuid.uuid4()),
        )

    with pytest.raises(LearningNotFoundError):
        teaching_offering_service.create_offering(
            session, approved_teacher, _payload(catalog, pathway_id=uuid.uuid4())
        )


def test_offering_status_transitions_and_archived_is_terminal(
    session: Session, catalog: dict, approved_teacher: User
) -> None:
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()
    offering_id = read.offering_id

    paused = teaching_offering_service.update_my_offering(
        session,
        approved_teacher,
        offering_id,
        TeachingOfferingUpdate(status=TeachingOfferingStatus.PAUSED),
    )
    assert paused.status == "paused"

    resumed = teaching_offering_service.update_my_offering(
        session,
        approved_teacher,
        offering_id,
        TeachingOfferingUpdate(status=TeachingOfferingStatus.ACTIVE),
    )
    assert resumed.status == "active"

    archived = teaching_offering_service.update_my_offering(
        session,
        approved_teacher,
        offering_id,
        TeachingOfferingUpdate(status=TeachingOfferingStatus.ARCHIVED),
    )
    assert archived.status == "archived"

    with pytest.raises(LearningConflictError) as excinfo:
        teaching_offering_service.update_my_offering(
            session,
            approved_teacher,
            offering_id,
            TeachingOfferingUpdate(status=TeachingOfferingStatus.ACTIVE),
        )
    assert "cannot move" in str(excinfo.value)

    with pytest.raises(LearningConflictError) as excinfo:
        teaching_offering_service.update_my_offering(
            session,
            approved_teacher,
            offering_id,
            TeachingOfferingUpdate(status=TeachingOfferingStatus.ARCHIVED),
        )
    assert "already" in str(excinfo.value)


def test_description_update_clears_and_writes_the_updated_audit_event(
    session: Session, catalog: dict, approved_teacher: User
) -> None:
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()

    updated = teaching_offering_service.update_my_offering(
        session,
        approved_teacher,
        read.offering_id,
        TeachingOfferingUpdate(description=None),
    )
    assert updated.description is None

    event_types = [
        e.event_type
        for e in session.scalars(
            select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
        )
    ]
    assert event_types == [
        "teaching_offering_created",
        "teaching_offering_updated",
    ]


def test_foreign_offering_id_404s_like_an_unknown_id(
    session: Session, catalog: dict, approved_teacher: User
) -> None:
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()
    other = _teacher_user(session, verification=TeacherVerificationStatus.APPROVED.value)

    unknown = uuid.uuid4()
    with pytest.raises(LearningNotFoundError) as foreign:
        teaching_offering_service.get_my_offering(session, other, read.offering_id)
    with pytest.raises(LearningNotFoundError) as unknown_exc:
        teaching_offering_service.get_my_offering(session, other, unknown)
    # L6: a foreign id must be indistinguishable from an unknown one —
    # same status, same message *shape*, no hint that the row exists.
    for excinfo in (foreign, unknown_exc):
        assert excinfo.value.status_code == 404
        message = str(excinfo.value)
        assert message.startswith("no teaching offering with id ")
        assert not any(word in message for word in ("teacher", "not yours", "other"))


# --- marketplace ------------------------------------------------------------------------


def test_marketplace_lists_active_offerings_of_approved_teachers(
    session: Session, catalog: dict, approved_teacher: User
) -> None:
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()

    listed = teaching_offering_service.list_marketplace_offerings(session)
    assert [o.offering_id for o in listed] == [read.offering_id]

    # Pausing hides the offering without touching the row.
    teaching_offering_service.update_my_offering(
        session,
        approved_teacher,
        read.offering_id,
        TeachingOfferingUpdate(status=TeachingOfferingStatus.PAUSED),
    )
    session.commit()
    assert teaching_offering_service.list_marketplace_offerings(session) == []

    # ...and so does suspending the teacher's verification.
    teaching_offering_service.update_my_offering(
        session,
        approved_teacher,
        read.offering_id,
        TeachingOfferingUpdate(status=TeachingOfferingStatus.ACTIVE),
    )
    _teacher_profile(session, approved_teacher).verification_status = (
        TeacherVerificationStatus.SUSPENDED.value
    )
    session.commit()
    assert teaching_offering_service.list_marketplace_offerings(session) == []

    _teacher_profile(session, approved_teacher).verification_status = (
        TeacherVerificationStatus.APPROVED.value
    )
    session.commit()
    assert len(teaching_offering_service.list_marketplace_offerings(session)) == 1


def test_marketplace_filters_by_subject(
    session: Session, catalog: dict, approved_teacher: User
) -> None:
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()

    assert (
        teaching_offering_service.list_marketplace_offerings(
            session, subject_id=catalog["maths"].id
        )
        != []
    )
    assert (
        teaching_offering_service.list_marketplace_offerings(
            session, subject_id=uuid.uuid4()
        )
        == []
    )
    assert read.subject_code == "MATH"


# --- enrollments -------------------------------------------------------------------------


def test_enroll_leave_and_switch_within_one_context(
    session: Session, catalog: dict, approved_teacher: User, student: User
) -> None:
    profile = _student_profile(session, student)
    first = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()

    enrollment = learning_enrollment_service.enroll(
        session, profile, first.offering_id
    )
    session.commit()
    assert enrollment.status == LearningEnrollmentStatus.ACTIVE.value
    assert enrollment.subject_code == "MATH"
    assert enrollment.teacher_name == "Test Teacher"
    assert enrollment.ended_at is None

    # A second teacher publishes the same context: the student cannot hold
    # two active memberships of that context.
    other = _teacher_user(session, verification=TeacherVerificationStatus.APPROVED.value)
    second = teaching_offering_service.create_offering(session, other, _payload(catalog))
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        learning_enrollment_service.enroll(session, profile, second.offering_id)
    assert "leave it before enrolling" in str(excinfo.value)

    ended = learning_enrollment_service.leave(session, profile, enrollment.enrollment_id)
    session.commit()
    assert ended.status == LearningEnrollmentStatus.ENDED.value
    assert ended.ended_at is not None

    # Leaving again is a 409...
    with pytest.raises(LearningConflictError):
        learning_enrollment_service.leave(session, profile, enrollment.enrollment_id)

    # ...and now the slot is free for the other teacher's offering.
    switched = learning_enrollment_service.enroll(session, profile, second.offering_id)
    assert switched.teacher_name == "Test Teacher"
    assert switched.offering_status == "active"


def test_enroll_refuses_paused_or_archived_offerings(
    session: Session, catalog: dict, approved_teacher: User, student: User
) -> None:
    profile = _student_profile(session, student)
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()
    teaching_offering_service.update_my_offering(
        session,
        approved_teacher,
        read.offering_id,
        TeachingOfferingUpdate(status=TeachingOfferingStatus.PAUSED),
    )
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        learning_enrollment_service.enroll(session, profile, read.offering_id)
    assert "not accepting enrollments" in str(excinfo.value)


def test_unknown_offering_id_404s_on_enroll(
    session: Session, student: User
) -> None:
    profile = _student_profile(session, student)
    with pytest.raises(LearningNotFoundError) as excinfo:
        learning_enrollment_service.enroll(session, profile, uuid.uuid4())
    assert excinfo.value.status_code == 404


def test_foreign_enrollment_id_404s_like_an_unknown_id(
    session: Session, catalog: dict, approved_teacher: User, student: User
) -> None:
    profile = _student_profile(session, student)
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()
    enrollment = learning_enrollment_service.enroll(session, profile, read.offering_id)
    session.commit()

    other_user = User(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(other_user)
    session.flush()
    other_profile = Student(user_id=other_user.id, full_name="Other Student")
    session.add(other_profile)
    session.commit()

    with pytest.raises(LearningNotFoundError) as foreign:
        learning_enrollment_service.get_my_enrollment(
            session, other_profile, enrollment.enrollment_id
        )
    with pytest.raises(LearningNotFoundError) as unknown:
        learning_enrollment_service.get_my_enrollment(
            session, other_profile, uuid.uuid4()
        )
    for excinfo in (foreign, unknown):
        assert excinfo.value.status_code == 404
        message = str(excinfo.value)
        assert message.startswith("no learning enrollment with id ")
        assert not any(word in message for word in ("student", "not yours", "other"))

    with pytest.raises(LearningNotFoundError):
        learning_enrollment_service.leave(
            session, other_profile, enrollment.enrollment_id
        )


def test_learning_history_lists_active_and_ended(
    session: Session, catalog: dict, approved_teacher: User, student: User
) -> None:
    profile = _student_profile(session, student)
    read = teaching_offering_service.create_offering(
        session, approved_teacher, _payload(catalog)
    )
    session.commit()
    enrollment = learning_enrollment_service.enroll(session, profile, read.offering_id)
    learning_enrollment_service.leave(session, profile, enrollment.enrollment_id)
    session.commit()

    history = learning_enrollment_service.list_my_enrollments(session, profile)
    assert [e.enrollment_id for e in history] == [enrollment.enrollment_id]
    assert history[0].status == LearningEnrollmentStatus.ENDED.value
