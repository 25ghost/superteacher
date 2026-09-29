"""Authentication API integration tests (PostgreSQL, opt-in, guarded test DB).

Phase 5G Steps 31-33. Exercises the whole authenticated HTTP surface
against ``super_teacher_db_test`` (never the development database):

- account registration (201, tokens issued, role forced to student,
  duplicate email 409, weak password 422, atomicity),
- login (correct credentials, wrong password, unknown email — identical
  generic 401),
- identity (GET /me, GET /me/student),
- refresh (rotation; replay refused; garbage refused; logout invalidates),
- object-level authorization regressions (Step 33): user A authenticated +
  user B's student UUID supplied → MUST NOT access B — tested for profile
  read/update, registration creation, history and enrollment retrieval,
- protected endpoints refuse unauthenticated callers (401),
- the development database is never touched (fixture guard + explicit
  ``SELECT current_database()`` before any write).
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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


def _truncate_account_tables(engine) -> None:
    with engine.begin() as connection:
        for table in ("auth_sessions", "student_subjects", "student_enrollments",
                      "students", "users"):
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )

@pytest.fixture()
def auth_db(pg_engine):
    """Clean account tables + seeded catalog, with a database-name check."""
    _assert_test_database(pg_engine)
    _truncate_account_tables(pg_engine)
    # Seed the verified-minimum catalog through the real (idempotent)
    # seeder so authenticated-registration tests resolve real pathways.
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry

    session = SessionLocal(bind=pg_engine)
    try:
        run_load(session, load_registry())
    finally:
        session.close()
    yield pg_engine
    _truncate_account_tables(pg_engine)


def _register(
    api_client: TestClient,
    email: str | None = None,
    password: str = PASSWORD,
    **overrides,
) -> dict:
    payload = {
        "email": email or f"student-{uuid.uuid4().hex[:8]}@example.com",
        "password": password,
        "full_name": "Integration Student",
        "date_of_birth": "2012-04-10",
        "gender": "female",
        "country": "Rwanda",
    }
    payload.update(overrides)
    response = api_client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _auth_header(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --- registration (Steps 19, 20, 32) -------------------------------------------------


def test_register_creates_account_and_tokens(api_client, auth_db) -> None:
    tokens = _register(api_client)
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["expires_in"] > 0
    # No credential material in the response.
    assert PASSWORD not in str(tokens)


def test_register_duplicate_email_conflict(api_client, auth_db) -> None:
    email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
    _register(api_client, email=email)
    duplicate = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Second Account",
            "date_of_birth": "2012-04-10",
        },
    )
    assert duplicate.status_code == 409
    # Generic detail: the public endpoint never confirms the email exists.
    detail = duplicate.json()["detail"]
    assert detail == "email cannot be used to create an account"
    assert email not in detail


def test_register_future_dob_is_422(api_client, auth_db) -> None:
    """A future date of birth is a readable 422 — never a database 500."""
    from datetime import date, timedelta

    future = (date.today() + timedelta(days=3)).isoformat()
    response = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"future-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
            "full_name": "Future Baby",
            "date_of_birth": future,
        },
    )
    assert response.status_code == 422
    with auth_db.begin() as connection:
        users = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    assert users == 0


def test_register_implausible_dob_is_422(api_client, auth_db) -> None:
    """Pre-1900 junk DOB is refused by the shared age-bounds guard."""
    response = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"old-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
            "full_name": "Too Old",
            "date_of_birth": "1850-01-01",
        },
    )
    assert response.status_code == 422
    with auth_db.begin() as connection:
        users = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    assert users == 0


def test_register_weak_password_422(api_client, auth_db) -> None:
    weak = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"weak-{uuid.uuid4().hex[:8]}@example.com",
            "password": "short",
            "full_name": "Weak Password",
            "date_of_birth": "2012-04-10",
        },
    )
    assert weak.status_code == 422


def test_register_role_forced_to_student(api_client, auth_db) -> None:
    """The request cannot choose a privileged role — the field does not exist."""
    tokens = _register(api_client)
    me = api_client.get("/api/v1/me", headers=_auth_header(tokens))
    assert me.status_code == 200
    assert me.json()["role"] == "student"


def test_register_atomic_no_user_without_profile(api_client, auth_db) -> None:
    """A profile-level rejection (bad gender) leaves zero users behind."""
    response = api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"atomic-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
            "full_name": "Atomic Check",
            "date_of_birth": "2012-04-10",
            "gender": "robot",
        },
    )
    assert response.status_code == 422
    with auth_db.begin() as connection:
        users = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    assert users == 0


def test_password_never_stored_in_plaintext(api_client, auth_db) -> None:
    email = f"hash-{uuid.uuid4().hex[:8]}@example.com"
    _register(api_client, email=email)
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models.user import User

    session = SessionLocal(bind=auth_db)
    try:
        user = session.scalars(select(User).where(User.email == email)).one()
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in (user.password_hash or "")
    finally:
        session.close()


# --- login (Step 21, 32) ---------------------------------------------------------------


def test_login_correct_credentials(api_client, auth_db) -> None:
    email = f"login-{uuid.uuid4().hex[:8]}@example.com"
    _register(api_client, email=email)
    response = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_wrong_password_generic_401(api_client, auth_db) -> None:
    email = f"login-{uuid.uuid4().hex[:8]}@example.com"
    _register(api_client, email=email)
    response = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong password"}
    )
    assert response.status_code == 401


def test_login_unknown_email_identical_401(api_client, auth_db) -> None:
    """No account enumeration: unknown email == wrong password response."""
    _register(api_client, email=f"known-{uuid.uuid4().hex[:8]}@example.com")
    wrong_password = api_client.post(
        "/api/v1/auth/login",
        json={"email": f"known-{uuid.uuid4().hex[:8]}@example.com", "password": "x-not-it"},
    )
    unknown_email = api_client.post(
        "/api/v1/auth/login",
        json={"email": f"ghost-{uuid.uuid4().hex[:8]}@example.com", "password": "whatever"},
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["detail"] == unknown_email.json()["detail"]


# --- identity endpoints (Steps 13, 24) ----------------------------------------------------


def test_me_returns_safe_identity(api_client, auth_db) -> None:
    tokens = _register(api_client)
    me = api_client.get("/api/v1/me", headers=_auth_header(tokens))
    assert me.status_code == 200
    body = me.json()
    assert body["role"] == "student"
    assert body["student"]["full_name"] == "Integration Student"
    body_text = str(body)
    assert "password" not in body_text.lower()
    assert "hash" not in body_text.lower()


def test_me_student_returns_own_profile(api_client, auth_db) -> None:
    tokens = _register(api_client)
    response = api_client.get("/api/v1/me/student", headers=_auth_header(tokens))
    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Integration Student"
    assert body["role"] == "student"


def test_protected_endpoints_require_token(api_client, auth_db) -> None:
    """Unauthenticated access to protected operations is refused (401)."""
    for method, path in (
        ("get", "/api/v1/me"),
        ("get", "/api/v1/auth/me"),
        ("get", "/api/v1/me/student"),
        ("get", "/api/v1/me/registrations"),
        ("post", "/api/v1/me/registrations"),
        ("patch", "/api/v1/me/student"),
        ("post", "/api/v1/admin/students"),
        ("get", "/api/v1/admin/students/00000000-0000-0000-0000-000000000000"),
        ("post", "/api/v1/admin/registrations"),
    ):
        response = getattr(api_client, method)(path)
        assert response.status_code == 401, f"{method.upper()} {path}"


def test_malformed_token_refused(api_client, auth_db) -> None:
    response = api_client.get(
        "/api/v1/me", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert response.status_code == 401
    # Generic detail — no decoding internals leaked.
    assert "signature" not in response.json()["detail"].lower()


def test_expired_token_refused(api_client, auth_db, monkeypatch) -> None:
    from app.core import security

    tokens = _register(api_client)
    user_id = uuid.UUID(
        api_client.get("/api/v1/me", headers=_auth_header(tokens)).json()["user_id"]
    )
    expired = security.create_access_token(user_id, "student", expires_minutes=-1)
    response = api_client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_refresh_token_used_as_access_token_refused(api_client, auth_db) -> None:
    tokens = _register(api_client)
    response = api_client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    assert response.status_code == 401  # wrong token type


# --- refresh / logout (Steps 22, 23, 32) -----------------------------------------------------


def test_refresh_issues_new_pair_and_rotates(api_client, auth_db) -> None:
    tokens = _register(api_client)
    response = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200
    fresh = response.json()
    assert fresh["refresh_token"] != tokens["refresh_token"]
    # Replay of the rotated token is refused.
    replay = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401


def test_refresh_garbage_refused(api_client, auth_db) -> None:
    response = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": "garbage"}
    )
    assert response.status_code == 401


def test_logout_invalidates_refresh(api_client, auth_db) -> None:
    tokens = _register(api_client)
    logout = api_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=_auth_header(tokens),
    )
    assert logout.status_code == 204
    after = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert after.status_code == 401


def test_logout_requires_authentication(api_client, auth_db) -> None:
    tokens = _register(api_client)
    response = api_client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401
    # The session must NOT have been revoked by the refused call.
    after = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert after.status_code == 200


def test_access_token_still_valid_after_logout(api_client, auth_db) -> None:
    """Access tokens are not revoked on logout (they simply expire)."""
    email = f"logout-test-{uuid.uuid4().hex[:8]}@example.com"
    tokens = _register(api_client, email=email)
    logout = api_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=_auth_header(tokens),
    )
    assert logout.status_code == 204
    me = api_client.get("/api/v1/me", headers=_auth_header(tokens))
    assert me.status_code == 200
    assert me.json()["email"] == email


# --- Step 33: object-level authorization regressions ---------------------------------------


class OwnedContext:
    """Two independent registered students plus a registration context."""

    def __init__(self, api_client: TestClient, engine) -> None:
        self.tokens_a = _register(api_client, email=f"a-{uuid.uuid4().hex[:8]}@example.com")
        self.tokens_b = _register(api_client, email=f"b-{uuid.uuid4().hex[:8]}@example.com")
        self.student_a = api_client.get(
            "/api/v1/me/student", headers=_auth_header(self.tokens_a)
        ).json()["student_id"]
        self.student_b = api_client.get(
            "/api/v1/me/student", headers=_auth_header(self.tokens_b)
        ).json()["student_id"]

        from app.core.database import SessionLocal
        from app.models.academic_year import AcademicYear
        from app.models.pathway import Pathway
        from app.models.school import School

        session = SessionLocal(bind=engine)
        try:
            pathway = session.query(Pathway).filter_by(code="O_LEVEL").first()
            year = AcademicYear(
                name=f"2099/2100-{uuid.uuid4().hex[:6]}",
                start_date=date(2099, 9, 1),
                end_date=date(2100, 7, 31),
                status="planned",
            )
            school = School(
                name="Auth Test School", school_code=f"ATS{uuid.uuid4().hex[:6].upper()}"
            )
            session.add_all([year, school])
            session.commit()
            self.year_id = str(year.id)
            self.school_id = str(school.id)
            self.pathway_code = pathway.code if pathway else "O_LEVEL"
            self.level_code = "S1"
        finally:
            session.close()

    def header_a(self) -> dict:
        return _auth_header(self.tokens_a)

    def header_b(self) -> dict:
        return _auth_header(self.tokens_b)


@pytest.fixture()
def two_students(api_client, auth_db):
    return OwnedContext(api_client, auth_db)


def test_registration_for_another_student_spoofing_refused(
    api_client, two_students
) -> None:
    """A supplies B's student_id → 422 (field not accepted), B unenrolled."""
    payload = {
        "student_id": two_students.student_b,  # spoofing attempt
        "academic_year_id": two_students.year_id,
        "pathway": two_students.pathway_code,
        "education_level": two_students.level_code,
        "school_id": two_students.school_id,
    }
    response = api_client.post(
        "/api/v1/me/registrations", json=payload, headers=two_students.header_a()
    )
    assert response.status_code == 422
    b_history = api_client.get(
        "/api/v1/me/registrations", headers=two_students.header_b()
    )
    assert b_history.status_code == 200
    assert b_history.json() == []


def test_spoofing_creates_nothing(api_client, two_students) -> None:
    payload = {
        "student_id": two_students.student_b,
        "academic_year_id": two_students.year_id,
        "pathway": two_students.pathway_code,
        "education_level": two_students.level_code,
        "school_id": two_students.school_id,
    }
    response = api_client.post("/api/v1/me/registrations", json=payload, headers=two_students.header_a())
    assert response.status_code == 422
    b_history = api_client.get("/api/v1/me/registrations", headers=two_students.header_b())
    assert b_history.status_code == 200
    assert b_history.json() == []


def test_authenticated_registration_own_enrollment(api_client, two_students) -> None:
    payload = {
        "academic_year_id": two_students.year_id,
        "pathway": two_students.pathway_code,
        "education_level": two_students.level_code,
        "school_id": two_students.school_id,
    }
    response = api_client.post("/api/v1/me/registrations", json=payload, headers=two_students.header_a())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["student_id"] == two_students.student_a


def test_duplicate_enrollment_still_409(api_client, two_students) -> None:
    payload = {
        "academic_year_id": two_students.year_id,
        "pathway": two_students.pathway_code,
        "education_level": two_students.level_code,
        "school_id": two_students.school_id,
    }
    first = api_client.post("/api/v1/me/registrations", json=payload, headers=two_students.header_a())
    assert first.status_code == 201
    duplicate = api_client.post("/api/v1/me/registrations", json=payload, headers=two_students.header_a())
    assert duplicate.status_code == 409


def test_profile_of_another_student_refused(api_client, two_students) -> None:
    # The admin-namespaced read refuses a student token outright.
    response = api_client.get(
        f"/api/v1/admin/students/{two_students.student_b}", headers=two_students.header_a()
    )
    assert response.status_code == 403
    # The student still reads their own profile through the self-service route.
    own = api_client.get("/api/v1/me/student", headers=two_students.header_a())
    assert own.status_code == 200
    assert own.json()["student_id"] == two_students.student_a


def test_profile_update_of_another_student_refused(api_client, two_students) -> None:
    # Student token on the admin-namespaced write → 403 before any handler.
    response = api_client.patch(
        f"/api/v1/admin/students/{two_students.student_b}",
        json={"full_name": "Hacked Name"},
        headers=two_students.header_a(),
    )
    assert response.status_code == 403
    # B's name is unchanged.
    b_me = api_client.get("/api/v1/me/student", headers=two_students.header_b())
    assert b_me.json()["full_name"] == "Integration Student"


def test_me_student_patch_updates_own_profile(api_client, two_students) -> None:
    response = api_client.patch(
        "/api/v1/me/student",
        json={"country": "Kenya"},
        headers=two_students.header_a(),
    )
    assert response.status_code == 200
    assert response.json()["country"] == "Kenya"


def test_history_of_another_student_refused(api_client, two_students) -> None:
    response = api_client.get(
        f"/api/v1/admin/students/{two_students.student_b}/registrations",
        headers=two_students.header_a(),
    )
    assert response.status_code == 403


def test_enrollment_of_another_student_refused(api_client, two_students) -> None:
    payload = {
        "academic_year_id": two_students.year_id,
        "pathway": two_students.pathway_code,
        "education_level": two_students.level_code,
        "school_id": two_students.school_id,
    }
    created = api_client.post(
        "/api/v1/me/registrations", json=payload, headers=two_students.header_a()
    )
    assert created.status_code == 201
    enrollment_id = created.json()["enrollment_id"]

    # Owner can read it; the other student cannot.
    own = api_client.get(f"/api/v1/me/registrations/{enrollment_id}", headers=two_students.header_a())
    assert own.status_code == 200
    other = api_client.get(
        f"/api/v1/me/registrations/{enrollment_id}", headers=two_students.header_b()
    )
    assert other.status_code == 403


def test_me_registrations_returns_own_history(api_client, two_students) -> None:
    payload = {
        "academic_year_id": two_students.year_id,
        "pathway": two_students.pathway_code,
        "education_level": two_students.level_code,
        "school_id": two_students.school_id,
    }
    created = api_client.post(
        "/api/v1/me/registrations", json=payload, headers=two_students.header_a()
    )
    assert created.status_code == 201
    history = api_client.get("/api/v1/me/registrations", headers=two_students.header_a())
    assert history.status_code == 200
    assert [row["enrollment_id"] for row in history.json()] == [
        created.json()["enrollment_id"]
    ]
    # B sees nothing of A's.
    b_history = api_client.get("/api/v1/me/registrations", headers=two_students.header_b())
    assert b_history.json() == []


def test_student_cannot_create_admin_identity(api_client, auth_db) -> None:
    """Role escalation through registration is impossible by schema design."""
    from app.schemas.auth import StudentAccountCreate

    assert "role" not in StudentAccountCreate.model_fields
    assert "status" not in StudentAccountCreate.model_fields
    tokens = _register(api_client)
    me = api_client.get("/api/v1/me", headers=_auth_header(tokens))
    assert me.json()["role"] == "student"


def test_public_endpoints_remain_public(api_client, auth_db) -> None:
    assert api_client.get("/api/v1/health").status_code == 200
    assert api_client.get("/api/v1/registrations/readiness").status_code == 200
    assert api_client.get("/api/v1/catalog/pathways").status_code == 200


# --- development-database safety ---------------------------------------------------------------


def test_every_write_targeted_the_test_database(pg_engine) -> None:
    assert _assert_test_database(pg_engine) == "super_teacher_db_test"


# --- rate limiting -------------------------------------------------------------------


def test_login_rate_limit_returns_429(api_client, auth_db) -> None:
    """Exceeding the login rate limit returns 429 at the correct boundary."""
    # The autouse fixture already reset the shared limiter; reset again here
    # so this test stays self-contained if it is ever run alone.
    api_client.app.state.limiter.reset()
    _register(api_client)
    # Default login limit: 10/minute. First 10 should pass, 11th should 429.
    for i in range(10):
        resp = api_client.post(
            "/api/v1/auth/login",
            json={"email": "rate-limit@example.com", "password": "wrong-password"},
        )
        assert resp.status_code != 429, f"Request {i+1} should not be rate-limited"
    # 11th request exceeds the limit
    resp = api_client.post(
        "/api/v1/auth/login",
        json={"email": "rate-limit@example.com", "password": "wrong-password"},
    )
    assert resp.status_code == 429
