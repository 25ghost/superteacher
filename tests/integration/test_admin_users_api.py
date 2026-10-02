"""Administrative user-management integration tests (PostgreSQL, guarded test DB).

End-to-end contract for ``/admin/teachers`` and ``/admin/users/{id}/role``
against ``super_teacher_db_test`` (never the development database):

- an administrator creates a teacher: 201, ``role=teacher``,
  ``status=pending``, no password hash, one ``invite_tokens`` row, and an
  ``auth_events`` row naming the acting administrator;
- a pending account cannot log in (401) — the invitation must be accepted
  first;
- the cross-role matrix: anonymous → 401, student → 403, teacher → 403,
  admin → served, for every route in the slice.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

# Tables this module's fixtures own (children first for TRUNCATE CASCADE).
_ACCOUNT_TABLES = (
    "auth_sessions",
    "invite_tokens",
    "auth_events",
    "student_subjects",
    "student_enrollments",
    "students",
    "teachers",
    "users",
)


# --- scaffolding ---------------------------------------------------------------------


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
        for table in _ACCOUNT_TABLES:
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )


@pytest.fixture()
def accounts(pg_engine):
    """Truncated account tables, guarded against a non-test database."""
    with pg_engine.begin() as connection:
        name = connection.execute(text("SELECT current_database()")).scalar_one()
    assert name.endswith("_test"), f"SAFETY REFUSAL: connected to {name!r}"
    _truncate(pg_engine)
    yield pg_engine
    _truncate(pg_engine)


def _seed_user(engine, *, role: str, status: str = "active") -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password

    email = f"{role}-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        from app.models.user import User

        account = User(
            email=email,
            role=role,
            status=status,
            password_hash=hash_password(PASSWORD),
        )
        session.add(account)
        session.commit()
        user_id = account.id
    finally:
        session.close()
    return {"email": email, "password": PASSWORD, "user_id": str(user_id)}


def _login(api_client: TestClient, account: dict) -> dict:
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _header(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --- creation flow ---------------------------------------------------------------------


def test_admin_creates_pending_teacher_with_invite_and_audit(
    accounts, api_client: TestClient
) -> None:
    admin = _seed_user(accounts, role="admin")
    admin_header = _header(_login(api_client, admin))
    email = f"invite-{uuid.uuid4().hex[:8]}@test.example"

    response = api_client.post(
        "/api/v1/admin/teachers",
        json={"email": email, "full_name": "Invited Teacher", "subject": "Physics"},
        headers=admin_header,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "teacher"
    assert body["status"] == "pending"
    assert body["email"] == email
    # No credential material of any kind in the response.
    assert "password" not in response.text.lower()
    assert "token" not in response.text.lower()

    from app.core.database import SessionLocal

    session = SessionLocal(bind=accounts)
    try:
        user_row = session.execute(
            text("SELECT id, role, status, password_hash FROM users WHERE email = :e"),
            {"e": email},
        ).one()
        assert user_row.role == "teacher"
        assert user_row.status == "pending"
        assert user_row.password_hash is None

        invite_count = session.execute(
            text("SELECT count(*) FROM invite_tokens WHERE user_id = :i"),
            {"i": user_row.id},
        ).scalar_one()
        assert invite_count == 1

        event = session.execute(
            text(
                "SELECT event_type, actor_user_id FROM auth_events "
                "WHERE user_id = :i AND event_type = 'teacher_created'"
            ),
            {"i": user_row.id},
        ).one()
        assert event.actor_user_id is not None
        assert str(event.actor_user_id) == admin["user_id"]
    finally:
        session.close()


def test_pending_teacher_cannot_log_in(accounts, api_client: TestClient) -> None:
    admin = _seed_user(accounts, role="admin")
    admin_header = _header(_login(api_client, admin))
    email = f"pending-{uuid.uuid4().hex[:8]}@test.example"

    created = api_client.post(
        "/api/v1/admin/teachers",
        json={"email": email, "full_name": "Pending Teacher"},
        headers=admin_header,
    )
    assert created.status_code == 201, created.text

    # No password yet, and the status gate refuses non-active accounts.
    response = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 401, response.text


# --- teacher list (one joined query, no N+1) ----------------------------------------------


def _seed_teachers(engine, count: int) -> None:
    """Direct ORM teacher accounts + profiles (no invitation emails)."""
    from app.core.database import SessionLocal
    from app.models.teacher import Teacher
    from app.models.user import User

    session = SessionLocal(bind=engine)
    try:
        for index in range(count):
            user = User(
                email=f"list-{uuid.uuid4().hex[:8]}@test.example",
                role="teacher",
                status="active",
                password_hash=None,
            )
            session.add(user)
            session.flush()
            session.add(Teacher(user_id=user.id, full_name=f"List Teacher {index}"))
        session.commit()
    finally:
        session.close()


def test_teacher_list_query_count_constant_and_profile_complete(
    accounts, api_client: TestClient
) -> None:
    """Account and profile come from ONE joined statement.

    The list used to look up each teacher's profile row separately, so
    its statement count grew with its size (N+1); both run lengths must
    now cost exactly the same, and no statement may take the per-row
    ``FROM teachers WHERE teachers.user_id`` shape.
    """
    from sqlalchemy import event

    admin = _seed_user(accounts, role="admin")
    admin_header = _header(_login(api_client, admin))
    _seed_teachers(accounts, 1)

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(accounts, "before_cursor_execute", _record)
    try:
        before = len(statements)
        one = api_client.get("/api/v1/admin/teachers", headers=admin_header)
        one_queries = len(statements) - before

        _seed_teachers(accounts, 24)

        before = len(statements)
        many = api_client.get(
            "/api/v1/admin/teachers?limit=200", headers=admin_header
        )
        many_queries = len(statements) - before
    finally:
        event.remove(accounts, "before_cursor_execute", _record)

    assert one.status_code == 200, one.text
    assert many.status_code == 200, many.text
    assert len(one.json()) == 1
    assert len(many.json()) == 25

    sample = many.json()[0]
    assert {
        "user_id",
        "teacher_id",
        "email",
        "full_name",
        "role",
        "status",
    } <= set(sample)
    assert all(item["role"] == "teacher" for item in many.json())

    assert one_queries > 0, "no statements were captured — the test proves nothing"
    assert one_queries == many_queries, (
        f"teacher list query count grew with row count: "
        f"{one_queries} (1 row) vs {many_queries} (25 rows)"
    )
    per_row_lookups = [
        statement
        for statement in statements
        if "FROM teachers WHERE teachers.user_id" in statement
    ]
    assert per_row_lookups == [], per_row_lookups


# --- cross-role matrix ------------------------------------------------------------------


def _routes(target_user_id: str, teacher_email: str) -> list[tuple[str, str, dict | None]]:
    return [
        (
            "POST",
            "/api/v1/admin/teachers",
            {"email": teacher_email, "full_name": "Route Teacher"},
        ),
        ("GET", "/api/v1/admin/teachers", None),
        ("POST", f"/api/v1/admin/teachers/{target_user_id}/invite", {}),
        ("POST", f"/api/v1/admin/teachers/{target_user_id}/activate", {}),
        ("POST", f"/api/v1/admin/teachers/{target_user_id}/deactivate", {}),
        ("PATCH", f"/api/v1/admin/users/{target_user_id}/role", {"role": "teacher"}),
    ]


@pytest.fixture()
def target_teacher(accounts) -> dict:
    """An active teacher account to point the admin routes at."""
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.teacher import Teacher
    from app.models.user import User

    session = SessionLocal(bind=accounts)
    try:
        user = User(
            email=f"target-{uuid.uuid4().hex[:8]}@test.example",
            role="teacher",
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        session.flush()
        session.add(Teacher(user_id=user.id, full_name="Target Teacher"))
        session.commit()
        return {
            "email": user.email,
            "password": PASSWORD,
            "user_id": str(user.id),
        }
    finally:
        session.close()


def test_admin_routes_refuse_anonymous(
    accounts, target_teacher: dict, api_client: TestClient
) -> None:
    for method, path, body in _routes(
        target_teacher["user_id"], f"anon-{uuid.uuid4().hex[:8]}@test.example"
    ):
        response = api_client.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path} -> {response.text}"


def test_admin_routes_refuse_students(
    accounts, target_teacher: dict, api_client: TestClient
) -> None:
    student = _seed_user(accounts, role="student")
    headers = _header(_login(api_client, student))
    for method, path, body in _routes(
        target_teacher["user_id"], f"stu-{uuid.uuid4().hex[:8]}@test.example"
    ):
        response = api_client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403, f"{method} {path} -> {response.text}"


def test_admin_routes_refuse_teachers(
    accounts, target_teacher: dict, api_client: TestClient
) -> None:
    teacher = _seed_user(accounts, role="teacher")
    headers = _header(_login(api_client, teacher))
    for method, path, body in _routes(
        target_teacher["user_id"], f"tch-{uuid.uuid4().hex[:8]}@test.example"
    ):
        response = api_client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403, f"{method} {path} -> {response.text}"


def test_admin_routes_serve_administrators(
    accounts, target_teacher: dict, api_client: TestClient
) -> None:
    admin = _seed_user(accounts, role="admin")
    headers = _header(_login(api_client, admin))
    for method, path, body in _routes(
        target_teacher["user_id"], f"ok-{uuid.uuid4().hex[:8]}@test.example"
    ):
        response = api_client.request(method, path, json=body, headers=headers)
        assert response.status_code not in (401, 403), (
            f"{method} {path} -> {response.status_code}: {response.text}"
        )


def test_role_change_is_refused_for_self(
    accounts, api_client: TestClient
) -> None:
    admin = _seed_user(accounts, role="admin")
    headers = _header(_login(api_client, admin))
    response = api_client.patch(
        f"/api/v1/admin/users/{admin['user_id']}/role",
        json={"role": "student"},
        headers=headers,
    )
    assert response.status_code == 403, response.text


def test_role_change_is_422_outside_the_vocabulary(
    accounts, api_client: TestClient
) -> None:
    admin = _seed_user(accounts, role="admin")
    other = _seed_user(accounts, role="student")
    headers = _header(_login(api_client, admin))
    response = api_client.patch(
        f"/api/v1/admin/users/{other['user_id']}/role",
        json={"role": "superuser"},
        headers=headers,
    )
    assert response.status_code == 422, response.text
