"""Catalog API integration tests (PostgreSQL, opt-in, guarded test DB).

Exercises every catalog endpoint through the FastAPI TestClient against
``super_teacher_db_test`` (the guarded test database — never the development
database). The suite seeds the test DB with the same verified-minimum
catalog the seeder loads (via the real loader, which is idempotent and
read-safe), then asserts:

- populated resources return real rows with the documented ordering,
- empty resources (programs, TVET, schools) truthfully return ``[]``,
- unknown identities produce 404s,
- response bodies validate against the Pydantic response models.

Cleanup: tests truncate everything they wrote so the ``clean_db``
leak-guard invariant (zero rows after the test) still holds.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def api_client(pg_engine):
    """TestClient with the app's get_db dependency overridden to the test DB."""
    from app.core.database import Base, SessionLocal, get_db
    import app.models  # noqa: F401
    from app.main import app

    Base.metadata.create_all(pg_engine)  # no-op if migration schema already applied

    def _override_get_db():
        db = SessionLocal(bind=pg_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _truncate_all(engine) -> None:
    from tests.conftest import APPLICATION_TABLES

    with engine.begin() as connection:
        for table in APPLICATION_TABLES:
            connection.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))


def _seed_catalog(engine) -> None:
    """Load the verified-minimum catalog through the real (idempotent) seeder."""
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry

    session = SessionLocal(bind=engine)
    try:
        reports = run_load(session, load_registry())
    finally:
        session.close()
    assert sum(r.writes for r in reports) == 40  # verified-minimum catalog size
    assert all(not r.warnings for r in reports)


@pytest.fixture()
def seeded_db(pg_engine):
    _truncate_all(pg_engine)
    _seed_catalog(pg_engine)
    yield pg_engine
    _truncate_all(pg_engine)


# --- populated resources -------------------------------------------------------


def test_health(api_client) -> None:
    response = api_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_academic_years(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/academic-years")
    assert response.status_code == 200
    rows = response.json()
    assert [r["name"] for r in rows] == ["2025/2026"]
    assert rows[0]["status"] == "closed"
    assert rows[0]["start_date"] == "2025-09-01"


def test_academic_years_status_filter_no_match_is_empty_list(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/academic-years?status=active")
    assert response.status_code == 200
    assert response.json() == []  # empty result, not an error


def test_pathways_ordered_by_code(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/pathways")
    assert response.status_code == 200
    assert [r["code"] for r in response.json()] == ["A_LEVEL", "O_LEVEL", "TTC", "TVET"]


def test_education_levels_all(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/education-levels")
    assert response.status_code == 200
    rows = response.json()
    assert [r["code"] for r in rows] == ["S1", "S2", "S3", "S4", "S5", "S6", "L3", "L4", "L5"]
    numbers = [r["level_number"] for r in rows]
    assert numbers == sorted(numbers)  # deterministic level_number ordering


def test_education_levels_filtered_by_pathway(api_client, seeded_db) -> None:
    o = api_client.get("/api/v1/catalog/education-levels?pathway=O_LEVEL").json()
    a = api_client.get("/api/v1/catalog/education-levels?pathway=A_LEVEL").json()
    tvet = api_client.get("/api/v1/catalog/education-levels?pathway=TVET").json()
    assert [r["code"] for r in o] == ["S1", "S2", "S3"]
    assert [r["code"] for r in a] == ["S4", "S5", "S6"]
    assert [r["code"] for r in tvet] == ["L3", "L4", "L5"]


def test_education_levels_unknown_pathway_is_404(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/education-levels?pathway=NOPE")
    assert response.status_code == 404
    assert "NOPE" in response.json()["detail"]


def test_subjects(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/subjects")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 14
    names = [r["name"] for r in rows]
    assert names == sorted(names)  # name ordering
    codes = {r["code"] for r in rows}
    assert "SUB_MATH" in codes and "SUB_KIN" in codes


# --- truthfully empty resources ---------------------------------------------------


def test_programs_truthfully_empty(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/programs")
    assert response.status_code == 200
    assert response.json() == []


def test_programs_unknown_pathway_is_404_even_when_empty(api_client, seeded_db) -> None:
    # Identity resolution happens BEFORE the (empty) query: an unknown
    # pathway code is an error, not an empty list.
    response = api_client.get("/api/v1/catalog/programs?pathway=NOPE")
    assert response.status_code == 404


def test_program_versions_truthfully_empty(api_client, seeded_db) -> None:
    assert api_client.get("/api/v1/catalog/program-versions").json() == []


def test_tvet_sectors_truthfully_empty(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/tvet/sectors")
    assert response.status_code == 200
    assert response.json() == []


def test_tvet_programs_truthfully_empty(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/tvet/programs")
    assert response.status_code == 200
    assert response.json() == []


def test_schools_truthfully_empty(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/schools")
    assert response.status_code == 200
    assert response.json() == []


def test_school_programs_unknown_school_is_404(api_client, seeded_db) -> None:
    response = api_client.get("/api/v1/catalog/schools/UNKNOWN01/programs")
    assert response.status_code == 404


def test_school_programs_enriched_response(api_client, seeded_db) -> None:
    """GET /schools/{code}/programs returns program version context fields."""
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models.academic_year import AcademicYear
    from app.models.education_level import EducationLevel
    from app.models.pathway import Pathway
    from app.models.program import Program
    from app.models.program_version import ProgramVersion
    from app.models.school import School
    from app.models.school_program import SchoolProgram

    session = SessionLocal(bind=seeded_db)
    try:
        year = AcademicYear(
            name="2026/2027", start_date=date(2026, 9, 1),
            end_date=date(2027, 7, 31), status="planned",
        )
        # O_LEVEL and S1 already exist — the seeded_db fixture loaded them
        # through the real seeder; re-inserting would violate the natural
        # key unique constraints (uq_pathways_code_key /
        # uq_education_levels_code_key).
        o_level = session.scalars(
            select(Pathway).where(Pathway.code == "O_LEVEL")
        ).one()
        s1 = session.scalars(
            select(EducationLevel).where(EducationLevel.code == "S1")
        ).one()
        program = Program(code="PRG_INT", name="Integration Test Program", status="active")
        school = School(name="Enriched School", school_code="ENR001", status="active")
        session.add_all([year, program, school])
        session.flush()

        pv = ProgramVersion(
            program_id=program.id, academic_year_id=year.id,
            pathway_id=o_level.id, education_level_id=s1.id,
            code="INT-V1", name="Integration V1", status="active",
        )
        session.add(pv)
        session.flush()

        sp = SchoolProgram(school_id=school.id, program_version_id=pv.id, status="active")
        session.add(sp)
        session.commit()
    finally:
        session.close()

    response = api_client.get("/api/v1/catalog/schools/ENR001/programs")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["program_code"] == "PRG_INT"
    assert row["program_name"] == "Integration Test Program"
    assert row["code"] == "INT-V1"
    assert row["name"] == "Integration V1"
    assert row["pathway_code"] == "O_LEVEL"
    assert row["level_code"] == "S1"
    assert row["school_code"] == "ENR001"
    assert row["status"] == "active"

    # cleanup: remove only the rows this test created (the fixture truncates
    # everything afterwards; deleting seeded reference rows here would be wrong)
    from sqlalchemy import text
    with seeded_db.begin() as conn:
        conn.execute(text('DELETE FROM school_programs'))
        conn.execute(text('DELETE FROM program_versions'))
        conn.execute(text('DELETE FROM programs'))
        conn.execute(text('DELETE FROM schools'))
        conn.execute(text("DELETE FROM academic_years WHERE name = '2026/2027'"))


# --- openapi --------------------------------------------------------------------


def test_openapi_schema_includes_catalog_endpoints(api_client, seeded_db) -> None:
    schema = api_client.get("/openapi.json").json()
    paths = schema["paths"]
    for path in (
        "/api/v1/catalog/academic-years",
        "/api/v1/catalog/pathways",
        "/api/v1/catalog/education-levels",
        "/api/v1/catalog/subjects",
        "/api/v1/catalog/programs",
        "/api/v1/catalog/program-versions",
        "/api/v1/catalog/tvet/sectors",
        "/api/v1/catalog/tvet/programs",
        "/api/v1/catalog/schools",
        "/api/v1/catalog/schools/{school_code}/programs",
    ):
        assert path in paths, path
