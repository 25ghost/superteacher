"""Student profile API integration tests (PostgreSQL, opt-in, guarded test DB).

Phase 5G contract: the profile API is **authenticated and role-split**.

- profile creation is administrative (admin role) under
  ``/admin/students`` — the old unauthenticated dev-stage POST is gone;
  self-service account creation is ``POST /auth/register``, and an
  authenticated student without a profile can attach one with
  ``POST /me/student``,
- reads/updates are role-split: students only through ``/me/student``,
  administrators only through ``/admin/students`` — no route serves both,
- unauthenticated callers are refused with 401.

Safety model unchanged: runs against ``super_teacher_db_test`` only
(ENVIRONMENT=testing + ``*_test`` database guard), truncates account
tables around every test, and verifies zero leaked rows afterwards.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration


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
def clean_tables(pg_engine):
    """Empty account tables before and after each test (catalog untouched)."""
    profile_tables = ("auth_sessions", "student_profile_history", "students", "users")

    def _truncate(engine):
        with engine.begin() as connection:
            for table in profile_tables:
                connection.execute(
                    text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
                )

    _truncate(pg_engine)
    yield pg_engine
    _truncate(pg_engine)  # guarantee: users = 0, students = 0 after every test


def _register_student(api_client: TestClient, email: str | None = None) -> dict:
    """A real student account through the public registration API."""
    response = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email or f"kid-{uuid.uuid4().hex[:8]}@example.com",
            "password": "correct horse battery staple",
            "full_name": "Aline Uwase",
            "date_of_birth": "2012-04-10",
            "gender": "female",
            "country": "Rwanda",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _admin_header(pg_engine) -> dict:
    """Create an admin directly in the test DB (callers log it in)."""
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    session = SessionLocal(bind=pg_engine)
    try:
        email = "platform-admin@test.example"
        user = User(
            email=email,
            role="admin",
            status="active",
            password_hash=hash_password("admin test passphrase"),
        )
        session.add(user)
        session.commit()
        assert session.scalars(select(User).where(User.email == email)).one()
    finally:
        session.close()
    return {"Authorization": "Bearer admin-login-pending"}


def test_create_and_retrieve_student_profile(api_client, clean_tables) -> None:
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = api_client.get("/api/v1/me", headers=header)
    assert me.status_code == 200
    body = me.json()
    assert body["student"]["full_name"] == "Aline Uwase"
    assert body["role"] == "student"

    student_id = body["student"]["student_id"]
    fetched = api_client.get("/api/v1/me/student", headers=header)
    assert fetched.status_code == 200
    assert fetched.json()["student_id"] == student_id
    assert fetched.json()["country"] == "Rwanda"


def test_unauthenticated_write_refused(api_client, clean_tables) -> None:
    response = api_client.post(
        "/api/v1/admin/students",
        json={
            "email": "no-auth@example.com",
            "full_name": "No Auth",
            "date_of_birth": "2012-04-10",
        },
    )
    assert response.status_code == 401


def test_unauthenticated_read_refused(api_client, clean_tables) -> None:
    response = api_client.get(f"/api/v1/admin/students/{uuid.uuid4()}")
    assert response.status_code == 401


def test_student_blocked_from_admin_namespace(api_client, clean_tables) -> None:
    """A student token never reaches the admin handlers (403, any id)."""
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    other = _register_student(api_client)
    other_header = {"Authorization": f"Bearer {other['access_token']}"}
    other_id = api_client.get("/api/v1/me/student", headers=other_header).json()["student_id"]

    # Reading another student's profile via the admin route → 403 (role).
    assert (
        api_client.get(f"/api/v1/admin/students/{other_id}", headers=header).status_code
        == 403
    )
    # Unknown ids are also 403 for students — the role guard runs first.
    assert (
        api_client.get(f"/api/v1/admin/students/{uuid.uuid4()}", headers=header).status_code
        == 403
    )
    # Same contract on PATCH.
    patch = api_client.patch(
        f"/api/v1/admin/students/{uuid.uuid4()}",
        json={"full_name": "Ghost"},
        headers=header,
    )
    assert patch.status_code == 403


def test_admin_404_contract_for_unknown_ids(api_client, clean_tables) -> None:
    """Unknown ids are 404 for an administrator — never a 500 (ProfileError)."""
    _admin_header(clean_tables)
    login = api_client.post(
        "/api/v1/auth/login",
        json={"email": "platform-admin@test.example", "password": "admin test passphrase"},
    )
    admin_header = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (
        api_client.get(f"/api/v1/admin/students/{uuid.uuid4()}", headers=admin_header).status_code
        == 404
    )
    patch = api_client.patch(
        f"/api/v1/admin/students/{uuid.uuid4()}",
        json={"full_name": "Ghost"},
        headers=admin_header,
    )
    assert patch.status_code == 404


def test_admin_can_create_bare_profile(api_client, clean_tables) -> None:
    """Administrative creation still works with explicit authorization."""
    admin_header = _admin_header(clean_tables)
    # Log the admin in through the real login endpoint.
    login = api_client.post(
        "/api/v1/auth/login",
        json={"email": "platform-admin@test.example", "password": "admin test passphrase"},
    )
    assert login.status_code == 200
    admin_header = {"Authorization": f"Bearer {login.json()['access_token']}"}

    email = f"admin-made-{uuid.uuid4().hex[:8]}@example.com"
    response = api_client.post(
        "/api/v1/admin/students",
        json={
            "email": email,
            "full_name": "Admin Created",
            "date_of_birth": "2012-04-10",
        },
        headers=admin_header,
    )
    assert response.status_code == 201
    student_id = response.json()["student_id"]

    # Creation is audited and attributed to the acting administrator.
    history = api_client.get(
        f"/api/v1/admin/students/{student_id}/history", headers=admin_header
    )
    assert history.status_code == 200
    creates = [r for r in history.json() if r["change_type"] == "create"]
    assert len(creates) == 1
    assert creates[0]["field_name"] == "profile"
    assert creates[0]["changed_by_email"] == "platform-admin@test.example"
    del admin_header


def test_admin_create_requires_email(api_client, clean_tables) -> None:
    """Phone-only identities are refused — they could never authenticate."""
    _admin_header(clean_tables)
    login = api_client.post(
        "/api/v1/auth/login",
        json={"email": "platform-admin@test.example", "password": "admin test passphrase"},
    )
    assert login.status_code == 200
    admin_header = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = api_client.post(
        "/api/v1/admin/students",
        json={
            "phone": "+250700000999",
            "full_name": "Phone Only",
            "date_of_birth": "2012-04-10",
        },
        headers=admin_header,
    )
    assert response.status_code == 422


def test_update_own_profile_via_me(api_client, clean_tables) -> None:
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = api_client.patch(
        "/api/v1/me/student", json={"country": "Kenya"}, headers=header
    )
    assert response.status_code == 200
    assert response.json()["country"] == "Kenya"
    # date_of_birth stays immutable.
    assert response.json()["date_of_birth"] == "2012-04-10"


def test_catalog_reference_data_untouched(api_client, clean_tables) -> None:
    """Profile operations must never alter the reference catalog."""
    catalog_tables = (
        "academic_years", "pathways", "education_levels", "pathway_levels",
        "subjects", "programs", "program_versions", "program_subjects",
        "tvet_sectors", "tvet_programs", "schools", "school_programs",
    )

    def _counts(engine) -> dict:
        with engine.begin() as connection:
            return {
                table: connection.execute(
                    text(f'SELECT count(*) FROM "{table}"')
                ).scalar_one()
                for table in catalog_tables
            }

    before = _counts(clean_tables)
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert api_client.get("/api/v1/me/student", headers=header).status_code == 200
    after = _counts(clean_tables)
    assert before == after


def test_admin_patch_creates_audit_trail(api_client, clean_tables) -> None:
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = api_client.get("/api/v1/me/student", headers=header).json()
    student_id = me["student_id"]

    # Create an admin and log in
    admin_header = _admin_header(clean_tables)
    login = api_client.post(
        "/api/v1/auth/login",
        json={"email": "platform-admin@test.example", "password": "admin test passphrase"},
    )
    assert login.status_code == 200
    admin_header = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Admin PATCHes the student profile
    resp = api_client.patch(
        f"/api/v1/admin/students/{student_id}",
        json={"full_name": "Admin Renamed"},
        headers=admin_header,
    )
    assert resp.status_code == 200

    # Verify audit trail endpoint returns the change (plus the create row
    # that registration itself wrote).
    history = api_client.get(
        f"/api/v1/admin/students/{student_id}/history",
        headers=admin_header,
    )
    assert history.status_code == 200
    records = history.json()
    update_rows = [r for r in records if r["change_type"] == "update"]
    create_rows = [r for r in records if r["change_type"] == "create"]
    assert len(create_rows) == 1
    assert len(update_rows) == 1
    row = update_rows[0]
    assert row["field_name"] == "full_name"
    assert row["old_value"] == "Aline Uwase"
    assert row["new_value"] == "Admin Renamed"
    # The actor is resolved to a human-readable email, not a bare UUID.
    assert row["changed_by_email"] == "platform-admin@test.example"


def test_history_pagination(api_client, clean_tables) -> None:
    """limit/offset are honoured on the history endpoint."""
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    student_id = api_client.get("/api/v1/me/student", headers=header).json()["student_id"]

    _admin_header(clean_tables)
    login = api_client.post(
        "/api/v1/auth/login",
        json={"email": "platform-admin@test.example", "password": "admin test passphrase"},
    )
    admin_header = {"Authorization": f"Bearer {login.json()['access_token']}"}

    for name in ("Name One", "Name Two", "Name Three"):
        resp = api_client.patch(
            f"/api/v1/admin/students/{student_id}",
            json={"full_name": name},
            headers=admin_header,
        )
        assert resp.status_code == 200

    # 1 create + 3 updates = 4 total
    full = api_client.get(
        f"/api/v1/admin/students/{student_id}/history?limit=200", headers=admin_header
    )
    assert full.status_code == 200
    assert len(full.json()) == 4

    page = api_client.get(
        f"/api/v1/admin/students/{student_id}/history?limit=2&offset=0",
        headers=admin_header,
    )
    assert page.status_code == 200
    assert len(page.json()) == 2

    rest = api_client.get(
        f"/api/v1/admin/students/{student_id}/history?limit=2&offset=2",
        headers=admin_header,
    )
    assert rest.status_code == 200
    assert len(rest.json()) == 2

    # Bounds are enforced (limit must be 1..200, offset >= 0).
    assert (
        api_client.get(
            f"/api/v1/admin/students/{student_id}/history?limit=0", headers=admin_header
        ).status_code
        == 422
    )
    assert (
        api_client.get(
            f"/api/v1/admin/students/{student_id}/history?limit=201", headers=admin_header
        ).status_code
        == 422
    )


def test_student_cannot_read_history(api_client, clean_tables) -> None:
    """History is administrative — a student gets 403 on their own history."""
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    student_id = api_client.get("/api/v1/me/student", headers=header).json()["student_id"]
    resp = api_client.get(f"/api/v1/admin/students/{student_id}/history", headers=header)
    assert resp.status_code == 403


def test_explicit_null_clears_country(api_client, clean_tables) -> None:
    """PATCH with gender/country: null clears; omission leaves untouched."""
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = api_client.get("/api/v1/me/student", headers=header).json()
    assert me["country"] == "Rwanda"
    assert me["gender"] == "female"

    # Omitted fields stay; supplied null clears country.
    resp = api_client.patch(
        "/api/v1/me/student", json={"country": None}, headers=header
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["country"] is None
    assert body["gender"] == "female"  # untouched
    assert body["full_name"] == "Aline Uwase"

    # full_name cannot be cleared (NOT NULL column).
    bad = api_client.patch(
        "/api/v1/me/student", json={"full_name": None}, headers=header
    )
    assert bad.status_code == 422


def test_duplicate_email_returns_409(api_client, clean_tables) -> None:
    email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
    first = _register_student(api_client, email=email)
    assert first  # registered

    # Second registration with the same email should fail
    dup = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "full_name": "Duplicate",
            "date_of_birth": "2012-04-10",
            "gender": "male",
            "country": "Rwanda",
        },
    )
    assert dup.status_code == 409
    # Generic detail — no email enumeration on the public endpoint.
    detail = dup.json()["detail"]
    assert detail == "email cannot be used to create an account"
    assert email not in detail


# --- self-service profile creation (POST /me/student) --------------------------


def _profileless_student(pg_engine) -> dict:
    """A real, active student account with NO students row — login-able.

    No production path mints this state; the fixture creates it directly so
    the self-service creation endpoint can be exercised end-to-end.
    """
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    email = f"profileless-{uuid.uuid4().hex[:8]}@example.com"
    session = SessionLocal(bind=pg_engine)
    try:
        session.add(
            User(
                email=email,
                role="student",
                status="active",
                password_hash=hash_password("correct horse battery staple"),
            )
        )
        session.commit()
    finally:
        session.close()
    return {"email": email, "password": "correct horse battery staple"}


def _login(api_client: TestClient, creds: dict) -> dict:
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_self_create_profile_happy_path(api_client, clean_tables) -> None:
    creds = _profileless_student(clean_tables)
    tokens = _login(api_client, creds)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}

    # Discovery signal: /me works with student: null; /me/student 403s and
    # points at the self-service remedy.
    me = api_client.get("/api/v1/me", headers=header)
    assert me.status_code == 200
    assert me.json()["student"] is None
    missing = api_client.get("/api/v1/me/student", headers=header)
    assert missing.status_code == 403
    assert "POST /api/v1/me/student" in missing.json()["detail"]

    # Create the profile.
    created = api_client.post(
        "/api/v1/me/student",
        json={
            "full_name": "Self Service Kid",
            "date_of_birth": "2012-04-10",
            "gender": "female",
            "country": "Rwanda",
        },
        headers=header,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["full_name"] == "Self Service Kid"
    assert body["email"] == creds["email"]  # identity untouched

    # The profile is now readable through every existing path.
    assert api_client.get("/api/v1/me/student", headers=header).status_code == 200
    assert api_client.get("/api/v1/me", headers=header).json()["student"] is not None

    # Audit: exactly one create/profile row attributed to the account itself.
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models.student_profile_history import StudentProfileHistory
    from app.models.user import User

    session = SessionLocal(bind=clean_tables)
    try:
        user = session.scalars(select(User).where(User.email == creds["email"])).one()
        rows = session.scalars(
            select(StudentProfileHistory)
            .where(StudentProfileHistory.student_id == uuid.UUID(body["student_id"]))
        ).all()
        assert len(rows) == 1
        assert rows[0].change_type == "create"
        assert rows[0].field_name == "profile"
        assert rows[0].changed_by == user.id
    finally:
        session.close()


def test_self_create_conflict_when_profile_exists(api_client, clean_tables) -> None:
    tokens = _register_student(api_client)  # register creates the profile too
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = api_client.post(
        "/api/v1/me/student",
        json={"full_name": "Second Profile", "date_of_birth": "2012-04-10"},
        headers=header,
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_self_create_requires_student_role(api_client, clean_tables) -> None:
    _admin_header(clean_tables)
    admin = _login(
        api_client,
        {"email": "platform-admin@test.example", "password": "admin test passphrase"},
    )
    header = {"Authorization": f"Bearer {admin['access_token']}"}
    response = api_client.post(
        "/api/v1/me/student",
        json={"full_name": "Admin Kid", "date_of_birth": "2012-04-10"},
        headers=header,
    )
    assert response.status_code == 403


def test_self_create_unauthenticated_refused(api_client, clean_tables) -> None:
    response = api_client.post(
        "/api/v1/me/student",
        json={"full_name": "No Auth", "date_of_birth": "2012-04-10"},
    )
    assert response.status_code == 401


def test_self_create_rejects_identity_anchors(api_client, clean_tables) -> None:
    creds = _profileless_student(clean_tables)
    tokens = _login(api_client, creds)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}

    with_email = api_client.post(
        "/api/v1/me/student",
        json={
            "email": "attacker@example.com",
            "full_name": "Anchor Try",
            "date_of_birth": "2012-04-10",
        },
        headers=header,
    )
    assert with_email.status_code == 422

    typo = api_client.post(
        "/api/v1/me/student",
        json={"fullNam": "Typo", "full_name": "Typo Kid", "date_of_birth": "2012-04-10"},
        headers=header,
    )
    assert typo.status_code == 422


def test_patch_rejects_identity_anchor_keys(api_client, clean_tables) -> None:
    """extra='forbid': anchors/typos in a PATCH body are 422, not silent no-ops."""
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = api_client.patch(
        "/api/v1/me/student",
        json={"email": "changed@example.com"},
        headers=header,
    )
    assert response.status_code == 422


def test_self_patch_writes_audit_row(api_client, clean_tables) -> None:
    creds = _profileless_student(clean_tables)
    tokens = _login(api_client, creds)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    created = api_client.post(
        "/api/v1/me/student",
        json={"full_name": "Audit Kid", "date_of_birth": "2012-04-10"},
        headers=header,
    )
    assert created.status_code == 201

    patched = api_client.patch(
        "/api/v1/me/student", json={"gender": "other"}, headers=header
    )
    assert patched.status_code == 200
    assert patched.json()["gender"] == "other"

    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models.student_profile_history import StudentProfileHistory
    from app.models.user import User

    session = SessionLocal(bind=clean_tables)
    try:
        user = session.scalars(select(User).where(User.email == creds["email"])).one()
        updates = session.scalars(
            select(StudentProfileHistory).where(
                StudentProfileHistory.change_type == "update"
            )
        ).all()
        assert len(updates) == 1
        assert updates[0].field_name == "gender"
        assert updates[0].old_value is None
        assert updates[0].new_value == "other"
        assert updates[0].changed_by == user.id  # self-attributed
    finally:
        session.close()


def test_db_gender_check_rejects_out_of_vocab_value(clean_tables) -> None:
    """The migration-0004 students_gender_check backstop, end-to-end.

    A raw INSERT bypassing the API schema must be refused by PostgreSQL —
    proving the constraint exists on the live test database.
    """
    import uuid as uuid_mod

    from sqlalchemy.exc import IntegrityError

    user_id = uuid_mod.uuid4()
    with clean_tables.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email, role, status) VALUES (:id, :email, 'student', 'active')"),
            {"id": str(user_id), "email": f"raw-{uuid_mod.uuid4().hex[:8]}@example.com"},
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO students (id, user_id, full_name, gender) "
                    "VALUES (:sid, :uid, 'Raw Kid', 'robot')"
                ),
                {"sid": str(uuid_mod.uuid4()), "uid": str(user_id)},
            )


def test_student_write_rate_limit_returns_429(api_client, clean_tables) -> None:
    """RATE_LIMIT_STUDENT_WRITE (10/minute) — 11th write in a minute is 429."""
    creds = _profileless_student(clean_tables)
    tokens = _login(api_client, creds)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}

    body = {"full_name": "Rate Kid", "date_of_birth": "2012-04-10"}
    statuses = []
    for _ in range(11):
        response = api_client.post("/api/v1/me/student", json=body, headers=header)
        statuses.append(response.status_code)
    # First request creates (201); the rest conflict (409) — but all consume
    # tokens, so the 11th is refused by the limiter before the handler runs.
    assert statuses[0] == 201
    assert 429 in statuses
    assert statuses[-1] == 429


# --- role-split guard matrix ------------------------------------------------------


def _admin_login(api_client: TestClient, pg_engine) -> dict:
    """Ensure the platform-admin account exists, then log it in."""
    _admin_header(pg_engine)
    login = api_client.post(
        "/api/v1/auth/login",
        json={"email": "platform-admin@test.example", "password": "admin test passphrase"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_admin_namespace_refuses_student_tokens(api_client, clean_tables) -> None:
    """Every /admin/students route answers 403 to a student token."""
    tokens = _register_student(api_client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    unknown = str(uuid.uuid4())

    cases = [
        ("post", "/api/v1/admin/students", {"email": "x@y.example", "full_name": "X", "date_of_birth": "2012-04-10"}),
        ("get", f"/api/v1/admin/students/{unknown}", None),
        ("patch", f"/api/v1/admin/students/{unknown}", {"country": "Kenya"}),
        ("get", f"/api/v1/admin/students/{unknown}/history", None),
    ]
    for method, path, body in cases:
        call = getattr(api_client, method)
        response = (
            call(path, json=body, headers=header)
            if body is not None
            else call(path, headers=header)
        )
        assert response.status_code == 403, f"{method.upper()} {path}"
        assert "administrator" in response.json()["detail"]


def test_self_service_namespace_refuses_admin_tokens(api_client, clean_tables) -> None:
    """Every /me/student route answers 403 to an administrator token."""
    admin_header = _admin_login(api_client, clean_tables)

    cases = [
        ("get", "/api/v1/me/student", None),
        ("post", "/api/v1/me/student", {"full_name": "Admin Kid", "date_of_birth": "2012-04-10"}),
        ("patch", "/api/v1/me/student", {"country": "Kenya"}),
    ]
    for method, path, body in cases:
        call = getattr(api_client, method)
        response = (
            call(path, json=body, headers=admin_header)
            if body is not None
            else call(path, headers=admin_header)
        )
        assert response.status_code == 403, f"{method.upper()} {path}"
