"""End-to-end registration integration test (PostgreSQL, guarded test DB).

The authoritative Phase 5G verification: the complete authenticated flow
against ``super_teacher_db_test`` — account registration, login, identity,
self-registration, enrollment + subject verification, retrieval, history,
duplicate 409, invalid pathway/level 422, school-not-offering 422,
closed-year refusal, logout.

Safety:
- ``pg_engine`` refuses to run unless ``ENVIRONMENT=testing`` and the
  database name ends with ``_test``;
- the actual database name is re-verified via ``SELECT current_database()``
  before any write;
- every row this test creates is disposable and truncated afterwards;
- the development database is never touched.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


def _assert_test_database(engine) -> str:
    with engine.begin() as connection:
        name = connection.execute(text("SELECT current_database()")).scalar_one()
    if not name.endswith("_test"):
        raise RuntimeError(f"SAFETY REFUSAL: connected to {name!r} (not a *_test database)")
    return name


def _truncate_all(engine) -> None:
    from tests.conftest import APPLICATION_TABLES

    with engine.begin() as connection:
        for table in APPLICATION_TABLES:
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )


@pytest.fixture(scope="module")
def api_client(pg_engine):
    from app.core.database import Base, SessionLocal, get_db
    import app.models  # noqa: F401
    from app.main import app

    Base.metadata.create_all(pg_engine)

    def _override_get_db():
        db = SessionLocal(bind=pg_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def e2e_db(pg_engine):
    _assert_test_database(pg_engine)
    _truncate_all(pg_engine)
    yield pg_engine
    _truncate_all(pg_engine)


def _build_complete_catalog(engine) -> dict:
    """A complete, disposable registration scenario (test DB only)."""
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry
    from app.models.academic_year import AcademicYear
    from app.models.education_level import EducationLevel
    from app.models.pathway import Pathway
    from app.models.program import Program
    from app.models.program_subject import ProgramSubject
    from app.models.program_version import ProgramVersion
    from app.models.school import School
    from app.models.school_program import SchoolProgram
    from app.models.subject import Subject

    session = SessionLocal(bind=engine)
    try:
        run_load(session, load_registry())

        year = AcademicYear(
            name="2026/2027",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 7, 31),
            status="planned",
        )
        school = School(name="E2E Secondary", school_code="E2E001")
        session.add_all([year, school])
        session.flush()

        pathway = session.scalars(
            select(Pathway).where(Pathway.code == "O_LEVEL")
        ).first()
        level = session.scalars(
            select(EducationLevel).where(EducationLevel.code == "S1")
        ).first()

        program = Program(code="E2E_COMBO", name="E2E Combination")
        session.add(program)
        session.flush()
        version = ProgramVersion(
            program_id=program.id,
            academic_year_id=year.id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            code="E2E_COMBO_V",
            name="E2E Combination 2026/2027",
        )
        session.add(version)
        session.flush()

        math = Subject(code="E2E_MATH", name="Mathematics")
        phy = Subject(code="E2E_PHY", name="Physics")
        session.add_all([math, phy])
        session.flush()
        session.add_all([
            ProgramSubject(program_version_id=version.id, subject_id=math.id, display_order=1),
            ProgramSubject(program_version_id=version.id, subject_id=phy.id, display_order=2),
        ])
        session.add(SchoolProgram(school_id=school.id, program_version_id=version.id))
        session.commit()

        return {
            "year_id": str(year.id),
            "year_name": year.name,
            "pathway_code": pathway.code,
            "level_code": level.code,
            "program_version_id": str(version.id),
            "school_id": str(school.id),
            "school_name": school.name,
            "closed_year_id": str(
                session.scalars(
                    select(AcademicYear.id).where(AcademicYear.name == "2025/2026")
                ).first()
            ),
        }
    finally:
        session.close()


def _register_account(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Eve Student",
            "date_of_birth": "2012-04-10",
            "gender": "female",
            "country": "Rwanda",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_complete_registration_flow(api_client, e2e_db) -> None:
    """The authoritative end-to-end authenticated scenario."""
    context = _build_complete_catalog(e2e_db)
    tokens = _register_account(api_client, "e2e-flow@example.com")
    header = {"Authorization": f"Bearer {tokens['access_token']}"}

    # --- identity -----------------------------------------------------------
    me = api_client.get("/api/v1/me", headers=header)
    assert me.status_code == 200
    assert me.json()["role"] == "student"
    student_id = api_client.get("/api/v1/me/student", headers=header).json()["student_id"]

    # --- submit registration ------------------------------------------------
    response = api_client.post(
        "/api/v1/me/registrations",
        json={
            "academic_year_id": context["year_id"],
            "pathway": context["pathway_code"],
            "education_level": context["level_code"],
            "program_version_id": context["program_version_id"],
            "school_id": context["school_id"],
        },
        headers=header,
    )
    assert response.status_code == 201, response.text
    registration = response.json()
    assert registration["status"] == "pending"
    assert registration["student_id"] == student_id
    assert registration["academic_year"] == context["year_name"]
    assert registration["program_code"] == "E2E_COMBO"
    assert registration["school_name"] == context["school_name"]
    assert sorted(s["code"] for s in registration["subjects"]) == ["E2E_MATH", "E2E_PHY"]

    enrollment_id = registration["enrollment_id"]

    # --- verify rows exist exactly once ---------------------------------------
    from app.core.database import SessionLocal
    from app.models.enrollment import StudentEnrollment
    from app.models.student_subject import StudentSubject

    session = SessionLocal(bind=e2e_db)
    try:
        enrollments = session.scalars(select(StudentEnrollment)).all()
        assert len(enrollments) == 1
        assert str(enrollments[0].id) == enrollment_id
        subjects = session.scalars(select(StudentSubject)).all()
        assert len(subjects) == 2
        assert all(link.enrollment_id == enrollments[0].id for link in subjects)
    finally:
        session.close()

    # --- retrieval --------------------------------------------------------------
    fetched = api_client.get(
        f"/api/v1/me/registrations/{enrollment_id}", headers=header
    )
    assert fetched.status_code == 200
    assert fetched.json()["enrollment_id"] == enrollment_id

    # --- history (newest first) ---------------------------------------------------
    history = api_client.get("/api/v1/me/registrations", headers=header)
    assert history.status_code == 200
    assert [row["enrollment_id"] for row in history.json()] == [enrollment_id]

    # --- repeat registration → 409 ------------------------------------------------
    duplicate = api_client.post(
        "/api/v1/me/registrations",
        json={
            "academic_year_id": context["year_id"],
            "pathway": context["pathway_code"],
            "education_level": context["level_code"],
            "program_version_id": context["program_version_id"],
            "school_id": context["school_id"],
        },
        headers=header,
    )
    assert duplicate.status_code == 409
    assert "already enrolled" in duplicate.json()["detail"]

    # --- invalid pathway/level → 422 (O_LEVEL is not mapped to S4) ------------------
    invalid_pair = api_client.post(
        "/api/v1/me/registrations",
        json={
            "academic_year_id": context["year_id"],
            "pathway": context["pathway_code"],
            "education_level": "S4",
            "school_id": context["school_id"],
        },
        headers=header,
    )
    assert invalid_pair.status_code == 422
    assert "does not include" in invalid_pair.json()["detail"]

    # --- school does not offer program → 422 --------------------------------------
    # A second school exists but has no school_programs row for the version.
    # A fresh account keeps the duplicate pre-check (409) out of the way.
    from app.models.school import School as SchoolModel

    session = SessionLocal(bind=e2e_db)
    try:
        other_school = SchoolModel(name="E2E Other School", school_code="E2E002")
        session.add(other_school)
        session.commit()
        other_school_id = str(other_school.id)
    finally:
        session.close()

    other_tokens = _register_account(api_client, "e2e-other@example.com")
    other_header = {"Authorization": f"Bearer {other_tokens['access_token']}"}
    not_offered = api_client.post(
        "/api/v1/me/registrations",
        json={
            "academic_year_id": context["year_id"],
            "pathway": context["pathway_code"],
            "education_level": context["level_code"],
            "program_version_id": context["program_version_id"],
            "school_id": other_school_id,
        },
        headers=other_header,
    )
    assert not_offered.status_code == 422
    assert "does not offer" in not_offered.json()["detail"]

    # --- closed academic year → 503 -------------------------------------------------
    closed = api_client.post(
        "/api/v1/me/registrations",
        json={
            "academic_year_id": context["closed_year_id"],
            "pathway": context["pathway_code"],
            "education_level": context["level_code"],
            "school_id": context["school_id"],
        },
        headers=header,
    )
    assert closed.status_code == 503
    assert "closed" in closed.json()["detail"]

    # --- readiness reflects the scenario ----------------------------------------------
    readiness = api_client.get("/api/v1/registrations/readiness")
    assert readiness.status_code == 200
    body = readiness.json()
    assert body["ready"] is True
    assert body["open_academic_year"]["name"] == "2026/2027"
    assert body["pathway_level_mappings_available"] is True
    assert body["program_catalog_available"] is True
    assert body["school_catalog_available"] is True

    # --- logout invalidates refresh; access token still works until expiry ----
    logout = api_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=header,
    )
    assert logout.status_code == 204
    replay = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401


def test_readiness_empty_database_reports_unavailable(api_client, e2e_db) -> None:
    """No catalog at all → truthful not-ready, no invented data."""
    response = api_client.get("/api/v1/registrations/readiness")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["open_academic_year"] is None
    assert body["pathways_available"] is False
    assert body["pathway_level_mappings_available"] is False
    assert body["school_catalog_available"] is False


def test_malformed_uuid_is_422_not_500(api_client, e2e_db) -> None:
    """Malformed path UUIDs are a client error handled by FastAPI."""
    tokens = _register_account(api_client, "e2e-malformed@example.com")
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = api_client.get("/api/v1/me/registrations/not-a-uuid", headers=header)
    assert response.status_code == 422
