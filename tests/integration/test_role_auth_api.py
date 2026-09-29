"""Role authorization integration tests (PostgreSQL, opt-in, guarded test DB).

Verifies the three-role contract end-to-end against ``super_teacher_db_test``
(never the development database):

- exactly three roles exist at the database level (``users_role_check``
  refuses anything else, legacy values included),
- ``student`` / ``teacher`` / ``admin`` accounts can all log in and receive
  a protected access token,
- ``GET /api/v1/auth/me`` returns the *authenticated* caller for every role,
  links students to their ``students`` row, and ignores any id supplied by
  the client,
- role guards on real routes: student-only, admin-only — 401 without
  credentials, 403 for the wrong role (the teacher-only and teacher/admin
  combinations are asserted against probe routes in
  ``tests/unit/test_role_authorization.py``),
- the ``role`` claim inside a JWT is never trusted: authorization reads the
  role from the database.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


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


def _truncate_account_tables(engine) -> None:
    """Empty every account-owned table (children first), then verify."""
    with engine.begin() as connection:
        for table in (
            "auth_sessions",
            "student_subjects",
            "student_enrollments",
            "students",
            "users",
        ):
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )


@pytest.fixture()
def accounts(pg_engine):
    """Truncated account tables, guarded against touching a non-test DB."""
    with pg_engine.begin() as connection:
        name = connection.execute(text("SELECT current_database()")).scalar_one()
    assert name.endswith("_test"), f"SAFETY REFUSAL: connected to {name!r}"
    _truncate_account_tables(pg_engine)
    yield pg_engine
    _truncate_account_tables(pg_engine)  # leak-guard: no account row survives


def _seed_user(engine, *, role: str, with_student: bool = False) -> dict:
    """Insert a user (and optionally a linked student profile) directly.

    Returns the credentials/identities the tests need. Provisioning teacher
    and admin accounts outside the API is deliberate: registration is
    student-only by design.
    """
    from app.core.database import SessionLocal
    from app.core.security import hash_password

    email = f"{role}-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        from app.models.student import Student
        from app.models.user import User

        account = User(
            email=email,
            role=role,
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(account)
        session.flush()
        user_id = account.id
        student_id = None
        if with_student:
            profile = Student(user_id=account.id, full_name="Seeded Student")
            session.add(profile)
            session.flush()
            student_id = profile.id
        session.commit()
    finally:
        session.close()
    return {"email": email, "password": PASSWORD, "user_id": user_id, "student_id": student_id}


def _login(api_client: TestClient, account: dict) -> dict:
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _header(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _register_student(api_client: TestClient) -> dict:
    response = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"student-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
            "full_name": "Integration Student",
            "date_of_birth": "2012-04-10",
            "gender": "female",
            "country": "Rwanda",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- the vocabulary is enforced by the database --------------------------------------


def test_database_refuses_roles_outside_the_vocabulary(accounts) -> None:
    """Only student/teacher/admin can be stored — legacy values included."""
    connection = accounts.connect()
    try:
        for legacy_role in ("parent", "school_admin", "rahura_admin", "superuser"):
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO users (id, email, role, status, password_hash, "
                        "created_at, updated_at) "
                        "VALUES (:id, :email, :role, 'active', NULL, now(), now())"
                    ),
                    {"id": str(uuid.uuid4()), "email": f"{legacy_role}@x.test", "role": legacy_role},
                )
            connection.rollback()  # clear the aborted transaction
    finally:
        connection.close()


def test_allowed_roles_are_exactly_three(accounts) -> None:
    with accounts.connect() as connection:
        sql = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'users_role_check'"
            )
        ).scalar_one()
    for role in ("student", "teacher", "admin"):
        assert f"'{role}'" in sql
    for legacy in ("parent", "school_admin", "rahura_admin"):
        assert legacy not in sql


# --- login: every allowed role gets a protected access token --------------------------


def test_every_role_can_login_and_get_an_access_token(api_client, accounts) -> None:
    teacher = _seed_user(accounts, role="teacher")
    admin = _seed_user(accounts, role="admin")

    for account, role in ((teacher, "teacher"), (admin, "admin")):
        tokens = _login(api_client, account)
        assert tokens["token_type"] == "bearer"
        assert tokens["access_token"] and tokens["refresh_token"]
        me = api_client.get("/api/v1/auth/me", headers=_header(tokens))
        assert me.status_code == 200
        assert me.json()["role"] == role


def test_student_login_still_works(api_client, accounts) -> None:
    tokens = _register_student(api_client)
    me = api_client.get("/api/v1/auth/me", headers=_header(tokens))
    assert me.status_code == 200
    assert me.json()["role"] == "student"


# --- GET /api/v1/auth/me ---------------------------------------------------------------


@pytest.mark.parametrize("role", ["student", "teacher", "admin"])
def test_auth_me_returns_the_authenticated_caller(api_client, accounts, role) -> None:
    account = _seed_user(accounts, role=role, with_student=(role == "student"))
    tokens = _login(api_client, account)

    response = api_client.get("/api/v1/auth/me", headers=_header(tokens))
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(account["user_id"])
    assert body["email"] == account["email"]
    assert body["role"] == role
    assert body["status"] == "active"
    # No credential material ever leaves the API.
    assert "password" not in str(body).lower()
    assert "hash" not in str(body).lower()


def test_auth_me_matches_the_existing_me_endpoint(api_client, accounts) -> None:
    account = _seed_user(accounts, role="teacher")
    tokens = _login(api_client, account)
    auth_me = api_client.get("/api/v1/auth/me", headers=_header(tokens))
    me = api_client.get("/api/v1/me", headers=_header(tokens))
    assert auth_me.status_code == me.status_code == 200
    assert auth_me.json() == me.json()


def test_auth_me_links_the_student_to_the_students_row(api_client, accounts) -> None:
    account = _seed_user(accounts, role="student", with_student=True)
    tokens = _login(api_client, account)

    body = api_client.get("/api/v1/auth/me", headers=_header(tokens)).json()
    assert body["student"] is not None
    assert body["student"]["student_id"] == str(account["student_id"])

    with accounts.connect() as connection:
        owner = connection.execute(
            text("SELECT user_id FROM students WHERE id = :sid"),
            {"sid": str(account["student_id"])},
        ).scalar_one()
    assert str(owner) == body["user_id"]


@pytest.mark.parametrize("role", ["teacher", "admin"])
def test_auth_me_has_no_student_profile_for_non_students(
    api_client, accounts, role
) -> None:
    account = _seed_user(accounts, role=role)
    tokens = _login(api_client, account)
    body = api_client.get("/api/v1/auth/me", headers=_header(tokens)).json()
    assert body["student"] is None


def test_auth_me_ignores_client_supplied_ids(api_client, accounts) -> None:
    """Identity comes from the token — query/body ids are not accepted."""
    victim = _seed_user(accounts, role="student", with_student=True)
    attacker = _seed_user(accounts, role="teacher")
    tokens = _login(api_client, attacker)

    spoofed = api_client.get(
        "/api/v1/auth/me",
        params={
            "user_id": str(victim["user_id"]),
            "student_id": str(victim["student_id"]),
            "role": "admin",
        },
        headers=_header(tokens),
    )
    assert spoofed.status_code == 200
    body = spoofed.json()
    assert body["user_id"] == str(attacker["user_id"])
    assert body["role"] == "teacher"
    assert body["student"] is None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer not.a.jwt"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
    ],
)
def test_auth_me_requires_valid_credentials(api_client, accounts, headers) -> None:
    response = api_client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_auth_me_rejects_a_refresh_token(api_client, accounts) -> None:
    account = _seed_user(accounts, role="admin")
    tokens = _login(api_client, account)
    response = api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert response.status_code == 401


def test_auth_me_rejects_a_disabled_account(api_client, accounts) -> None:
    account = _seed_user(accounts, role="teacher")
    tokens = _login(api_client, account)
    with accounts.begin() as connection:
        connection.execute(
            text("UPDATE users SET status = 'disabled' WHERE id = :id"),
            {"id": str(account["user_id"])},
        )
    response = api_client.get("/api/v1/auth/me", headers=_header(tokens))
    assert response.status_code == 401


# --- role guards on the real routes ---------------------------------------------------


def test_admin_route_refuses_student_and_teacher(api_client, accounts) -> None:
    student = _seed_user(accounts, role="student", with_student=True)
    teacher = _seed_user(accounts, role="teacher")
    admin = _seed_user(accounts, role="admin")

    target = str(student["student_id"])
    path = f"/api/v1/admin/students/{target}"

    student_header = _header(_login(api_client, student))
    teacher_header = _header(_login(api_client, teacher))
    admin_header = _header(_login(api_client, admin))

    for header, role in ((student_header, "student"), (teacher_header, "teacher")):
        response = api_client.get(path, headers=header)
        assert response.status_code == 403, role
        assert response.json()["detail"] == "administrator role required for this operation"

    allowed = api_client.get(path, headers=admin_header)
    assert allowed.status_code == 200
    assert allowed.json()["full_name"] == "Seeded Student"


def test_student_self_service_refuses_teacher_and_admin(api_client, accounts) -> None:
    teacher = _seed_user(accounts, role="teacher")
    admin = _seed_user(accounts, role="admin")

    for account in (teacher, admin):
        header = _header(_login(api_client, account))
        assert api_client.get("/api/v1/me/student", headers=header).status_code == 403
        assert (
            api_client.post(
                "/api/v1/me/student",
                json={"full_name": "Should Not Exist"},
                headers=header,
            ).status_code
            == 403
        )


def test_student_self_service_works_for_students(api_client, accounts) -> None:
    tokens = _register_student(api_client)
    response = api_client.get("/api/v1/me/student", headers=_header(tokens))
    assert response.status_code == 200
    assert response.json()["role"] == "student"


def test_unauthenticated_admin_and_student_routes_are_401(api_client, accounts) -> None:
    unknown = str(uuid.uuid4())
    for method, path in (
        ("get", "/api/v1/auth/me"),
        ("get", f"/api/v1/admin/students/{unknown}"),
        ("get", "/api/v1/me/student"),
        ("post", "/api/v1/me/student"),
    ):
        response = getattr(api_client, method)(path)
        assert response.status_code == 401, f"{method.upper()} {path}"


# --- the token's role claim is never trusted ------------------------------------------


def test_forged_role_claim_cannot_reach_admin_routes(api_client, accounts) -> None:
    """A student token that *claims* ``admin`` is still only a student."""
    from app.core import security

    account = _seed_user(accounts, role="student", with_student=True)
    forged = security.create_access_token(account["user_id"], "admin")
    headers = {"Authorization": f"Bearer {forged}"}

    response = api_client.get(
        f"/api/v1/admin/students/{account['student_id']}", headers=headers
    )
    assert response.status_code == 403

    # ... while /auth/me still reports the database role.
    me = api_client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "student"
