"""Teacher↔student visibility: what a teacher may see, and nothing else.

MVP rules exercised here (service layer, in-memory SQLite, no HTTP):

- a teacher's students exist only through ``teacher → own offering →
  enrollment → student``; the roster of one offering is its ACTIVE
  ``learning_enrollments`` rows and never a directory;
- a foreign offering id answers the same 404 as an unknown one (L6);
- only the teacher role can use the roster (403 for everyone else);
- reads do not require an approved verification (authoring does);
- no messaging, payments, ratings, assignments or global student
  search exists anywhere in the product yet — asserted against the
  live route table so a later addition has to change this test on
  purpose.
"""
from __future__ import annotations

import inspect
import uuid
from datetime import date, datetime

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.router import api_router
from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    LearningEnrollmentStatus,
    MaterialType,
    TeacherVerificationStatus,
    UserRole,
    UserStatus,
)
from app.models.learning_enrollment import LearningEnrollment
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.student import Student
from app.models.subject import Subject
from app.models.teacher import Teacher
from app.models.teaching_offering import TeachingOffering
from app.models.user import User
from app.repositories import learning_enrollment_repository as enrollment_repo
from app.schemas.learning import OfferingStudentRead, TeachingOfferingCreate
from app.services import teaching_offering_service
from app.services.learning_context_service import (
    LearningForbiddenError,
    LearningNotFoundError,
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
    session.add(maths)
    session.flush()
    session.add(
        ProgramSubject(program_version_id=program_version.id, subject_id=maths.id)
    )
    session.commit()
    return {
        "year": year,
        "pathway": pathway,
        "level": level,
        "program_version": program_version,
        "maths": maths,
    }


def _teacher(
    session: Session, *, full_name: str, verification: str
) -> User:
    user = User(
        email=f"{full_name.lower()}-{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(
            user_id=user.id,
            full_name=full_name,
            verification_status=verification,
        )
    )
    session.commit()
    return user


def _student(session: Session, *, full_name: str) -> User:
    user = User(
        email=f"{full_name.lower().split()[0]}-{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(Student(user_id=user.id, full_name=full_name))
    session.commit()
    return user


def _student_profile(session: Session, user: User) -> Student:
    return session.scalar(select(Student).where(Student.user_id == user.id))


def _other_actor(session: Session, role: UserRole) -> User:
    user = User(
        email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.com",
        role=role.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.commit()
    return user


def _offering(session: Session, catalog: dict, teacher: User) -> uuid.UUID:
    read = teaching_offering_service.create_offering(
        session,
        teacher,
        TeachingOfferingCreate(
            academic_year_id=catalog["year"].id,
            pathway_id=catalog["pathway"].id,
            education_level_id=catalog["level"].id,
            program_version_id=catalog["program_version"].id,
            subject_id=catalog["maths"].id,
        ),
    )
    session.commit()
    return read.offering_id


def _enroll(
    session: Session, student_user: User, offering_id: uuid.UUID, started_at: datetime
) -> LearningEnrollment:
    student = session.scalar(select(Student).where(Student.user_id == student_user.id))
    offering = session.get(TeachingOffering, offering_id)
    enrollment = enrollment_repo.create(
        session,
        student_id=student.id,
        teaching_offering_id=offering.id,
        learning_context_id=offering.learning_context_id,
    )
    enrollment.started_at = started_at
    session.commit()
    return enrollment


@pytest.fixture()
def alice(session: Session) -> User:
    return _teacher(
        session,
        full_name="Alice Mwangi",
        verification=TeacherVerificationStatus.APPROVED.value,
    )


@pytest.fixture()
def brian(session: Session) -> User:
    return _teacher(
        session,
        full_name="Brian Okello",
        verification=TeacherVerificationStatus.APPROVED.value,
    )


@pytest.fixture()
def alice_offering(session: Session, catalog: dict, alice: User) -> uuid.UUID:
    return _offering(session, catalog, alice)


@pytest.fixture()
def brian_offering(session: Session, catalog: dict, brian: User) -> uuid.UUID:
    return _offering(session, catalog, brian)


@pytest.fixture()
def roster(session: Session, alice_offering: uuid.UUID) -> dict[str, LearningEnrollment]:
    """Two ACTIVE students of Alice's offering, one ENDED, one of Brian's."""
    ana = _student(session, full_name="Ana Njeri")
    bob = _student(session, full_name="Bob Kato")
    carol = _student(session, full_name="Carol Adhiambo")
    dave = _student(session, full_name="Dave Otieno")

    ena = _enroll(
        session, ana, alice_offering, datetime(2026, 9, 1, 8, 0, 0)
    )
    ebob = _enroll(
        session, bob, alice_offering, datetime(2026, 9, 15, 8, 0, 0)
    )
    ecarol = _enroll(
        session, carol, alice_offering, datetime(2026, 9, 10, 8, 0, 0)
    )
    ecarol.status = LearningEnrollmentStatus.ENDED.value
    session.commit()

    return {
        "ana": ana,
        "bob": bob,
        "carol": carol,
        "dave": dave,
        "ena": ena,
        "ebob": ebob,
        "ecarol": ecarol,
    }


# --- the roster itself --------------------------------------------------------------------


def test_roster_is_one_offerings_active_students_newest_first(
    session: Session, alice: User, alice_offering: uuid.UUID, roster: dict
) -> None:
    rows = teaching_offering_service.list_offering_students(
        session, alice, alice_offering
    )

    # Newest ACTIVE enrollment first; the ENDED one is not in the roster.
    assert [r.enrollment_id for r in rows] == [
        roster["ebob"].id,
        roster["ena"].id,
    ]
    assert {r.student_id for r in rows} == {
        _student_profile(session, roster["ana"]).id,
        _student_profile(session, roster["bob"]).id,
    }

    bob_row = rows[0]
    assert bob_row.full_name == "Bob Kato"
    assert bob_row.email == roster["bob"].email
    assert bob_row.enrollment_status == LearningEnrollmentStatus.ACTIVE.value
    assert bob_row.started_at is not None
    assert bob_row.ended_at is None
    assert bob_row.enrollment_id == roster["ebob"].id


def test_students_of_another_offering_are_invisible_to_this_teacher(
    session: Session,
    alice: User,
    brian: User,
    alice_offering: uuid.UUID,
    brian_offering: uuid.UUID,
    roster: dict,
) -> None:
    # Dave joins Brian's offering only.
    _enroll(session, roster["dave"], brian_offering, datetime(2026, 9, 20, 8, 0, 0))

    rows = teaching_offering_service.list_offering_students(
        session, alice, alice_offering
    )
    assert len(rows) == 2
    dave_profile = _student_profile(session, roster["dave"])
    assert dave_profile.id not in {r.student_id for r in rows}


def test_foreign_and_unknown_offering_ids_answer_the_same_404(
    session: Session, alice: User, brian_offering: uuid.UUID
) -> None:
    unknown = uuid.uuid4()
    messages = []
    for target in (brian_offering, unknown):
        with pytest.raises(LearningNotFoundError) as excinfo:
            teaching_offering_service.list_offering_students(session, alice, target)
        messages.append(str(excinfo.value))

    # Same shape for both: the id named, nothing else about the owner.
    assert messages[0] == f"no teaching offering with id {brian_offering}"
    assert messages[1] == f"no teaching offering with id {unknown}"
    for word in ("brian", "other", "not yours", "teacher"):
        assert word not in messages[0]


def test_non_teachers_are_refused_before_any_lookup(
    session: Session, catalog: dict, alice_offering: uuid.UUID, roster: dict
) -> None:
    student = roster["ana"]
    for role in (UserRole.STUDENT, UserRole.ADMIN):
        caller = _other_actor(session, role)
        with pytest.raises(LearningForbiddenError):
            teaching_offering_service.list_offering_students(
                session, caller, alice_offering
            )
        # Same answer for a foreign id: the guard fires first.
        with pytest.raises(LearningForbiddenError):
            teaching_offering_service.list_offering_students(
                session, caller, uuid.uuid4()
            )


def test_reads_do_not_require_an_approved_verification(
    session: Session, alice: User, alice_offering: uuid.UUID, roster: dict
) -> None:
    profile = session.scalar(select(Teacher).where(Teacher.user_id == alice.id))
    profile.verification_status = TeacherVerificationStatus.SUSPENDED.value
    session.commit()

    rows = teaching_offering_service.list_offering_students(
        session, alice, alice_offering
    )
    assert len(rows) == 2  # visibility is not a content write


def test_the_roster_payload_cannot_grow_into_a_profile(
    session: Session, alice: User, alice_offering: uuid.UUID, roster: dict
) -> None:
    """Exactly these fields exist — no phone, notes, scores or search keys."""
    assert set(OfferingStudentRead.model_fields) == {
        "enrollment_id",
        "student_id",
        "full_name",
        "email",
        "enrollment_status",
        "started_at",
        "ended_at",
    }

    # The service takes no student id, name, email or page/window either.
    signature = inspect.signature(teaching_offering_service.list_offering_students)
    assert list(signature.parameters) == ["session", "user", "offering_id"]

    rows = teaching_offering_service.list_offering_students(
        session, alice, alice_offering
    )
    payload = rows[0].model_dump()
    assert set(payload) == set(OfferingStudentRead.model_fields)
    for leak in ("student_number", "phone", "notes", "progress", "role"):
        assert leak not in payload


def test_the_repository_query_never_widens_past_its_offering(
    session: Session, alice_offering: uuid.UUID, brian_offering: uuid.UUID, roster: dict
) -> None:
    # Dave joins Brian's offering, so both offerings have rows.
    _enroll(session, roster["dave"], brian_offering, datetime(2026, 9, 20, 8, 0, 0))

    alice_rows = enrollment_repo.list_active_students_for_offering(
        session, alice_offering
    )
    brian_rows = enrollment_repo.list_active_students_for_offering(
        session, brian_offering
    )
    assert len(alice_rows) == 2
    assert len(brian_rows) == 1
    assert brian_rows[0].student_id == _student_profile(session, roster["dave"]).id
    assert {r.id for r in alice_rows}.isdisjoint({r.id for r in brian_rows})
    assert all(r.teaching_offering_id == alice_offering for r in alice_rows)
    assert all(r.teaching_offering_id == brian_offering for r in brian_rows)
    # An offering nobody joined yields an empty list, not a wildcard result.
    empty = enrollment_repo.list_active_students_for_offering(session, uuid.uuid4())
    assert empty == []


# --- MVP boundary: the product has no messaging / commerce / discovery -------------------


def _v1_route_table() -> list[tuple[str, str]]:
    return sorted(
        (method, route.path)
        for route in api_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in ("HEAD", "OPTIONS")
    )


def test_students_are_only_reachable_through_their_own_offering() -> None:
    """No bare ``/students`` directory for teachers; no student id input."""
    student_paths = [
        path for _, path in _v1_route_table() if "student" in path and "teacher" in path
    ]
    assert student_paths == [
        "/me/teacher/offerings/{offering_id}/students",
    ]
    # ...and nothing else feeds a student id to a teacher namespace.
    for method, path in _v1_route_table():
        if path.startswith("/me/teacher") and "{student" in path:
            raise AssertionError(f"{method} {path} exposes a per-student path")


def test_no_messaging_payments_ratings_or_assignments_exist() -> None:
    banned_segments = (
        "message",
        "conversation",
        "chat",
        "thread",
        "payment",
        "price",
        "purchase",
        "checkout",
        "rating",
        "review",
        "assignment",
        "homework",
        "quiz",
        "feedback",
        "search",
        "directory",
    )
    for method, path in _v1_route_table():
        for banned in banned_segments:
            assert banned not in path, f"{method} {path} introduces '{banned}'"


def test_the_material_vocabulary_is_video_and_pdf_only() -> None:
    assert {m.value for m in MaterialType} == {"video", "pdf_document"}
