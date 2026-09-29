"""Registration API integration tests (PostgreSQL, opt-in, guarded test DB).

Phase 5G contract: registration is **authenticated**.

- students register themselves (identity from the Bearer token, no
  ``student_id`` in the request);
- administrators (admin role) may register on behalf of a student by
  supplying ``student_id`` — the scenarios that exercise the service's
  catalog rules use this trusted workflow;
- ownership is enforced: A cannot read B's history or enrollments;
- unauthenticated callers are refused with 401.

Safety model unchanged: ``pg_engine`` refuses to run unless
``ENVIRONMENT=testing`` and the database ends with ``_test``; the actual
connected database name is re-verified (``SELECT current_database()``)
before any write; disposable rows are truncated afterwards; the
development database is never touched.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


# --- scaffolding -------------------------------------------------------------------


@pytest.fixture(scope="module")
def api_client(pg_engine):
    from app.core.database import Base, SessionLocal, get_db
    import app.models  # noqa: F401
    from app.main import app

    Base.metadata.create_all(pg_engine)  # no-op when migration schema applied

    def _override_get_db():
        db = SessionLocal(bind=pg_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _assert_test_database(engine) -> str:
    """Verify the actual connected database name before any write (Step 31)."""
    with engine.begin() as connection:
        name = connection.execute(text("SELECT current_database()")).scalar_one()
    if not name.endswith("_test"):
        raise RuntimeError(
            f"SAFETY REFUSAL: connected to {name!r}, which is not a *_test database"
        )
    return name


def _truncate_all(engine) -> None:
    from tests.conftest import APPLICATION_TABLES

    with engine.begin() as connection:
        for table in APPLICATION_TABLES:
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )


@pytest.fixture()
def registration_db(pg_engine):
    """Empty + catalog-seeded test database, with an explicit name check."""
    _assert_test_database(pg_engine)
    _truncate_all(pg_engine)

    # Seed the verified-minimum catalog through the real (idempotent) seeder.
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry

    session = SessionLocal(bind=pg_engine)
    try:
        reports = run_load(session, load_registry())
    finally:
        session.close()
    assert sum(r.writes for r in reports) == 40  # verified-minimum catalog size

    yield pg_engine

    _truncate_all(pg_engine)  # leak-guard: no registration rows survive


def _register_admin(api_client: TestClient, pg_engine) -> dict:
    """Create an admin directly in the test DB and log it in."""
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    session = SessionLocal(bind=pg_engine)
    try:
        session.add(
            User(
                email="reg-admin@test.example",
                role="admin",
                status="active",
                password_hash=hash_password("admin test passphrase"),
            )
        )
        session.commit()
    finally:
        session.close()
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": "reg-admin@test.example", "password": "admin test passphrase"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _register_student(api_client: TestClient) -> tuple[str, dict]:
    """A real student account through the public registration API."""
    response = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"reg-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
            "full_name": "Registration Test Student",
            "date_of_birth": "2012-04-10",
            "gender": "female",
            "country": "Rwanda",
        },
    )
    assert response.status_code == 201, response.text
    tokens = response.json()
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    student_id = api_client.get("/api/v1/me/student", headers=header).json()["student_id"]
    return student_id, header


# --- disposable context builders (test DB only, never seed datasets) --------------


class Context:
    """A controlled registration context inside the test database.

    Uses the real seeded catalog (pathway O_LEVEL, levels S1/S2/S3 and the
    closed 2025/2026 year) and adds ONLY the records the verified catalog
    legitimately lacks — a planned academic year and a school.
    """

    def __init__(self, engine) -> None:
        from app.core.database import SessionLocal
        from app.models.academic_year import AcademicYear
        from app.models.education_level import EducationLevel
        from app.models.pathway import Pathway
        from app.models.school import School

        self.engine = engine
        session = SessionLocal(bind=engine)
        try:
            self.pathway_code = session.scalars(
                select(Pathway.code).where(Pathway.code == "O_LEVEL")
            ).first()
            self.s1_code = session.scalars(
                select(EducationLevel.code).where(EducationLevel.code == "S1")
            ).first()
            self.s2_code = session.scalars(
                select(EducationLevel.code).where(EducationLevel.code == "S2")
            ).first()
            self.closed_year_id = str(
                session.scalars(
                    select(AcademicYear.id).where(AcademicYear.name == "2025/2026")
                ).first()
            )

            year = AcademicYear(
                name="2026/2027",
                start_date=date(2026, 9, 1),
                end_date=date(2027, 7, 31),
                status="planned",
            )
            school = School(name="Registration Test School", school_code="RTS000001")
            session.add_all([year, school])
            session.commit()
            self.year_id = str(year.id)
            self.school_id = str(school.id)
        finally:
            session.close()


def _make_program_context(
    engine,
    year_id: str,
    *,
    with_subjects: bool = True,
    school_offers: bool = True,
    pathway_code: str = "O_LEVEL",
    level_code: str = "S1",
) -> dict:
    """Disposable program version (+ optional subjects) and its school offering."""
    from app.core.database import SessionLocal
    from app.models.education_level import EducationLevel
    from app.models.pathway import Pathway
    from app.models.program import Program
    from app.models.program_subject import ProgramSubject
    from app.models.program_version import ProgramVersion
    from app.models.school import School
    from app.models.school_program import SchoolProgram
    from app.models.subject import Subject

    suffix = uuid.uuid4().hex[:6].upper()
    session = SessionLocal(bind=engine)
    try:
        pathway = session.scalars(
            select(Pathway).where(Pathway.code == pathway_code)
        ).first()
        level = session.scalars(
            select(EducationLevel).where(EducationLevel.code == level_code)
        ).first()
        program = Program(code=f"TST_{suffix}", name="Test Combination")
        school = School(name=f"Offering School {suffix}", school_code=f"OS{suffix}")
        session.add_all([program, school])
        session.flush()
        version = ProgramVersion(
            program_id=program.id,
            academic_year_id=year_id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            code=f"TST_{suffix}_V",
            name=f"Test Combination {suffix}",
        )
        session.add(version)
        session.flush()
        if with_subjects:
            math = Subject(code=f"TSUB_M_{suffix}", name="Mathematics (test)")
            phy = Subject(code=f"TSUB_P_{suffix}", name="Physics (test)")
            session.add_all([math, phy])
            session.flush()
            session.add_all([
                ProgramSubject(program_version_id=version.id, subject_id=math.id, display_order=1),
                ProgramSubject(program_version_id=version.id, subject_id=phy.id, display_order=2),
            ])
        if school_offers:
            session.add(SchoolProgram(school_id=school.id, program_version_id=version.id))
        session.commit()
        return {
            "program_version_id": str(version.id),
            "school_id": str(school.id),
            "program_code": program.code,
        }
    finally:
        session.close()


def _admin_registration_payload(
    context: Context, student_id: str, **overrides
) -> dict:
    payload = {
        "student_id": student_id,
        "academic_year_id": context.year_id,
        "pathway": context.pathway_code,
        "education_level": context.s1_code,
        "school_id": context.school_id,
    }
    payload.update(overrides)
    return payload


def _self_registration_payload(context: Context, **overrides) -> dict:
    payload = {
        "academic_year_id": context.year_id,
        "pathway": context.pathway_code,
        "education_level": context.s1_code,
        "school_id": context.school_id,
    }
    payload.update(overrides)
    return payload


# --- happy paths -----------------------------------------------------------------


def test_student_registers_self_without_program(api_client, registration_db) -> None:
    context = Context(registration_db)
    _, header = _register_student(api_client)

    response = api_client.post(
        "/api/v1/me/registrations", json=_self_registration_payload(context), headers=header
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["academic_year"] == "2026/2027"
    assert body["pathway_code"] == "O_LEVEL"
    assert body["level_code"] == "S1"
    assert body["school_name"] == "Registration Test School"
    assert body["program_version_id"] is None
    assert body["status"] == "pending"
    assert body["subjects"] == []


def test_student_registers_self_with_program_and_subjects(
    api_client, registration_db
) -> None:
    context = Context(registration_db)
    _, header = _register_student(api_client)
    program = _make_program_context(registration_db, context.year_id)

    response = api_client.post(
        "/api/v1/me/registrations",
        json=_self_registration_payload(
            context,
            program_version_id=program["program_version_id"],
            school_id=program["school_id"],
        ),
        headers=header,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["program_version_id"] == program["program_version_id"]
    assert body["program_code"] == program["program_code"]
    codes = sorted(s["code"] for s in body["subjects"])
    assert len(codes) == 2
    assert all(s["status"] == "active" for s in body["subjects"])

    # Owner retrieval returns the same summary.
    fetched = api_client.get(
        f"/api/v1/me/registrations/{body['enrollment_id']}", headers=header
    )
    assert fetched.status_code == 200
    assert sorted(s["code"] for s in fetched.json()["subjects"]) == codes


def test_admin_registers_on_behalf_of_student(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)

    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(context, student_id),
        headers=admin_header,
    )
    assert response.status_code == 201, response.text
    assert response.json()["student_id"] == student_id


# --- ownership enforcement (Step 33) ---------------------------------------------


def test_student_spoofing_student_id_refused(api_client, registration_db) -> None:
    """student_id is not a field on POST /me/registrations at all (422)."""
    context = Context(registration_db)
    other_id, _ = _register_student(api_client)
    _, header = _register_student(api_client)

    response = api_client.post(
        "/api/v1/me/registrations",
        json=_self_registration_payload(context, student_id=other_id),
        headers=header,
    )
    assert response.status_code == 422


def test_history_of_another_student_refused(api_client, registration_db) -> None:
    context = Context(registration_db)
    other_id, other_header = _register_student(api_client)
    _, header = _register_student(api_client)

    api_client.post(
        "/api/v1/me/registrations",
        json=_self_registration_payload(context),
        headers=other_header,
    )
    # The admin-namespaced history endpoint refuses a student token outright.
    response = api_client.get(
        f"/api/v1/admin/students/{other_id}/registrations", headers=header
    )
    assert response.status_code == 403
    # The owner still sees their own history through the self-service route.
    own = api_client.get("/api/v1/me/registrations", headers=other_header)
    assert own.status_code == 200
    assert len(own.json()) == 1


def test_enrollment_of_another_student_refused(api_client, registration_db) -> None:
    context = Context(registration_db)
    _, owner_header = _register_student(api_client)
    _, attacker_header = _register_student(api_client)

    created = api_client.post(
        "/api/v1/me/registrations",
        json=_self_registration_payload(context),
        headers=owner_header,
    )
    assert created.status_code == 201
    enrollment_id = created.json()["enrollment_id"]

    other = api_client.get(
        f"/api/v1/me/registrations/{enrollment_id}", headers=attacker_header
    )
    assert other.status_code == 403
    own = api_client.get(f"/api/v1/me/registrations/{enrollment_id}", headers=owner_header)
    assert own.status_code == 200


def test_admin_reads_any_history(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, student_header = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)

    api_client.post(
        "/api/v1/me/registrations",
        json=_self_registration_payload(context),
        headers=student_header,
    )
    response = api_client.get(
        f"/api/v1/admin/students/{student_id}/registrations", headers=admin_header
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_unauthenticated_registration_refused(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    response = api_client.post(
        "/api/v1/admin/registrations", json=_admin_registration_payload(context, student_id)
    )
    assert response.status_code == 401


# --- not-found rules ---------------------------------------------------------------


def test_unknown_student_is_404(api_client, registration_db) -> None:
    context = Context(registration_db)
    admin_header = _register_admin(api_client, registration_db)
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(context, student_id=str(uuid.uuid4())),
        headers=admin_header,
    )
    assert response.status_code == 404
    assert "student" in response.json()["detail"]


def test_unknown_academic_year_is_404(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, admin_header = (
        _register_student(api_client)[0],
        _register_admin(api_client, registration_db),
    )
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(
            context, student_id, academic_year_id=str(uuid.uuid4())
        ),
        headers=admin_header,
    )
    assert response.status_code == 404


def test_unknown_pathway_is_404(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(context, student_id, pathway="NO_SUCH_PATHWAY"),
        headers=admin_header,
    )
    assert response.status_code == 404


def test_unknown_education_level_is_404(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(
            context, student_id, education_level="S99"
        ),
        headers=admin_header,
    )
    assert response.status_code == 404


def test_unknown_school_is_404(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(context, student_id, school_id=str(uuid.uuid4())),
        headers=admin_header,
    )
    assert response.status_code == 404


# --- business-rule errors ------------------------------------------------------------


def test_invalid_pathway_level_pair_is_422(api_client, registration_db) -> None:
    """O_LEVEL is not mapped to S4 in the real pathway_levels rows."""
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(context, student_id, education_level="S4"),
        headers=admin_header,
    )
    assert response.status_code == 422
    assert "does not include" in response.json()["detail"]


def test_invalid_program_version_year_is_422(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)
    wrong_year_program = _make_program_context(registration_db, context.closed_year_id)
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(
            context,
            student_id,
            program_version_id=wrong_year_program["program_version_id"],
            school_id=wrong_year_program["school_id"],
        ),
        headers=admin_header,
    )
    assert response.status_code == 422
    assert "does not belong" in response.json()["detail"]


def test_invalid_program_version_level_is_422(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)
    other_level_program = _make_program_context(
        registration_db, context.year_id, level_code="S1"
    )
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(
            context,
            student_id,
            education_level=context.s2_code,  # valid pair, but version belongs to S1
            program_version_id=other_level_program["program_version_id"],
            school_id=other_level_program["school_id"],
        ),
        headers=admin_header,
    )
    assert response.status_code == 422


def test_school_not_offering_program_is_422(api_client, registration_db) -> None:
    context = Context(registration_db)
    student_id, _ = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)
    program = _make_program_context(
        registration_db, context.year_id, school_offers=False
    )
    response = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(
            context,
            student_id,
            program_version_id=program["program_version_id"],
            school_id=program["school_id"],
        ),
        headers=admin_header,
    )
    assert response.status_code == 422
    assert "does not offer" in response.json()["detail"]


def test_duplicate_enrollment_is_409(api_client, registration_db) -> None:
    context = Context(registration_db)
    _, header = _register_student(api_client)
    first = api_client.post(
        "/api/v1/me/registrations", json=_self_registration_payload(context), headers=header
    )
    assert first.status_code == 201
    duplicate = api_client.post(
        "/api/v1/me/registrations", json=_self_registration_payload(context), headers=header
    )
    assert duplicate.status_code == 409
    assert "already enrolled" in duplicate.json()["detail"]


def test_closed_seed_year_refuses_registration(api_client, registration_db) -> None:
    """The real verified-minimum year 2025/2026 is 'closed' — honest refusal."""
    context = Context(registration_db)
    _, header = _register_student(api_client)
    response = api_client.post(
        "/api/v1/me/registrations",
        json=_self_registration_payload(context, academic_year_id=context.closed_year_id),
        headers=header,
    )
    assert response.status_code == 503
    assert "closed" in response.json()["detail"]


# --- transaction rollback (Step 28) ----------------------------------------------------


def test_rollback_leaves_no_enrollment_or_subjects(api_client, registration_db) -> None:
    """Force a failure after the enrollment insert; prove nothing remains."""
    from app.core.database import SessionLocal
    from app.models.enrollment import StudentEnrollment
    from app.models.student_subject import StudentSubject
    from app.schemas.registration import RegistrationCreate
    from app.services import registration_service
    from unittest.mock import patch

    context = Context(registration_db)
    _, header = _register_student(api_client)
    _, attacker_header = _register_student(api_client)

    # First registration succeeds.
    first = api_client.post(
        "/api/v1/me/registrations", json=_self_registration_payload(context), headers=header
    )
    assert first.status_code == 201

    # Prove rollback on the service path: a forced failure after flush while
    # deriving student subjects from a program version. A fresh student keeps
    # the duplicate-enrollment pre-check out of the way.
    program = _make_program_context(registration_db, context.year_id)
    rollback_student_id, _ = _register_student(api_client)
    payload = RegistrationCreate(
        student_id=uuid.UUID(rollback_student_id),
        academic_year_id=uuid.UUID(context.year_id),
        pathway=context.pathway_code,
        education_level=context.s1_code,
        program_version_id=uuid.UUID(program["program_version_id"]),
        school_id=uuid.UUID(program["school_id"]),
    )
    session = SessionLocal(bind=registration_db)
    try:
        with patch.object(
            registration_service.enrollment_repo,
            "create_student_subjects",
            side_effect=RuntimeError("forced failure during subject derivation"),
        ):
            with pytest.raises(RuntimeError):
                registration_service.register_student(session, payload)
        session.rollback()

        # Nothing from the failed attempt remains.
        enrollments = session.scalars(select(StudentEnrollment)).all()
        subjects = session.scalars(select(StudentSubject)).all()
        assert len(enrollments) == 1  # only the first, successful registration
        assert subjects == []  # the rolled-back attempt left no subjects
        assert enrollments[0].id == uuid.UUID(first.json()["enrollment_id"])
    finally:
        session.close()
    del attacker_header


# --- retrieval and history ---------------------------------------------------------------


def test_registration_retrieval_unknown_id_is_404(api_client, registration_db) -> None:
    _, header = _register_student(api_client)
    response = api_client.get(
        f"/api/v1/me/registrations/{uuid.uuid4()}", headers=header
    )
    assert response.status_code == 404


def test_student_registration_history_ordered(api_client, registration_db) -> None:
    context = Context(registration_db)
    _, header = _register_student(api_client)

    first = api_client.post(
        "/api/v1/me/registrations", json=_self_registration_payload(context), headers=header
    )
    assert first.status_code == 201

    # A second year so the ordering (newest first) is observable.
    from app.core.database import SessionLocal
    from app.models.academic_year import AcademicYear

    session = SessionLocal(bind=registration_db)
    try:
        year = AcademicYear(
            name="2027/2028",
            start_date=date(2027, 9, 1),
            end_date=date(2028, 7, 31),
            status="planned",
        )
        session.add(year)
        session.commit()
        second_year_id = str(year.id)
    finally:
        session.close()

    second = api_client.post(
        "/api/v1/me/registrations",
        json=_self_registration_payload(context, academic_year_id=second_year_id),
        headers=header,
    )
    assert second.status_code == 201

    history = api_client.get("/api/v1/me/registrations", headers=header)
    assert history.status_code == 200
    names = [row["academic_year"] for row in history.json()]
    assert names == ["2027/2028", "2026/2027"]


# --- role-split guard matrix ---------------------------------------------------------------


def test_registration_namespaces_refuse_cross_role_tokens(
    api_client, registration_db
) -> None:
    """Student → 403 on /admin/registrations; admin → 403 on /me/registrations."""
    context = Context(registration_db)
    _, student_header = _register_student(api_client)
    admin_header = _register_admin(api_client, registration_db)

    # Student token on the administrative create → 403 before any handler.
    resp = api_client.post(
        "/api/v1/admin/registrations",
        json=_admin_registration_payload(context, str(uuid.uuid4())),
        headers=student_header,
    )
    assert resp.status_code == 403
    assert "administrator" in resp.json()["detail"]

    # Student token on the administrative history → 403.
    resp = api_client.get(
        f"/api/v1/admin/students/{uuid.uuid4()}/registrations",
        headers=student_header,
    )
    assert resp.status_code == 403

    # Administrator token on the self-service routes → 403.
    resp = api_client.post(
        "/api/v1/me/registrations",
        json=_self_registration_payload(context),
        headers=admin_header,
    )
    assert resp.status_code == 403
    resp = api_client.get("/api/v1/me/registrations", headers=admin_header)
    assert resp.status_code == 403


# --- development-database safety (guard re-check) ------------------------------------------


def test_every_write_targeted_the_test_database(pg_engine) -> None:
    assert _assert_test_database(pg_engine) == "super_teacher_db_test"
