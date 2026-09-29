"""Unit tests: registration service (no PostgreSQL).

Service tests run on in-memory SQLite through the real ORM models,
proving the Phase 5D validation rules end to end:

- student / academic-year / pathway / level resolution (404 mapping),
- pathway ↔ level validation through the real pathway_levels rows,
- program-version context validation against the four-column offering
  tuple (program, academic_year, pathway, education_level),
- school-offering validation through real school_programs rows,
- duplicate-enrollment handling (409) matching the DB unique constraint,
- initial enrollment status (``pending`` — the model's vocabulary),
- student-subject derivation strictly from program_subjects,
- transaction rollback: a failure after the enrollment insert leaves
  no enrollment and no student-subject rows.

The dev-catalog limitation (empty programs/TVET/schools) is honored:
tests create disposable rows via the ORM, never via the seed datasets.
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.academic_year import AcademicYear
from app.models.education_level import EducationLevel
from app.models.enrollment import StudentEnrollment
from app.models.enums import AcademicYearStatus, EnrollmentStatus
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.school import School
from app.models.school_program import SchoolProgram
from app.models.student import Student
from app.models.student_subject import StudentSubject
from app.models.subject import Subject
from app.models.user import User
from app.repositories.enrollment_repository import subject_ids_for_program_version
from app.schemas.registration import RegistrationCreate
from app.services import registration_service as svc
from app.services.registration_service import (
    RegistrationConflictError,
    RegistrationNotFoundError,
    RegistrationUnavailableError,
    RegistrationValidationError,
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


# --- disposable catalog context (ORM rows, never seed data) ---------------------


def _make_student(session: Session) -> Student:
    user = User(email=f"s{uuid.uuid4().hex[:8]}@example.com", role="student")
    session.add(user)
    session.flush()
    student = Student(user_id=user.id, full_name="Aline Uwase")
    session.add(student)
    session.flush()
    return student


class Catalog:
    """Minimal verified-shaped context: O_LEVEL→S1/S2, A_LEVEL→S4."""

    def __init__(self, session: Session) -> None:
        self.year = AcademicYear(
            name="2026/2027",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 7, 31),
            status=AcademicYearStatus.PLANNED.value,
        )
        self.o_level = Pathway(code="O_LEVEL", name="O-Level")
        self.a_level = Pathway(code="A_LEVEL", name="A-Level")
        self.s1 = EducationLevel(code="S1", name="Senior 1", level_number=7)
        self.s2 = EducationLevel(code="S2", name="Senior 2", level_number=8)
        self.s4 = EducationLevel(code="S4", name="Senior 4", level_number=10)
        self.school = School(name="Test Secondary School", school_code="TSS001")
        session.add_all([
            self.year, self.o_level, self.a_level, self.s1, self.s2, self.s4, self.school,
        ])
        session.flush()
        session.add_all([
            PathwayLevel(pathway_id=self.o_level.id, education_level_id=self.s1.id),
            PathwayLevel(pathway_id=self.o_level.id, education_level_id=self.s2.id),
            PathwayLevel(pathway_id=self.a_level.id, education_level_id=self.s4.id),
        ])
        session.flush()


@pytest.fixture()
def catalog(session: Session) -> Catalog:
    return Catalog(session)


def _payload(session: Session, catalog: Catalog, **overrides) -> RegistrationCreate:
    base = dict(
        student_id=_make_student(session).id,
        academic_year_id=catalog.year.id,
        pathway="O_LEVEL",
        education_level="S1",
        program_version_id=None,
        school_id=catalog.school.id,
    )
    base.update(overrides)
    return RegistrationCreate(**base)


def _make_program_version(
    session: Session,
    catalog: Catalog,
    *,
    pathway: Pathway | None = None,
    level: EducationLevel | None = None,
    with_subjects: bool = True,
) -> ProgramVersion:
    """Disposable program → version (+ subjects) for one offering context."""
    program = Program(code=f"PG_{uuid.uuid4().hex[:6].upper()}", name="Test Combination")
    session.add(program)
    session.flush()
    version = ProgramVersion(
        program_id=program.id,
        academic_year_id=catalog.year.id,
        pathway_id=(pathway or catalog.o_level).id,
        education_level_id=(level or catalog.s1).id,
        code=f"PV_{program.code}",
        name="Test Combination 2026/2027",
    )
    session.add(version)
    session.flush()
    if with_subjects:
        math = Subject(code="SUB_MATH", name="Mathematics")
        phy = Subject(code="SUB_PHY", name="Physics")
        session.add_all([math, phy])
        session.flush()
        session.add_all([
            ProgramSubject(program_version_id=version.id, subject_id=math.id, display_order=1),
            ProgramSubject(program_version_id=version.id, subject_id=phy.id, display_order=2),
        ])
        session.flush()
    return version


# --- resolution rules (Steps 6–8) ------------------------------------------------


def test_unknown_student_is_404(session: Session, catalog: Catalog) -> None:
    payload = _payload(session, catalog, student_id=uuid.uuid4())
    with pytest.raises(RegistrationNotFoundError):
        svc.register_student(session, payload)


def test_unknown_academic_year_is_404(session: Session, catalog: Catalog) -> None:
    payload = _payload(session, catalog, academic_year_id=uuid.uuid4())
    with pytest.raises(RegistrationNotFoundError):
        svc.register_student(session, payload)


def test_unknown_pathway_code_is_404(session: Session, catalog: Catalog) -> None:
    payload = _payload(session, catalog, pathway="NO_SUCH_PATHWAY")
    with pytest.raises(RegistrationNotFoundError):
        svc.register_student(session, payload)


def test_unknown_level_code_is_404(session: Session, catalog: Catalog) -> None:
    payload = _payload(session, catalog, education_level="S99")
    with pytest.raises(RegistrationNotFoundError):
        svc.register_student(session, payload)


def test_unknown_school_is_404(session: Session, catalog: Catalog) -> None:
    payload = _payload(session, catalog, school_id=uuid.uuid4())
    with pytest.raises(RegistrationNotFoundError):
        svc.register_student(session, payload)


# --- academic-year status (Step 7) -------------------------------------------------


@pytest.mark.parametrize("status_value", ["closed", "archived"])
def test_closed_or_archived_year_refuses_registration(
    session: Session, catalog: Catalog, status_value: str
) -> None:
    catalog.year.status = status_value
    session.commit()
    payload = _payload(session, catalog)
    with pytest.raises(RegistrationUnavailableError) as excinfo:
        svc.register_student(session, payload)
    assert status_value in str(excinfo.value)


def test_closed_verified_year_is_handled_honestly(session: Session, catalog: Catalog) -> None:
    """The real 2025/2026 seed year is 'closed' — registration must refuse it."""
    catalog.year.status = "closed"  # matches the actual verified-minimum seed
    session.commit()
    with pytest.raises(RegistrationUnavailableError):
        svc.register_student(session, _payload(session, catalog))


@pytest.mark.parametrize("status_value", ["planned", "active"])
def test_planned_or_active_year_accepts_registration(
    session: Session, catalog: Catalog, status_value: str
) -> None:
    catalog.year.status = status_value
    session.commit()
    result = svc.register_student(session, _payload(session, catalog))
    assert result.status == EnrollmentStatus.PENDING.value


# --- pathway ↔ level (Steps 8–9) ----------------------------------------------------


def test_invalid_pathway_level_pair_is_422(session: Session, catalog: Catalog) -> None:
    """O_LEVEL is not mapped to S4 — only both rows existing is not enough."""
    payload = _payload(session, catalog, pathway="O_LEVEL", education_level="S4")
    with pytest.raises(RegistrationValidationError) as excinfo:
        svc.register_student(session, payload)
    assert "does not include" in str(excinfo.value)


def test_valid_pathway_level_pair_creates_enrollment(session: Session, catalog: Catalog) -> None:
    payload = _payload(session, catalog, pathway="A_LEVEL", education_level="S4")
    result = svc.register_student(session, payload)
    assert result.pathway_code == "A_LEVEL"
    assert result.level_code == "S4"


# --- duplicate enrollment (Step 15) --------------------------------------------------


def test_duplicate_enrollment_in_same_year_is_409(session: Session, catalog: Catalog) -> None:
    payload = _payload(session, catalog)
    first = svc.register_student(session, payload)
    session.commit()
    with pytest.raises(RegistrationConflictError):
        svc.register_student(session, payload)
    session.rollback()
    # Exactly one enrollment remains, and it is the first one.
    count = len(session.scalars(select(StudentEnrollment)).all())
    assert count == 1
    assert count and first.enrollment_id


def test_same_student_different_year_is_allowed(session: Session, catalog: Catalog) -> None:
    svc.register_student(session, _payload(session, catalog))
    session.commit()
    second_year = AcademicYear(
        name="2027/2028",
        start_date=date(2027, 9, 1),
        end_date=date(2028, 7, 31),
        status=AcademicYearStatus.PLANNED.value,
    )
    session.add(second_year)
    session.commit()
    result = svc.register_student(
        session, _payload(session, catalog, academic_year_id=second_year.id)
    )
    assert result.academic_year == "2027/2028"


# --- program version context (Steps 10–12) ---------------------------------------------


def test_program_version_from_wrong_year_is_422(session: Session, catalog: Catalog) -> None:
    version = _make_program_version(session, catalog)
    session.commit()
    other_year = AcademicYear(
        name="2027/2028",
        start_date=date(2027, 9, 1),
        end_date=date(2028, 7, 31),
        status=AcademicYearStatus.PLANNED.value,
    )
    session.add(other_year)
    session.commit()
    payload = _payload(
        session, catalog, program_version_id=version.id, academic_year_id=other_year.id
    )
    with pytest.raises(RegistrationValidationError):
        svc.register_student(session, payload)


def test_program_version_from_wrong_pathway_is_422(session: Session, catalog: Catalog) -> None:
    version = _make_program_version(session, catalog, pathway=catalog.o_level)
    session.commit()
    payload = _payload(
        session, catalog, program_version_id=version.id,
        pathway="A_LEVEL", education_level="S4",
    )
    with pytest.raises(RegistrationValidationError):
        svc.register_student(session, payload)


def test_program_version_from_wrong_level_is_422(session: Session, catalog: Catalog) -> None:
    version = _make_program_version(session, catalog, level=catalog.s1)
    session.commit()
    payload = _payload(session, catalog, program_version_id=version.id, education_level="S2")
    with pytest.raises(RegistrationValidationError):
        svc.register_student(session, payload)


def test_unknown_program_version_id_is_404(session: Session, catalog: Catalog) -> None:
    payload = _payload(session, catalog, program_version_id=uuid.uuid4())
    with pytest.raises(RegistrationNotFoundError):
        svc.register_student(session, payload)


def test_inactive_program_version_is_422(session: Session, catalog: Catalog) -> None:
    version = _make_program_version(session, catalog)
    version.status = "inactive"
    session.commit()
    payload = _payload(session, catalog, program_version_id=version.id)
    with pytest.raises(RegistrationValidationError):
        svc.register_student(session, payload)


def test_tvet_program_without_sector_profile_is_unavailable(
    session: Session, catalog: Catalog
) -> None:
    """A TVET-typed program without a tvet_programs row refuses honestly."""
    program = Program(
        code="PG_TVET_NOSECTOR", name="ICT Trade", program_type="tvet_program"
    )
    session.add(program)
    session.flush()
    version = ProgramVersion(
        program_id=program.id,
        academic_year_id=catalog.year.id,
        pathway_id=catalog.o_level.id,
        education_level_id=catalog.s1.id,
        code="PV_TVET_NOSECTOR",
        name="ICT Trade 2026/2027",
    )
    session.add(version)
    session.commit()
    payload = _payload(session, catalog, program_version_id=version.id)
    with pytest.raises(RegistrationUnavailableError):
        svc.register_student(session, payload)


# --- school offering (Step 13) -------------------------------------------------------


def test_school_not_offering_program_is_422(session: Session, catalog: Catalog) -> None:
    version = _make_program_version(session, catalog)
    session.commit()  # no SchoolProgram row created — the school does not offer it
    payload = _payload(session, catalog, program_version_id=version.id)
    with pytest.raises(RegistrationValidationError) as excinfo:
        svc.register_student(session, payload)
    assert "does not offer" in str(excinfo.value)


def test_school_offering_program_succeeds(session: Session, catalog: Catalog) -> None:
    version = _make_program_version(session, catalog)
    session.add(SchoolProgram(school_id=catalog.school.id, program_version_id=version.id))
    session.commit()
    result = svc.register_student(session, _payload(session, catalog, program_version_id=version.id))
    assert result.program_code == version.program.code
    assert result.school_name == "Test Secondary School"


# --- enrollment status (Step 18) -----------------------------------------------------


def test_initial_status_is_pending(session: Session, catalog: Catalog) -> None:
    result = svc.register_student(session, _payload(session, catalog))
    assert result.status == EnrollmentStatus.PENDING.value


def test_registration_is_never_auto_completed(session: Session, catalog: Catalog) -> None:
    result = svc.register_student(session, _payload(session, catalog))
    assert result.status != "completed"


# --- student subjects (Step 17) --------------------------------------------------------


def test_student_subjects_derived_from_program_version(session: Session, catalog: Catalog) -> None:
    version = _make_program_version(session, catalog, with_subjects=True)
    session.add(SchoolProgram(school_id=catalog.school.id, program_version_id=version.id))
    session.commit()
    result = svc.register_student(session, _payload(session, catalog, program_version_id=version.id))
    codes = sorted(s.code for s in result.subjects)
    assert codes == ["SUB_MATH", "SUB_PHY"]
    assert all(s.status == "active" for s in result.subjects)


def test_no_subjects_when_program_version_has_none(session: Session, catalog: Catalog) -> None:
    version = _make_program_version(session, catalog, with_subjects=False)
    session.add(SchoolProgram(school_id=catalog.school.id, program_version_id=version.id))
    session.commit()
    result = svc.register_student(session, _payload(session, catalog, program_version_id=version.id))
    assert result.subjects == []


def test_no_subjects_without_program_version(session: Session, catalog: Catalog) -> None:
    result = svc.register_student(session, _payload(session, catalog))
    assert result.subjects == []


def test_subject_mapping_uses_program_subjects_not_bare_subjects(
    session: Session, catalog: Catalog
) -> None:
    """Only subjects actually mapped to the version become student subjects,
    even when other subjects exist in the shared catalog."""
    version = _make_program_version(session, catalog, with_subjects=True)
    session.add(Subject(code="SUB_UNRELATED", name="Unrelated"))
    session.add(SchoolProgram(school_id=catalog.school.id, program_version_id=version.id))
    session.commit()
    rows = subject_ids_for_program_version(session, version.id)
    assert len(rows) == 2


# --- transaction rollback (Steps 16 / 28) ------------------------------------------------


def test_failure_after_enrollment_insert_rolls_everything_back(
    session: Session, catalog: Catalog
) -> None:
    """A failure during subject derivation must not leave the enrollment."""
    version = _make_program_version(session, catalog, with_subjects=True)
    session.add(SchoolProgram(school_id=catalog.school.id, program_version_id=version.id))
    session.commit()

    with patch.object(
        svc.enrollment_repo,
        "create_student_subjects",
        side_effect=RuntimeError("boom during subject creation"),
    ):
        with pytest.raises(RuntimeError):
            svc.register_student(
                session, _payload(session, catalog, program_version_id=version.id)
            )
    session.rollback()

    assert session.scalars(select(StudentSubject)).all() == []
    assert session.scalars(select(StudentEnrollment)).all() == []


# --- retrieval (Step 21) -----------------------------------------------------------------


def test_get_registration_roundtrip(session: Session, catalog: Catalog) -> None:
    created = svc.register_student(session, _payload(session, catalog))
    session.commit()
    fetched = svc.get_registration(session, created.enrollment_id)
    assert fetched.enrollment_id == created.enrollment_id
    assert fetched.academic_year == "2026/2027"
    assert fetched.pathway_code == "O_LEVEL"
    assert fetched.level_code == "S1"
    assert fetched.school_name == "Test Secondary School"


def test_get_unknown_registration_is_404(session: Session) -> None:
    with pytest.raises(RegistrationNotFoundError):
        svc.get_registration(session, uuid.uuid4())


def test_list_student_registrations_newest_year_first(session: Session, catalog: Catalog) -> None:
    student = _make_student(session)
    session.commit()
    svc.register_student(
        session,
        RegistrationCreate(
            student_id=student.id,
            academic_year_id=catalog.year.id,
            pathway="O_LEVEL",
            education_level="S1",
            school_id=catalog.school.id,
        ),
    )
    session.commit()
    second_year = AcademicYear(
        name="2027/2028",
        start_date=date(2027, 9, 1),
        end_date=date(2028, 7, 31),
        status=AcademicYearStatus.PLANNED.value,
    )
    session.add(second_year)
    session.commit()
    svc.register_student(
        session,
        RegistrationCreate(
            student_id=student.id,
            academic_year_id=second_year.id,
            pathway="O_LEVEL",
            education_level="S2",
            school_id=catalog.school.id,
        ),
    )
    session.commit()
    history = svc.list_student_registrations(session, student.id)
    assert [r.academic_year for r in history] == ["2027/2028", "2026/2027"]


def test_list_unknown_student_is_404(session: Session) -> None:
    with pytest.raises(RegistrationNotFoundError):
        svc.list_student_registrations(session, uuid.uuid4())
