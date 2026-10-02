"""GET /api/v1/admin/registrations — authorization, pagination, filters, N+1.

Covers the administrative registration list on the real router plus the
eager-loading contract of the registration summary: the page query must
keep a constant statement count whether it returns one row or twenty-five
(each row carrying a program version, a program and subjects), and
``GET /admin/registrations/{id}`` must cost the same for one subject or
five.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

LIST_PATH = "/api/v1/admin/registrations"


# --- fixtures ----------------------------------------------------------------------


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


@pytest.fixture()
def reg_db(clean_db):
    """Empty test database seeded with the verified-minimum catalog."""
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry

    name = _current_database(clean_db)
    assert name.endswith("_test"), f"SAFETY REFUSAL: connected to {name!r}"
    session = SessionLocal(bind=clean_db)
    try:
        reports = run_load(session, load_registry())
    finally:
        session.close()
    assert sum(r.writes for r in reports) == 40  # verified-minimum catalog size
    yield clean_db
    # Remove the catalog again so clean_db's zero-row leak guard still holds.
    from tests.conftest import APPLICATION_TABLES

    with clean_db.begin() as connection:
        for table in APPLICATION_TABLES:
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )


def _current_database(engine) -> str:
    with engine.begin() as connection:
        return connection.execute(text("SELECT current_database()")).scalar_one()


# --- principals --------------------------------------------------------------------


def _admin_header(api_client: TestClient, engine) -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    email = f"regs-admin-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        session.add(
            User(
                email=email,
                role="admin",
                status="active",
                password_hash=hash_password(PASSWORD),
            )
        )
        session.commit()
    finally:
        session.close()
    login = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _student_header(api_client: TestClient) -> dict:
    response = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"reglist-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
            "full_name": "Registration List Student",
            "date_of_birth": "2012-04-10",
            "gender": "female",
            "country": "Rwanda",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _teacher_header(api_client: TestClient, engine) -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.teacher import Teacher
    from app.models.user import User

    email = f"reglist-teacher-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        user = User(
            email=email,
            role="teacher",
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        session.flush()
        session.add(Teacher(user_id=user.id, full_name="Registration List Teacher"))
        session.commit()
    finally:
        session.close()
    login = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# --- data builders -----------------------------------------------------------------


class Context:
    """Two years, two schools, one program version — direct test-DB rows.

    Everything is inserted with plain ORM rows (no service validation), so
    the catalog can carry exactly the combinations the list filters need.
    """

    def __init__(self, engine) -> None:
        from app.core.database import SessionLocal
        from app.models.academic_year import AcademicYear
        from app.models.education_level import EducationLevel
        from app.models.pathway import Pathway
        from app.models.program import Program
        from app.models.program_subject import ProgramSubject
        from app.models.program_version import ProgramVersion
        from app.models.school import School
        from app.models.subject import Subject

        session = SessionLocal(bind=engine)
        try:
            pathway = session.scalars(
                select(Pathway).where(Pathway.code == "O_LEVEL")
            ).one()
            level = session.scalars(
                select(EducationLevel).where(EducationLevel.code == "S1")
            ).one()
            subjects = list(session.scalars(select(Subject).limit(5)))
            assert len(subjects) == 5, "seeded catalog must provide 5 subjects"

            self.year_one = AcademicYear(
                name="2026/2027",
                start_date=date(2026, 9, 1),
                end_date=date(2027, 7, 31),
                status="planned",
            )
            self.year_two = AcademicYear(
                name="2027/2028",
                start_date=date(2027, 9, 1),
                end_date=date(2028, 7, 31),
                status="planned",
            )
            self.school_one = School(name="List School One", school_code="LSO000001")
            self.school_two = School(name="List School Two", school_code="LST000002")
            session.add_all(
                [self.year_one, self.year_two, self.school_one, self.school_two]
            )
            session.flush()

            program = Program(code="LSTPRG1", name="List Program")
            session.add(program)
            session.flush()
            self.program_version = ProgramVersion(
                program_id=program.id,
                academic_year_id=self.year_one.id,
                pathway_id=pathway.id,
                education_level_id=level.id,
                code="LSTPRG1_V1",
                name="List Program v1",
            )
            session.add(self.program_version)
            session.flush()
            session.add_all(
                [
                    ProgramSubject(
                        program_version_id=self.program_version.id,
                        subject_id=subject.id,
                        display_order=index,
                    )
                    for index, subject in enumerate(subjects[:2], start=1)
                ]
            )
            session.commit()

            self.pathway_id = pathway.id
            self.level_id = level.id
            self.subject_ids = [subject.id for subject in subjects]
            self.program_id = program.id
        finally:
            session.close()


@pytest.fixture()
def ctx(reg_db) -> Context:
    return Context(reg_db)


def _seed_enrollments(
    engine,
    context: Context,
    count: int,
    *,
    status: str = "pending",
    year=None,
    school=None,
    with_program: bool = True,
    subjects: int = 2,
    name_prefix: str = "Reg Kid",
) -> list[dict]:
    """Insert ``count`` user+student+enrollment(+subject) sets in ONE
    transaction, so every enrollment shares a single ``created_at`` — the
    duplicate sort key the ordering test needs."""
    from app.core.database import SessionLocal
    from app.models.enrollment import StudentEnrollment
    from app.models.student import Student
    from app.models.student_subject import StudentSubject
    from app.models.user import User

    year = year or context.year_one
    school = school or context.school_one
    created: list[dict] = []
    session = SessionLocal(bind=engine)
    try:
        for index in range(count):
            user = User(
                email=f"{name_prefix.lower().replace(' ', '-')}-"
                f"{uuid.uuid4().hex[:10]}@example.com",
                role="student",
                status="active",
                password_hash=None,
            )
            session.add(user)
            session.flush()
            student = Student(
                user_id=user.id, full_name=f"{name_prefix} {index:03d}"
            )
            session.add(student)
            session.flush()
            enrollment = StudentEnrollment(
                student_id=student.id,
                academic_year_id=year.id,
                school_id=school.id,
                pathway_id=context.pathway_id,
                education_level_id=context.level_id,
                program_version_id=(
                    context.program_version.id if with_program else None
                ),
                status=status,
            )
            session.add(enrollment)
            session.flush()
            for subject_id in context.subject_ids[:subjects]:
                session.add(
                    StudentSubject(
                        enrollment_id=enrollment.id,
                        subject_id=subject_id,
                        status="active",
                    )
                )
            created.append(
                {
                    "enrollment_id": str(enrollment.id),
                    "student_id": str(student.id),
                    "user_id": str(user.id),
                }
            )
        session.commit()
    finally:
        session.close()
    return created


# --- query counter -----------------------------------------------------------------


@pytest.fixture()
def query_counter(pg_engine):
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(pg_engine, "before_cursor_execute", _record)


# --- authorization -----------------------------------------------------------------


def test_anonymous_gets_401(api_client, reg_db) -> None:
    assert api_client.get(LIST_PATH).status_code == 401


def test_student_token_gets_403(api_client, reg_db) -> None:
    response = api_client.get(LIST_PATH, headers=_student_header(api_client))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "administrator role required for this operation"


def test_teacher_token_gets_403(api_client, reg_db) -> None:
    header = _teacher_header(api_client, reg_db)
    response = api_client.get(LIST_PATH, headers=header)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "administrator role required for this operation"


def test_admin_gets_the_paging_envelope(api_client, reg_db, ctx) -> None:
    _seed_enrollments(reg_db, ctx, 3)
    admin = _admin_header(api_client, reg_db)

    response = api_client.get(LIST_PATH, headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert len(body["items"]) == 3
    item = body["items"][0]
    # The summary is complete without per-row lazy loads: program and
    # subjects were eager-loaded by the page query.
    assert item["program_name"] == "List Program"
    assert item["program_code"] == "LSTPRG1"
    assert len(item["subjects"]) == 2
    assert {"enrollment_id", "student_id", "status", "school_name"} <= set(item)


# --- pagination bounds -------------------------------------------------------------


def test_limit_and_offset_bounds_are_422(api_client, reg_db) -> None:
    admin = _admin_header(api_client, reg_db)

    assert api_client.get(f"{LIST_PATH}?limit=101", headers=admin).status_code == 422
    assert api_client.get(f"{LIST_PATH}?limit=0", headers=admin).status_code == 422
    assert api_client.get(f"{LIST_PATH}?offset=-1", headers=admin).status_code == 422
    assert (
        api_client.get(f"{LIST_PATH}?limit=100&offset=0", headers=admin).status_code
        == 200
    )


def test_offset_beyond_total_is_empty_with_correct_total(api_client, reg_db, ctx) -> None:
    _seed_enrollments(reg_db, ctx, 3)
    admin = _admin_header(api_client, reg_db)

    response = api_client.get(f"{LIST_PATH}?limit=20&offset=50", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 3
    assert body["offset"] == 50


def test_ordering_is_stable_across_pages_with_duplicate_sort_keys(
    api_client, reg_db, ctx
) -> None:
    _seed_enrollments(reg_db, ctx, 25)
    admin = _admin_header(api_client, reg_db)

    pages = []
    for offset in (0, 10, 20):
        response = api_client.get(
            f"{LIST_PATH}?limit=10&offset={offset}", headers=admin
        )
        assert response.status_code == 200, response.text
        pages.append(response.json())

    assert [p["total"] for p in pages] == [25, 25, 25]
    ids = [item["enrollment_id"] for page in pages for item in page["items"]]
    assert len(ids) == 25
    assert len(set(ids)) == 25  # no duplicates, no gaps across the offset seams

    repeat = api_client.get(f"{LIST_PATH}?limit=10&offset=10", headers=admin)
    assert [i["enrollment_id"] for i in repeat.json()["items"]] == [
        i["enrollment_id"] for i in pages[1]["items"]
    ]


# --- filters -----------------------------------------------------------------------


def test_academic_year_filter(api_client, reg_db, ctx) -> None:
    _seed_enrollments(reg_db, ctx, 2, year=ctx.year_one)
    _seed_enrollments(reg_db, ctx, 3, year=ctx.year_two)
    admin = _admin_header(api_client, reg_db)

    response = api_client.get(
        f"{LIST_PATH}?academic_year_id={ctx.year_two.id}", headers=admin
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert all(
        item["academic_year_id"] == str(ctx.year_two.id) for item in body["items"]
    )


def test_status_filter_and_vocabulary_validation(api_client, reg_db, ctx) -> None:
    _seed_enrollments(reg_db, ctx, 2, status="pending")
    _seed_enrollments(reg_db, ctx, 3, status="cancelled")
    admin = _admin_header(api_client, reg_db)

    response = api_client.get(f"{LIST_PATH}?status=cancelled", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 3
    assert all(i["status"] == "cancelled" for i in response.json()["items"])

    invalid = api_client.get(f"{LIST_PATH}?status=exploded", headers=admin)
    assert invalid.status_code == 422
    assert "status" in invalid.text


def test_school_id_and_school_code_filters(api_client, reg_db, ctx) -> None:
    _seed_enrollments(reg_db, ctx, 2, school=ctx.school_one)
    _seed_enrollments(reg_db, ctx, 4, school=ctx.school_two)
    admin = _admin_header(api_client, reg_db)

    by_id = api_client.get(
        f"{LIST_PATH}?school_id={ctx.school_two.id}", headers=admin
    ).json()
    assert by_id["total"] == 4

    by_code = api_client.get(f"{LIST_PATH}?school_code=LSO000001", headers=admin).json()
    assert by_code["total"] == 2
    assert all(i["school_code"] == "LSO000001" for i in by_code["items"])

    empty_code = api_client.get(f"{LIST_PATH}?school_code=NOPE", headers=admin).json()
    assert empty_code["items"] == [] and empty_code["total"] == 0


def test_student_id_filter(api_client, reg_db, ctx) -> None:
    rows = _seed_enrollments(reg_db, ctx, 3)
    admin = _admin_header(api_client, reg_db)

    response = api_client.get(
        f"{LIST_PATH}?student_id={rows[1]['student_id']}", headers=admin
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["student_id"] == rows[1]["student_id"]


def test_filters_combine(api_client, reg_db, ctx) -> None:
    _seed_enrollments(reg_db, ctx, 2, status="pending", year=ctx.year_one, school=ctx.school_one)
    _seed_enrollments(reg_db, ctx, 5, status="cancelled", year=ctx.year_one, school=ctx.school_one)
    _seed_enrollments(reg_db, ctx, 4, status="cancelled", year=ctx.year_one, school=ctx.school_two)
    _seed_enrollments(reg_db, ctx, 3, status="cancelled", year=ctx.year_two, school=ctx.school_one)
    admin = _admin_header(api_client, reg_db)

    combined = api_client.get(
        f"{LIST_PATH}?academic_year_id={ctx.year_one.id}"
        f"&status=cancelled&school_id={ctx.school_one.id}",
        headers=admin,
    )
    assert combined.status_code == 200, combined.text
    assert combined.json()["total"] == 5

    with_student = api_client.get(
        f"{LIST_PATH}?academic_year_id={ctx.year_one.id}"
        f"&status=cancelled&school_code=LSO000001",
        headers=admin,
    ).json()
    assert with_student["total"] == 5

    nothing = api_client.get(
        f"{LIST_PATH}?academic_year_id={ctx.year_two.id}&status=pending",
        headers=admin,
    ).json()
    assert nothing["items"] == [] and nothing["total"] == 0


# --- query counts ------------------------------------------------------------------


def test_query_count_constant_for_one_and_twenty_five_rows(
    api_client, reg_db, ctx, query_counter
) -> None:
    """Program/program/subjects eager loading keeps statements constant."""
    _seed_enrollments(reg_db, ctx, 1, subjects=2)
    admin = _admin_header(api_client, reg_db)

    before = len(query_counter)
    one = api_client.get(f"{LIST_PATH}?limit=100", headers=admin)
    one_queries = len(query_counter) - before
    assert one.status_code == 200, one.text
    assert one.json()["total"] == 1
    assert len(one.json()["items"][0]["subjects"]) == 2

    _seed_enrollments(reg_db, ctx, 24, subjects=2)

    before = len(query_counter)
    many = api_client.get(f"{LIST_PATH}?limit=100", headers=admin)
    many_queries = len(query_counter) - before
    assert many.status_code == 200, many.text
    assert many.json()["total"] == 25

    assert one_queries == many_queries, (
        f"list query count grew with row count: {one_queries} vs {many_queries}"
    )


def test_get_by_id_query_count_constant_across_subject_counts(
    api_client, reg_db, ctx, query_counter
) -> None:
    """The summary read is fully eager: 1 subject costs the same as 5."""
    thin = _seed_enrollments(reg_db, ctx, 1, subjects=1)[0]
    thick = _seed_enrollments(reg_db, ctx, 1, subjects=5)[0]
    admin = _admin_header(api_client, reg_db)

    before = len(query_counter)
    thin_response = api_client.get(
        f"{LIST_PATH}/{thin['enrollment_id']}", headers=admin
    )
    thin_queries = len(query_counter) - before
    assert thin_response.status_code == 200, thin_response.text
    assert len(thin_response.json()["subjects"]) == 1
    assert thin_response.json()["program_name"] == "List Program"

    before = len(query_counter)
    thick_response = api_client.get(
        f"{LIST_PATH}/{thick['enrollment_id']}", headers=admin
    )
    thick_queries = len(query_counter) - before
    assert thick_response.status_code == 200, thick_response.text
    assert len(thick_response.json()["subjects"]) == 5

    assert thin_queries == thick_queries, (
        f"get-by-id query count grew with subject count: "
        f"{thin_queries} vs {thick_queries}"
    )
