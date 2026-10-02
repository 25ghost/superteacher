"""GET /api/v1/admin/students — authorization, pagination, search, filters.

Covers the administrative student list end to end on the real router:
401/403 role contract, the items/total/limit/offset envelope and its
bounds, deterministic offset paging under duplicate sort keys, literal
wildcard handling in ``q``, vocabulary validation of ``gender``, and a
SQLAlchemy statement counter proving the number of queries per request is
independent of the number of rows returned.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

LIST_PATH = "/api/v1/admin/students"

# Tables this module owns (children first for TRUNCATE CASCADE).
_PROFILE_TABLES = (
    "auth_sessions",
    "student_profile_history",
    "students",
    "users",
)


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


def _truncate(engine) -> None:
    with engine.begin() as connection:
        for table in _PROFILE_TABLES:
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )


@pytest.fixture()
def clean_students(pg_engine):
    """Empty account/profile tables before and after every test."""
    name = _current_database(pg_engine)
    assert name.endswith("_test"), f"SAFETY REFUSAL: connected to {name!r}"
    _truncate(pg_engine)
    yield pg_engine
    _truncate(pg_engine)


def _current_database(engine) -> str:
    with engine.begin() as connection:
        return connection.execute(text("SELECT current_database()")).scalar_one()


# --- principals --------------------------------------------------------------------


def _admin_header(api_client: TestClient, engine) -> dict:
    """An active admin account (direct insert) logged in for a token."""
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    email = f"students-admin-{uuid.uuid4().hex[:8]}@test.example"
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
    """A real student account through the public registration API."""
    response = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"list-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
            "full_name": "List Test Student",
            "date_of_birth": "2012-04-10",
            "gender": "female",
            "country": "Rwanda",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _teacher_header(api_client: TestClient, engine) -> dict:
    """A direct-insert teacher account (user + profile) logged in."""
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.teacher import Teacher
    from app.models.user import User

    email = f"list-teacher-{uuid.uuid4().hex[:8]}@test.example"
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
        session.add(Teacher(user_id=user.id, full_name="List Test Teacher"))
        session.commit()
    finally:
        session.close()
    login = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# --- seeding -----------------------------------------------------------------------


def _seed_students(engine, specs: list[dict]) -> None:
    """Insert user+student pairs in ONE transaction.

    PostgreSQL's ``now()`` is transaction start time, so every row shares
    one ``created_at`` — the duplicate sort key the ordering test needs.
    """
    from app.core.database import SessionLocal
    from app.models.student import Student
    from app.models.user import User

    session = SessionLocal(bind=engine)
    try:
        for spec in specs:
            user = User(
                email=spec["email"],
                role="student",
                status="active",
                password_hash=None,
            )
            session.add(user)
            session.flush()
            session.add(
                Student(
                    user_id=user.id,
                    full_name=spec["full_name"],
                    gender=spec.get("gender"),
                    country=spec.get("country"),
                )
            )
        session.commit()
    finally:
        session.close()


def _specs(count: int, *, prefix: str = "Row") -> list[dict]:
    return [
        {
            "email": f"{prefix.lower()}-{i:03d}@example.com",
            "full_name": f"{prefix} Number {i:03d}",
            "gender": ("female", "male", "other", "undisclosed")[i % 4],
            "country": "Rwanda",
        }
        for i in range(count)
    ]


# --- query counter -----------------------------------------------------------------


@pytest.fixture()
def query_counter(pg_engine):
    """Statements executed on pg_engine while the test runs."""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(pg_engine, "before_cursor_execute", _record)


# --- authorization -----------------------------------------------------------------


def test_anonymous_gets_401(api_client, clean_students) -> None:
    assert api_client.get(LIST_PATH).status_code == 401


def test_student_token_gets_403(api_client, clean_students) -> None:
    response = api_client.get(LIST_PATH, headers=_student_header(api_client))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "administrator role required for this operation"


def test_teacher_token_gets_403(api_client, clean_students) -> None:
    header = _teacher_header(api_client, clean_students)
    response = api_client.get(LIST_PATH, headers=header)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "administrator role required for this operation"


def test_admin_gets_the_paging_envelope(api_client, clean_students) -> None:
    _seed_students(clean_students, _specs(2))
    admin = _admin_header(api_client, clean_students)

    response = api_client.get(LIST_PATH, headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 2
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert {"student_id", "user_id", "email", "full_name", "gender", "country"} <= set(item)


# --- pagination bounds -------------------------------------------------------------


def test_limit_and_offset_bounds_are_422(api_client, clean_students) -> None:
    _seed_students(clean_students, _specs(1))
    admin = _admin_header(api_client, clean_students)

    assert api_client.get(f"{LIST_PATH}?limit=101", headers=admin).status_code == 422
    assert api_client.get(f"{LIST_PATH}?limit=0", headers=admin).status_code == 422
    assert api_client.get(f"{LIST_PATH}?offset=-1", headers=admin).status_code == 422
    # In-range bounds stay 200.
    assert api_client.get(f"{LIST_PATH}?limit=100&offset=0", headers=admin).status_code == 200


def test_offset_beyond_total_is_empty_with_correct_total(api_client, clean_students) -> None:
    _seed_students(clean_students, _specs(3))
    admin = _admin_header(api_client, clean_students)

    response = api_client.get(f"{LIST_PATH}?limit=20&offset=50", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 3
    assert body["limit"] == 20
    assert body["offset"] == 50


def test_ordering_is_stable_across_pages_with_duplicate_sort_keys(api_client, clean_students) -> None:
    """25 rows sharing one created_at page deterministically by id."""
    _seed_students(clean_students, _specs(25))
    admin = _admin_header(api_client, clean_students)

    pages = []
    for offset in (0, 10, 20):
        response = api_client.get(
            f"{LIST_PATH}?limit=10&offset={offset}", headers=admin
        )
        assert response.status_code == 200, response.text
        pages.append(response.json())

    assert [p["total"] for p in pages] == [25, 25, 25]
    ids = [item["student_id"] for page in pages for item in page["items"]]
    assert len(ids) == 25
    assert len(set(ids)) == 25  # no duplicates, no gaps across the offset seams

    # Re-requesting a middle page yields byte-identical ordering.
    repeat = api_client.get(f"{LIST_PATH}?limit=10&offset=10", headers=admin)
    assert [i["student_id"] for i in repeat.json()["items"]] == [
        i["student_id"] for i in pages[1]["items"]
    ]


# --- search ------------------------------------------------------------------------


def test_q_matches_email_case_insensitively(api_client, clean_students) -> None:
    _seed_students(
        clean_students,
        [
            {"email": "Aline.Uwase@Example.COM", "full_name": "Nobody Special"},
            {"email": "other.kid@example.com", "full_name": "Someone Else"},
        ],
    )
    admin = _admin_header(api_client, clean_students)

    response = api_client.get(f"{LIST_PATH}?q=aline.uwase@example.com", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "Aline.Uwase@Example.COM"


def test_q_matches_full_name_case_insensitively(api_client, clean_students) -> None:
    _seed_students(
        clean_students,
        [
            {"email": "one@example.com", "full_name": "Aline Uwase"},
            {"email": "two@example.com", "full_name": "Bereta Grace"},
        ],
    )
    admin = _admin_header(api_client, clean_students)

    response = api_client.get(f"{LIST_PATH}?q=ALINE", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["full_name"] == "Aline Uwase"


def test_q_treats_wildcards_literally(api_client, clean_students) -> None:
    _seed_students(
        clean_students,
        [
            {"email": "pct@example.com", "full_name": "Scholar 100% Sure"},
            {"email": "plain@example.com", "full_name": "Scholar 100 Sure"},
            {"email": "under@example.com", "full_name": "A_B Kid"},
            {"email": "nope@example.com", "full_name": "AXB Kid"},
        ],
    )
    admin = _admin_header(api_client, clean_students)

    percent = api_client.get(f"{LIST_PATH}?q=100%25", headers=admin).json()
    assert percent["total"] == 1
    assert percent["items"][0]["full_name"] == "Scholar 100% Sure"

    underscore = api_client.get(f"{LIST_PATH}?q=a_b", headers=admin).json()
    assert underscore["total"] == 1
    assert underscore["items"][0]["full_name"] == "A_B Kid"

    # A bare '%' must not degenerate into a match-everything pattern: it
    # only matches rows that literally contain a percent sign.
    bare_percent = api_client.get(f"{LIST_PATH}?q=%25", headers=admin).json()
    assert bare_percent["total"] == 1
    assert bare_percent["items"][0]["full_name"] == "Scholar 100% Sure"


# --- filters -----------------------------------------------------------------------


def test_gender_filter_and_vocabulary_validation(api_client, clean_students) -> None:
    _seed_students(clean_students, _specs(4))  # one of each gender
    admin = _admin_header(api_client, clean_students)

    response = api_client.get(f"{LIST_PATH}?gender=female", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["gender"] == "female"

    invalid = api_client.get(f"{LIST_PATH}?gender=alien", headers=admin)
    assert invalid.status_code == 422
    assert "gender" in invalid.text


def test_country_filter(api_client, clean_students) -> None:
    _seed_students(
        clean_students,
        [
            {"email": "rw@example.com", "full_name": "Local Kid", "country": "Rwanda"},
            {"email": "ke@example.com", "full_name": "Visitor Kid", "country": "Kenya"},
        ],
    )
    admin = _admin_header(api_client, clean_students)

    response = api_client.get(f"{LIST_PATH}?country=Kenya", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["country"] == "Kenya"

    combined = api_client.get(
        f"{LIST_PATH}?country=Rwanda&q=local", headers=admin
    ).json()
    assert combined["total"] == 1
    assert combined["items"][0]["full_name"] == "Local Kid"

    empty = api_client.get(f"{LIST_PATH}?country=Togo", headers=admin).json()
    assert empty["items"] == [] and empty["total"] == 0


# --- query count -------------------------------------------------------------------


def test_query_count_constant_for_one_and_twenty_five_rows(
    api_client, clean_students, query_counter
) -> None:
    """One count query + one page query: statement count independent of rows."""
    _seed_students(clean_students, _specs(1, prefix="Single"))
    admin = _admin_header(api_client, clean_students)

    before = len(query_counter)
    one = api_client.get(f"{LIST_PATH}?limit=100", headers=admin)
    one_queries = len(query_counter) - before
    assert one.status_code == 200, one.text
    assert one.json()["total"] == 1 and len(one.json()["items"]) == 1

    _seed_students(clean_students, _specs(24, prefix="Bulk"))

    before = len(query_counter)
    many = api_client.get(f"{LIST_PATH}?limit=100", headers=admin)
    many_queries = len(query_counter) - before
    assert many.status_code == 200, many.text
    assert many.json()["total"] == 25 and len(many.json()["items"]) == 25

    assert one_queries == many_queries, (
        f"list query count grew with row count: {one_queries} vs {many_queries}"
    )
