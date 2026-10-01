"""Teacher onboarding + self-service integration tests (PostgreSQL, guarded test DB).

End-to-end contract for the slice 5 flows against ``super_teacher_db_test``:

- an administrator creates a teacher, the invitation email's token is
  captured (the API never returns it), and ``POST /auth/accept-invite``
  activates the account with a password — a second use of the same token
  is refused with 401;
- the new teacher can log in, read their own profile and update it;
- the role matrix on ``/me/teacher``: anonymous → 401, student → 403,
  admin → 403, teacher → 200.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

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


def _truncate(engine) -> None:
    with engine.begin() as connection:
        for table in _ACCOUNT_TABLES:
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )


@pytest.fixture()
def accounts(pg_engine):
    with pg_engine.begin() as connection:
        name = connection.execute(text("SELECT current_database()")).scalar_one()
    assert name.endswith("_test"), f"SAFETY REFUSAL: connected to {name!r}"
    _truncate(pg_engine)
    yield pg_engine
    _truncate(pg_engine)


def _seed_user(engine, *, role: str, status: str = "active") -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{role}-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
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


@pytest.fixture()
def captured_invites(monkeypatch):
    """Capture the raw invitation tokens the service emails (never returned by the API)."""
    sent: list[str] = []
    monkeypatch.setattr(
        "app.core.email.send_teacher_invite_email",
        lambda *, to_email, invite_token, frontend_url: sent.append(invite_token),
        raising=False,
    )
    return sent


def _create_teacher(
    api_client: TestClient, admin_header: dict, email: str
) -> dict:
    response = api_client.post(
        "/api/v1/admin/teachers",
        json={"email": email, "full_name": "Onboarded Teacher", "subject": "Biology"},
        headers=admin_header,
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- end-to-end onboarding ----------------------------------------------------------------


def test_end_to_end_teacher_onboarding(
    accounts, captured_invites: list[str], api_client: TestClient
) -> None:
    admin = _seed_user(accounts, role="admin")
    admin_header = _header(_login(api_client, admin))
    email = f"onboard-{uuid.uuid4().hex[:8]}@test.example"

    created = _create_teacher(api_client, admin_header, email)
    assert created["status"] == "pending"
    assert len(captured_invites) == 1
    token = captured_invites[0]

    # 1. the pending account cannot authenticate yet.
    assert (
        api_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        ).status_code
        == 401
    )

    # 2. accepting the invitation sets the password, activates and logs in.
    accepted = api_client.post(
        "/api/v1/auth/accept-invite",
        json={"token": token, "new_password": PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    teacher_tokens = accepted.json()
    assert teacher_tokens["access_token"]
    teacher_header = _header(teacher_tokens)

    # 3. the same token cannot be replayed.
    replay = api_client.post(
        "/api/v1/auth/accept-invite",
        json={"token": token, "new_password": PASSWORD},
    )
    assert replay.status_code == 401, replay.text

    # 4. login with the new password works.
    login = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text

    # 5. self-service profile read + update.
    read = api_client.get("/api/v1/me/teacher", headers=teacher_header)
    assert read.status_code == 200, read.text
    assert read.json()["email"] == email
    assert read.json()["status"] == "active"
    assert read.json()["full_name"] == "Onboarded Teacher"

    patched = api_client.patch(
        "/api/v1/me/teacher",
        json={"subject": "Chemistry", "phone": "+250782222222"},
        headers=teacher_header,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["subject"] == "Chemistry"

    # Unknown keys are refused — the boundary cannot be widened.
    assert (
        api_client.patch(
            "/api/v1/me/teacher",
            json={"school_id": str(uuid.uuid4())},
            headers=teacher_header,
        ).status_code
        == 422
    )


def test_accept_invite_rejects_an_unknown_token(accounts, api_client: TestClient) -> None:
    response = api_client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": f"not-a-real-token-{uuid.uuid4().hex}",
            "new_password": PASSWORD,
        },
    )
    assert response.status_code == 401, response.text


# --- role matrix on /me/teacher ------------------------------------------------------------


@pytest.fixture()
def teacher_session(accounts, captured_invites: list[str], api_client: TestClient) -> dict:
    """An accepted, active teacher plus the admin header used to create them."""
    admin = _seed_user(accounts, role="admin")
    admin_header = _header(_login(api_client, admin))
    email = f"matrix-{uuid.uuid4().hex[:8]}@test.example"
    created = _create_teacher(api_client, admin_header, email)
    accepted = api_client.post(
        "/api/v1/auth/accept-invite",
        json={"token": captured_invites[-1], "new_password": PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    return {
        "email": email,
        "password": PASSWORD,
        "teacher_header": _header(accepted.json()),
        "admin_header": admin_header,
        "user_id": created["user_id"],
    }


def test_me_teacher_refuses_anonymous(accounts, api_client: TestClient) -> None:
    assert api_client.get("/api/v1/me/teacher").status_code == 401
    assert (
        api_client.patch("/api/v1/me/teacher", json={"phone": "+250780000000"}).status_code
        == 401
    )


def test_me_teacher_refuses_students(
    accounts, teacher_session: dict, api_client: TestClient
) -> None:
    student = _seed_user(accounts, role="student")
    header = _header(_login(api_client, student))
    assert api_client.get("/api/v1/me/teacher", headers=header).status_code == 403
    assert (
        api_client.patch(
            "/api/v1/me/teacher", json={"phone": "+250780000000"}, headers=header
        ).status_code
        == 403
    )


def test_me_teacher_refuses_administrators(
    accounts, teacher_session: dict, api_client: TestClient
) -> None:
    assert (
        api_client.get(
            "/api/v1/me/teacher", headers=teacher_session["admin_header"]
        ).status_code
        == 403
    )


def test_me_teacher_serves_the_teacher(
    accounts, teacher_session: dict, api_client: TestClient
) -> None:
    header = teacher_session["teacher_header"]
    read = api_client.get("/api/v1/me/teacher", headers=header)
    assert read.status_code == 200, read.text
    assert read.json()["user_id"] == teacher_session["user_id"]

    patched = api_client.patch(
        "/api/v1/me/teacher", json={"subject": "Physics"}, headers=header
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["subject"] == "Physics"


def test_me_teacher_update_persists_across_sessions(
    accounts, teacher_session: dict, api_client: TestClient
) -> None:
    """PATCH /me/teacher must COMMIT: a fresh session sees the new row.

    The endpoint commits after the service flush, so the change outlives the
    request-scoped session. Reading the row back through a separate
    ``SessionLocal`` (its own pooled connection, outside the API's
    transaction) proves the commit really happened — a handler that returns
    without committing would still answer 200 (the response is built from the
    in-session profile) while the database silently keeps the old value.
    """
    from app.core.database import SessionLocal
    from app.models.teacher import Teacher

    header = teacher_session["teacher_header"]
    patched = api_client.patch(
        "/api/v1/me/teacher", json={"subject": "Chemistry"}, headers=header
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["subject"] == "Chemistry"

    fresh = SessionLocal(bind=accounts)
    try:
        profile = fresh.query(Teacher).filter(
            Teacher.user_id == uuid.UUID(teacher_session["user_id"])
        ).one()
        assert profile.subject == "Chemistry", (
            "PATCH /me/teacher returned 200 but never committed: "
            f"fresh session still sees subject={profile.subject!r}"
        )
    finally:
        fresh.close()
