"""Unit tests: catalog service mapping and identity resolution (no PostgreSQL).

The service is exercised with an in-memory SQLite database seeded through the
real ORM models, proving:

- payload mapping (service -> Pydantic),
- pathway -> level resolution through the real pathway_levels relationship,
- unknown-identity errors (CatalogNotFoundError),
- deterministic ordering (level_number, code, name, start_date desc),
- truthful empty lists for catalog tables with no rows.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.academic_year import AcademicYear
from app.models.education_level import EducationLevel
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_version import ProgramVersion
from app.models.school import School
from app.models.school_program import SchoolProgram
from app.models.subject import Subject
from app.repositories.catalog_repository import CatalogNotFoundError
from app.services import catalog_service as svc


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


def _seed_minimum_catalog(session: Session) -> None:
    year = AcademicYear(
        name="2025/2026", start_date=date(2025, 9, 1),
        end_date=date(2026, 7, 31), status="closed",
    )
    o_level = Pathway(code="O_LEVEL", name="O-Level", status="active")
    a_level = Pathway(code="A_LEVEL", name="A-Level", status="active")
    s1 = EducationLevel(code="S1", name="Senior 1", level_number=7, status="active")
    s4 = EducationLevel(code="S4", name="Senior 4", level_number=10, status="active")
    math = Subject(code="SUB_MATH", name="Mathematics", status="active")
    art = Subject(code="SUB_ART", name="Art", status="active")
    session.add_all([year, o_level, a_level, s1, s4, math, art])
    session.flush()
    session.add_all([
        PathwayLevel(pathway_id=o_level.id, education_level_id=s1.id),
        PathwayLevel(pathway_id=a_level.id, education_level_id=s4.id),
    ])
    session.commit()


def test_academic_years_newest_first(session: Session) -> None:
    session.add_all([
        AcademicYear(name="2024/2025", start_date=date(2024, 9, 1),
                     end_date=date(2025, 7, 31), status="closed"),
        AcademicYear(name="2025/2026", start_date=date(2025, 9, 1),
                     end_date=date(2026, 7, 31), status="closed"),
    ])
    session.commit()
    rows = svc.list_academic_years(session)
    assert [r.name for r in rows] == ["2025/2026", "2024/2025"]


def test_academic_years_status_filter_and_empty(session: Session) -> None:
    assert svc.list_academic_years(session) == []
    _seed_minimum_catalog(session)
    assert svc.list_academic_years(session, status="active") == []


def test_pathways_ordered_by_code_and_empty_state(session: Session) -> None:
    assert svc.list_pathways(session) == []
    _seed_minimum_catalog(session)
    assert [p.code for p in svc.list_pathways(session)] == ["A_LEVEL", "O_LEVEL"]


def test_levels_filtered_through_pathway_levels(session: Session) -> None:
    _seed_minimum_catalog(session)
    o_levels = svc.list_education_levels(session, pathway_code="O_LEVEL")
    a_levels = svc.list_education_levels(session, pathway_code="A_LEVEL")
    assert [l.code for l in o_levels] == ["S1"]
    assert [l.code for l in a_levels] == ["S4"]
    # Unfiltered returns everything ordered by level_number.
    assert [l.code for l in svc.list_education_levels(session)] == ["S1", "S4"]


def test_unknown_pathway_code_raises(session: Session) -> None:
    _seed_minimum_catalog(session)
    with pytest.raises(CatalogNotFoundError):
        svc.list_education_levels(session, pathway_code="NOPE")


def test_subjects_ordered_by_name(session: Session) -> None:
    _seed_minimum_catalog(session)
    assert [s.name for s in svc.list_subjects(session)] == ["Art", "Mathematics"]


def test_programs_and_versions_truthfully_empty(session: Session) -> None:
    assert svc.list_programs(session) == []
    assert svc.list_program_versions(session) == []
    assert svc.list_tvet_sectors(session) == []
    assert svc.list_tvet_programs(session) == []
    assert svc.list_schools(session) == []


def test_unknown_school_code_raises(session: Session) -> None:
    with pytest.raises(CatalogNotFoundError):
        svc.list_school_programs_for_school(session, "999999")


def test_school_programs_enriched_with_program_version_context(session: Session) -> None:
    """list_school_programs_for_school returns the full offering context."""
    year = AcademicYear(
        name="2026/2027", start_date=date(2026, 9, 1),
        end_date=date(2027, 7, 31), status="planned",
    )
    o_level = Pathway(code="O_LEVEL", name="O-Level", status="active")
    s1 = EducationLevel(code="S1", name="Senior 1", level_number=7, status="active")
    program = Program(code="PRG_MATH", name="Mathematics", status="active")
    school = School(name="Test School", school_code="TST001", status="active")
    session.add_all([year, o_level, s1, program, school])
    session.flush()

    pv = ProgramVersion(
        program_id=program.id, academic_year_id=year.id,
        pathway_id=o_level.id, education_level_id=s1.id,
        code="MATH-V1", name="Mathematics V1", status="active",
    )
    session.add(pv)
    session.flush()

    sp = SchoolProgram(
        school_id=school.id, program_version_id=pv.id, status="active",
    )
    session.add(sp)
    session.commit()

    results = svc.list_school_programs_for_school(session, "TST001")
    assert len(results) == 1
    r = results[0]
    assert r.program_code == "PRG_MATH"
    assert r.program_name == "Mathematics"
    assert r.code == "MATH-V1"
    assert r.name == "Mathematics V1"
    assert r.pathway_code == "O_LEVEL"
    assert r.level_code == "S1"
    assert r.school_code == "TST001"
    assert r.status == "active"
