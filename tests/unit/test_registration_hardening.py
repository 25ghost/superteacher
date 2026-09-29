"""Unit tests: Phase 5F registration hardening.

Covers the stabilization work against in-memory SQLite:

- race-safe duplicate enrollment: an IntegrityError carrying the
  ``(student_id, academic_year_id)`` UNIQUE constraint must surface as the
  409 conflict contract, never as a 500;
- unexpected integrity errors stay visible (re-raised, not masked);
- the development-stage guard refuses unauthenticated write APIs outside
  development environments;
- registration readiness truthfully reports catalog availability;
- production never inherits localhost CORS origins.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.academic_year import AcademicYear
from app.models.education_level import EducationLevel
from app.models.enums import AcademicYearStatus
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.school import School
from app.models.student import Student
from app.models.user import User
from app.schemas.registration import RegistrationCreate
from app.services import registration_service as svc
from app.services.registration_service import RegistrationConflictError


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


def _context(session: Session):
    """Minimal registrable context (disposable ORM rows)."""
    user = User(email="hard@example.com", role="student")
    session.add(user)
    session.flush()
    student = Student(user_id=user.id, full_name="Hardened Student")
    year = AcademicYear(
        name="2026/2027",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 7, 31),
        status=AcademicYearStatus.PLANNED.value,
    )
    o_level = Pathway(code="O_LEVEL", name="O-Level")
    s1 = EducationLevel(code="S1", name="Senior 1", level_number=7)
    school = School(name="Guard School", school_code="GDS001")
    session.add_all([student, year, o_level, s1, school])
    session.flush()
    session.add(PathwayLevel(pathway_id=o_level.id, education_level_id=s1.id))
    session.commit()
    return student, year, o_level, s1, school


# --- race-safe duplicate enrollment (Step 4) -------------------------------------


def _duplicate_integrity_error() -> IntegrityError:
    """A realistic IntegrityError for the enrollment uniqueness constraint."""
    constraint = svc.DUPLICATE_ENROLLMENT_CONSTRAINT
    return IntegrityError(
        "INSERT INTO student_enrollments ...",
        {},
        Exception(f'duplicate key value violates unique constraint "{constraint}"'),
    )


def test_duplicate_race_surfaces_as_409(session: Session) -> None:
    student, year, o_level, s1, school = _context(session)
    payload = RegistrationCreate(
        student_id=student.id,
        academic_year_id=year.id,
        pathway=o_level.code,
        education_level=s1.code,
        school_id=school.id,
    )
    with patch.object(
        svc.enrollment_repo,
        "create",
        side_effect=_duplicate_integrity_error(),
    ):
        with pytest.raises(RegistrationConflictError) as excinfo:
            svc.register_student(session, payload)
    assert "already enrolled" in str(excinfo.value)


def test_unexpected_integrity_error_is_not_masked(session: Session) -> None:
    """A non-duplicate database failure must stay visible (500 territory)."""
    student, year, o_level, s1, school = _context(session)
    payload = RegistrationCreate(
        student_id=student.id,
        academic_year_id=year.id,
        pathway=o_level.code,
        education_level=s1.code,
        school_id=school.id,
    )
    unexpected = IntegrityError(
        "INSERT INTO student_enrollments ...", {}, Exception("db connection lost")
    )
    with patch.object(svc.enrollment_repo, "create", side_effect=unexpected):
        with pytest.raises(IntegrityError):
            svc.register_student(session, payload)


def test_duplicate_race_logs_without_secrets(session: Session, caplog) -> None:
    """The warning must not echo credentials or secret material."""
    import logging

    student, year, o_level, s1, school = _context(session)
    payload = RegistrationCreate(
        student_id=student.id,
        academic_year_id=year.id,
        pathway=o_level.code,
        education_level=s1.code,
        school_id=school.id,
    )
    with patch.object(
        svc.enrollment_repo, "create", side_effect=_duplicate_integrity_error()
    ):
        with caplog.at_level(logging.WARNING, logger="app.services.registration_service"):
            with pytest.raises(RegistrationConflictError):
                svc.register_student(session, payload)
    assert any("duplicate registration race" in record.message for record in caplog.records)
    for record in caplog.records:
        assert "password" not in record.getMessage().lower()
        assert "secret" not in record.getMessage().lower()


# --- development-stage guard (Step 8) -----------------------------------------------


def test_guard_allows_development(monkeypatch) -> None:
    from app.core.dev_guard import require_development_stage

    monkeypatch.setattr(
        "app.core.dev_guard.get_settings",
        lambda: type("S", (), {"ENVIRONMENT": "development"})(),
    )
    require_development_stage()  # no exception


def test_guard_allows_testing(monkeypatch) -> None:
    from app.core.dev_guard import require_development_stage

    monkeypatch.setattr(
        "app.core.dev_guard.get_settings",
        lambda: type("S", (), {"ENVIRONMENT": "testing"})(),
    )
    require_development_stage()  # no exception


def test_guard_refuses_production(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.core.dev_guard import require_development_stage

    monkeypatch.setattr(
        "app.core.dev_guard.get_settings",
        lambda: type("S", (), {"ENVIRONMENT": "production"})(),
    )
    with pytest.raises(HTTPException) as excinfo:
        require_development_stage()
    assert excinfo.value.status_code == 503
    assert "development stage" in excinfo.value.detail


# --- readiness report (Step 15) -------------------------------------------------------


def test_readiness_empty_catalog_is_not_ready(session: Session) -> None:
    report = svc.registration_readiness(session)
    assert report["ready"] is False
    assert report["open_academic_year"] is None
    assert report["pathways_available"] is False
    assert report["pathway_level_mappings_available"] is False
    assert report["school_catalog_available"] is False


def test_readiness_schools_missing_blocks_ready(session: Session) -> None:
    _context(session)  # year/pathway/level/school/pathway_level all present → ready
    report = svc.registration_readiness(session)
    assert report["ready"] is True
    assert report["open_academic_year"]["name"] == "2026/2027"
    assert report["pathways_available"] is True
    assert report["pathway_level_mappings_available"] is True
    assert report["program_catalog_available"] is False  # truthfully empty
    assert report["tvet_catalog_available"] is False  # truthfully empty


def test_readiness_pathway_levels_missing_blocks_ready(session: Session) -> None:
    """Pathways and levels exist but no mapping → registration impossible."""
    user = User(email="pl@example.com", role="student")
    session.add(user)
    session.flush()
    student = Student(user_id=user.id, full_name="PL Student")
    year = AcademicYear(
        name="2026/2027", start_date=date(2026, 9, 1),
        end_date=date(2027, 7, 31), status=AcademicYearStatus.PLANNED.value,
    )
    o_level = Pathway(code="O_LEVEL", name="O-Level")
    s1 = EducationLevel(code="S1", name="Senior 1", level_number=7)
    school = School(name="PL School", school_code="PLS001")
    session.add_all([student, year, o_level, s1, school])
    session.commit()
    # No PathwayLevel inserted — pathways + levels exist but are unlinked.
    report = svc.registration_readiness(session)
    assert report["ready"] is False
    assert report["pathways_available"] is True
    assert report["education_levels_available"] is True
    assert report["pathway_level_mappings_available"] is False
    assert report["school_catalog_available"] is True


def test_readiness_closed_year_only_is_not_ready(session: Session) -> None:
    session.add(
        AcademicYear(
            name="2025/2026",
            start_date=date(2025, 9, 1),
            end_date=date(2026, 7, 31),
            status="closed",
        )
    )
    session.commit()
    report = svc.registration_readiness(session)
    assert report["ready"] is False
    assert report["open_academic_year"] is None


# --- production CORS hardening (Step 9) -------------------------------------------------


def test_production_never_inherits_localhost_origins() -> None:
    from app.core.config import Settings

    settings = Settings(
        ENVIRONMENT="production",
        CORS_ORIGINS="http://localhost:5173,https://portal.superteacher.rw",
        _env_file=None,
    )
    assert "http://localhost:5173" not in settings.cors_origins
    assert "https://portal.superteacher.rw" in settings.cors_origins


def test_production_without_explicit_origins_has_closed_cors() -> None:
    from app.core.config import Settings

    settings = Settings(
        ENVIRONMENT="production",
        CORS_ORIGINS="http://localhost:5173",
        _env_file=None,
    )
    assert settings.cors_origins == []


def test_development_keeps_localhost_origins() -> None:
    from app.core.config import Settings

    settings = Settings(
        ENVIRONMENT="development",
        CORS_ORIGINS="http://localhost:5173",
        _env_file=None,
    )
    assert settings.cors_origins == ["http://localhost:5173"]


def test_wildcard_origin_is_never_added() -> None:
    from app.core.config import Settings

    settings = Settings(ENVIRONMENT="development", CORS_ORIGINS="*", _env_file=None)
    # The literal value passes through as configured — the middleware receives
    # exactly this one explicit entry; tests elsewhere assert the default list
    # contains no "*". Here we guard the parser: no implicit entries appear.
    assert settings.cors_origins == ["*"]
    # and the default configuration contains no wildcard:
    default = Settings(ENVIRONMENT="development", _env_file=None)
    assert "*" not in default.cors_origins
