"""Auth hardening integration tests (H3) — PostgreSQL, guarded test DB.

Covers the security MVP's remaining behavioural contracts against
``super_teacher_db_test`` (never the development database), with the Resend
boundary mocked at ``app.core.email``:

- forgot-password: 204 for a known *and* an unknown address, the reset email
  is sent exactly once and only after the token row is committed, and the raw
  token never reaches the logs,
- reset-password: single-use token, expiry, token-type confusion, password
  policy (the token survives a policy failure), every refresh session
  revoked and the old password dead,
- change-password: wrong current password, policy, and the behaviour of
  already-issued sessions (they stay valid — that is what the service does),
- deactivate: every authenticated surface answers 401, refresh sessions are
  revoked, login is refused,
- audit rows: the seven API-driven ``auth_events`` types carry the caller's
  ip address and user-agent,
- create_admin against real PostgreSQL (create, duplicate, production guard,
  password never on argv),
- the 429 contract: ``{"detail": ...}`` plus a ``Retry-After`` header,
- ``verify_database.py``: structural checks (model == migrations) and live
  checks (model == PostgreSQL) both pass.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a completely different passphrase"
UA = "rahura-hardening/1.0"

#: Children first, so TRUNCATE ... CASCADE is deterministic.
_ACCOUNT_TABLES = (
    "auth_sessions",
    "password_reset_tokens",
    "auth_events",
    "invite_tokens",
    "student_subjects",
    "student_enrollments",
    "students",
    "teachers",
    "users",
)


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


def _truncate(engine) -> None:
    with engine.begin() as connection:
        name = connection.execute(text("SELECT current_database()")).scalar_one()
        assert name.endswith("_test"), f"SAFETY REFUSAL: connected to {name!r}"
        for table in _ACCOUNT_TABLES:
            connection.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )


@pytest.fixture()
def auth_db(pg_engine):
    """Empty account tables + the seeded minimum catalog."""
    _truncate(pg_engine)
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry

    session = SessionLocal(bind=pg_engine)
    try:
        run_load(session, load_registry())
    finally:
        session.close()
    yield pg_engine
    _truncate(pg_engine)


def _register(
    api_client: TestClient,
    email: str | None = None,
    password: str = PASSWORD,
    **overrides,
) -> dict:
    payload = {
        "email": email or f"student-{uuid.uuid4().hex[:8]}@example.com",
        "password": password,
        "full_name": "Hardening Student",
        "date_of_birth": "2012-04-10",
        "gender": "female",
        "country": "Rwanda",
    }
    payload.update(overrides)
    response = api_client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _login(api_client: TestClient, email: str, password: str = PASSWORD, **kwargs) -> dict:
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        **kwargs,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _header(tokens: dict, user_agent: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    if user_agent is not None:
        headers["user-agent"] = user_agent
    return headers


def _seed_user(engine, *, role: str) -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{role}-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        account = User(
            email=email,
            role=role,
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(account)
        session.commit()
        user_id = str(account.id)
    finally:
        session.close()
    return {"email": email, "user_id": user_id}


def _events_for(engine, user_id: str) -> list:
    """Every audit row for one user, oldest first."""
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT event_type, ip_address, user_agent FROM auth_events "
                "WHERE user_id = :u ORDER BY id"
            ),
            {"u": user_id},
        ).all()


@pytest.fixture()
def reset_email(monkeypatch, auth_db) -> SimpleNamespace:
    """Capture reset tokens and prove the token row is committed at send time.

    The send runs from a background task; a *separate* connection can only see
    the row once the endpoint has committed it (READ COMMITTED), so
    ``committed`` is the L14 proof.
    """
    state = SimpleNamespace(sent=[], committed=[])

    def _send(*, to_email: str, reset_token: str, frontend_url: str) -> None:
        token_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
        with auth_db.connect() as connection:
            rows = connection.execute(
                text("SELECT count(*) FROM password_reset_tokens WHERE token_hash = :h"),
                {"h": token_hash},
            ).scalar_one()
        state.committed.append(rows == 1)
        state.sent.append(reset_token)

    monkeypatch.setattr("app.core.email.send_password_reset_email", _send)
    return state


@pytest.fixture()
def captured_invites(monkeypatch) -> list:
    """Capture the raw invitation tokens the service emails."""
    sent: list[str] = []
    monkeypatch.setattr(
        "app.core.email.send_teacher_invite_email",
        lambda *, to_email, invite_token, frontend_url: sent.append(invite_token),
        raising=False,
    )
    return sent


def _request_reset_token(api_client: TestClient, email: str, reset_email) -> str:
    response = api_client.post("/api/v1/auth/forgot-password", json={"email": email})
    assert response.status_code == 204, response.text
    assert len(reset_email.sent) == 1, reset_email.sent
    return reset_email.sent[0]


# --- forgot password ----------------------------------------------------------------


def test_forgot_password_known_email_sends_once_after_commit(
    api_client, auth_db, reset_email, caplog
) -> None:
    email = f"forgot-{uuid.uuid4().hex[:8]}@example.com"
    _register(api_client, email=email)

    with caplog.at_level(logging.INFO):
        response = api_client.post("/api/v1/auth/forgot-password", json={"email": email})

    assert response.status_code == 204
    assert response.content == b""
    assert len(reset_email.sent) == 1, "exactly one send for the known account"
    assert reset_email.committed == [True], "token row must be committed before the send"
    # The raw token never appears in the response or in the logs.
    token = reset_email.sent[0]
    assert token not in response.text
    assert token not in caplog.text


def test_forgot_password_unknown_email_is_204_and_sends_nothing(
    api_client, auth_db, reset_email
) -> None:
    response = api_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.com"},
    )
    assert response.status_code == 204, response.text
    assert reset_email.sent == []
    assert reset_email.committed == []


# --- reset password -----------------------------------------------------------------


def test_reset_password_works_once_then_the_token_is_dead(
    api_client, auth_db, reset_email
) -> None:
    email = f"reset-{uuid.uuid4().hex[:8]}@example.com"
    registration = _register(api_client, email=email)
    token = _request_reset_token(api_client, email, reset_email)

    used = api_client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert used.status_code == 204, used.text

    # The new password works, the old one is dead.
    assert _login(api_client, email, password=NEW_PASSWORD)
    refused = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert refused.status_code == 401, refused.text

    # Single use: the very same token cannot be replayed.
    replay = api_client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "another passphrase 123"},
    )
    assert replay.status_code == 401, replay.text

    # The registration-era refresh session was revoked by the reset.
    refreshed = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": registration["refresh_token"]}
    )
    assert refreshed.status_code == 401, refreshed.text


def test_reset_password_revokes_every_refresh_session(
    api_client, auth_db, reset_email
) -> None:
    email = f"revoke-{uuid.uuid4().hex[:8]}@example.com"
    first = _register(api_client, email=email)
    second = _login(api_client, email)
    with auth_db.begin() as connection:
        active = connection.execute(
            text(
                "SELECT count(*) FROM auth_sessions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE u.email = :e AND s.revoked_at IS NULL"
            ),
            {"e": email},
        ).scalar_one()
    assert active >= 2

    token = _request_reset_token(api_client, email, reset_email)
    assert (
        api_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": NEW_PASSWORD},
        ).status_code
        == 204
    )

    with auth_db.begin() as connection:
        revoked = connection.execute(
            text(
                "SELECT count(*) FROM auth_sessions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE u.email = :e AND s.revoked_at IS NULL"
            ),
            {"e": email},
        ).scalar_one()
    assert revoked == 0, "a reset must revoke every refresh session"

    for stale in (first["refresh_token"], second["refresh_token"]):
        assert (
            api_client.post("/api/v1/auth/refresh", json={"refresh_token": stale}).status_code
            == 401
        )


def test_reset_password_expired_token_fails(api_client, auth_db, reset_email) -> None:
    email = f"expired-{uuid.uuid4().hex[:8]}@example.com"
    _register(api_client, email=email)
    token = _request_reset_token(api_client, email, reset_email)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    with auth_db.begin() as connection:
        connection.execute(
            text(
                "UPDATE password_reset_tokens SET expires_at = now() - interval '1 hour' "
                "WHERE token_hash = :h"
            ),
            {"h": token_hash},
        )

    response = api_client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 401, response.text

    with auth_db.begin() as connection:
        used = connection.execute(
            text(
                "SELECT used_at FROM password_reset_tokens WHERE token_hash = :h"
            ),
            {"h": token_hash},
        ).scalar_one()
    assert used is None, "an expired token must not be consumed"


def test_reset_password_refuses_a_token_of_the_wrong_type(
    api_client, auth_db, reset_email
) -> None:
    """An access token is not a reset token — type confusion is refused."""
    email = f"wrongtyp-{uuid.uuid4().hex[:8]}@example.com"
    registration = _register(api_client, email=email)

    response = api_client.post(
        "/api/v1/auth/reset-password",
        json={"token": registration["access_token"], "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 401, response.text


def test_reset_password_enforces_the_policy_without_consuming_the_token(
    api_client, auth_db, reset_email
) -> None:
    email = f"policy-{uuid.uuid4().hex[:8]}@example.com"
    _register(api_client, email=email)
    token = _request_reset_token(api_client, email, reset_email)

    weak = api_client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "short"}
    )
    assert weak.status_code == 422, weak.text

    # The refusal happened before the token was consumed, so a good password
    # still works with the very same token.
    assert (
        api_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": NEW_PASSWORD},
        ).status_code
        == 204
    )


# --- change password ----------------------------------------------------------------


def test_change_password_rejects_wrong_current_password_and_enforces_policy(
    api_client, auth_db
) -> None:
    email = f"change-{uuid.uuid4().hex[:8]}@example.com"
    tokens = _register(api_client, email=email)

    wrong = api_client.post(
        "/api/v1/me/change-password",
        json={"current_password": "not the real one", "new_password": NEW_PASSWORD},
        headers=_header(tokens),
    )
    assert wrong.status_code == 401, wrong.text

    weak = api_client.post(
        "/api/v1/me/change-password",
        json={"current_password": PASSWORD, "new_password": "short"},
        headers=_header(tokens),
    )
    assert weak.status_code == 422, weak.text

    changed = api_client.post(
        "/api/v1/me/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=_header(tokens),
    )
    assert changed.status_code == 204, changed.text

    assert _login(api_client, email, password=NEW_PASSWORD)
    assert (
        api_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        ).status_code
        == 401
    )


def test_change_password_leaves_existing_sessions_valid(
    api_client, auth_db
) -> None:
    """What the service actually does: change-password does NOT revoke."""
    email = f"sessions-{uuid.uuid4().hex[:8]}@example.com"
    tokens = _register(api_client, email=email)

    changed = api_client.post(
        "/api/v1/me/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=_header(tokens),
    )
    assert changed.status_code == 204, changed.text

    # The already-issued access token still identifies the caller...
    me = api_client.get("/api/v1/me", headers=_header(tokens))
    assert me.status_code == 200, me.text
    # ...and the pre-change refresh session still rotates.
    rotated = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert rotated.status_code == 200, rotated.text


# --- deactivation -------------------------------------------------------------------


def test_deactivate_blocks_identity_refresh_and_login(api_client, auth_db) -> None:
    email = f"deactivate-{uuid.uuid4().hex[:8]}@example.com"
    tokens = _register(api_client, email=email)

    wrong = api_client.post(
        "/api/v1/me/deactivate",
        json={"password": "not the real one"},
        headers=_header(tokens),
    )
    assert wrong.status_code == 401, wrong.text

    done = api_client.post(
        "/api/v1/me/deactivate", json={"password": PASSWORD}, headers=_header(tokens)
    )
    assert done.status_code == 204, done.text

    # Every authenticated surface now answers 401.
    assert api_client.get("/api/v1/me", headers=_header(tokens)).status_code == 401
    assert (
        api_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        api_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        ).status_code
        == 401
    )

    with auth_db.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM users WHERE email = :e"), {"e": email}
        ).scalar_one()
        active = connection.execute(
            text(
                "SELECT count(*) FROM auth_sessions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE u.email = :e AND s.revoked_at IS NULL"
            ),
            {"e": email},
        ).scalar_one()
    assert status == "disabled"
    assert active == 0


# --- audit trail --------------------------------------------------------------------


def test_audit_rows_record_ip_and_user_agent_for_the_account_lifecycle(
    api_client, auth_db, reset_email
) -> None:
    """login, token_refresh, logout, password_change, password_reset,
    account_deactivated — six of the seven API-driven event types, each with
    the caller's ip and user-agent."""
    email = f"audit-{uuid.uuid4().hex[:8]}@example.com"
    _register(api_client, email=email)
    with auth_db.begin() as connection:
        user_id = connection.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": email}
        ).scalar_one()

    # 1. login
    tokens = _login(api_client, email, headers={"user-agent": UA})
    # 2. token_refresh
    rotated = api_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"user-agent": UA},
    )
    assert rotated.status_code == 200, rotated.text
    tokens = rotated.json()
    # 3. password_change
    assert (
        api_client.post(
            "/api/v1/me/change-password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=_header(tokens, UA),
        ).status_code
        == 204
    )
    # 4. logout
    assert (
        api_client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers=_header(tokens, UA),
        ).status_code
        == 204
    )
    # 5. login again (the new password), then 6. password_reset
    tokens = _login(api_client, email, password=NEW_PASSWORD, headers={"user-agent": UA})
    reset_token = _request_reset_token(api_client, email, reset_email)
    assert (
        api_client.post(
            "/api/v1/auth/reset-password",
            json={"token": reset_token, "new_password": PASSWORD},
            headers={"user-agent": UA},
        ).status_code
        == 204
    )
    # 7. login once more (the reset revoked every session), then deactivate
    tokens = _login(api_client, email, headers={"user-agent": UA})
    assert (
        api_client.post(
            "/api/v1/me/deactivate",
            json={"password": PASSWORD},
            headers=_header(tokens, UA),
        ).status_code
        == 204
    )

    rows = _events_for(auth_db, user_id)
    seen = [row.event_type for row in rows]
    for expected in (
        "login",
        "token_refresh",
        "logout",
        "password_change",
        "password_reset",
        "account_deactivated",
    ):
        assert expected in seen, f"missing audit event {expected!r} (saw {seen})"
    assert rows, "the lifecycle must leave audit rows behind"
    for row in rows:
        assert row.ip_address == "testclient", (row.event_type, row.ip_address)
        assert row.user_agent == UA, (row.event_type, row.user_agent)


def test_audit_row_for_invite_accepted_records_ip_and_user_agent(
    api_client, auth_db, captured_invites
) -> None:
    """The seventh API-driven event type: invite_accepted."""
    admin = _seed_user(auth_db, role="admin")
    admin_tokens = _login(api_client, admin["email"], headers={"user-agent": UA})
    email = f"teacher-{uuid.uuid4().hex[:8]}@test.example"

    created = api_client.post(
        "/api/v1/admin/teachers",
        json={"email": email, "full_name": "Audited Teacher", "subject": "Physics"},
        headers=_header(admin_tokens, UA),
    )
    assert created.status_code == 201, created.text
    assert len(captured_invites) == 1, captured_invites

    accepted = api_client.post(
        "/api/v1/auth/accept-invite",
        json={"token": captured_invites[0], "new_password": PASSWORD},
        headers={"user-agent": UA},
    )
    assert accepted.status_code == 200, accepted.text

    with auth_db.begin() as connection:
        teacher_id = connection.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": email}
        ).scalar_one()

    rows = _events_for(auth_db, teacher_id)
    invite_events = [row for row in rows if row.event_type == "invite_accepted"]
    assert len(invite_events) == 1, [row.event_type for row in rows]
    assert invite_events[0].ip_address == "testclient"
    assert invite_events[0].user_agent == UA


# --- scripts/create_admin.py --------------------------------------------------------


def test_create_admin_creates_the_account_and_its_audit_row(
    auth_db, monkeypatch, capsys
) -> None:
    import scripts.create_admin as create_admin_cli

    email = f"root-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    exit_code = create_admin_cli.main(["--email", email])
    out = capsys.readouterr()

    assert exit_code == 0, out.err
    assert PASSWORD not in out.out and PASSWORD not in out.err

    with auth_db.begin() as connection:
        user = connection.execute(
            text("SELECT id, role, status FROM users WHERE email = :e"), {"e": email}
        ).one()
        events = connection.execute(
            text(
                "SELECT event_type FROM auth_events WHERE user_id = :u"
            ),
            {"u": user.id},
        ).all()
    assert user.role == "admin"
    assert user.status == "active"
    assert [event.event_type for event in events] == ["admin_created"]


def test_create_admin_refuses_a_duplicate_email(auth_db, monkeypatch, capsys) -> None:
    import scripts.create_admin as create_admin_cli

    email = f"root-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    assert create_admin_cli.main(["--email", email]) == 0
    capsys.readouterr()

    exit_code = create_admin_cli.main(["--email", email.upper()])
    out = capsys.readouterr()
    assert exit_code == 1
    assert "refusing" in out.err.lower()

    with auth_db.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM users WHERE lower(email) = lower(:e)"),
            {"e": email},
        ).scalar_one()
    assert count == 1, "a duplicate run must never overwrite or duplicate the account"


def test_create_admin_refuses_production_before_opening_a_database(
    monkeypatch, capsys
) -> None:
    import scripts.create_admin as create_admin_cli

    monkeypatch.setattr(
        create_admin_cli, "get_settings", lambda: SimpleNamespace(ENVIRONMENT="production")
    )

    def _no_session():  # pragma: no cover - must never be reached
        raise AssertionError("the production guard must fire before SessionLocal()")

    monkeypatch.setattr(create_admin_cli, "SessionLocal", _no_session)
    exit_code = create_admin_cli.main(["--email", "root@example.com"])
    out = capsys.readouterr()
    assert exit_code == 1
    assert "--allow-production" in out.err


def test_create_admin_password_is_never_accepted_from_argv(capsys) -> None:
    import scripts.create_admin as create_admin_cli

    with pytest.raises(SystemExit) as excinfo:
        create_admin_cli.parse_args(
            ["--email", "root@example.com", "--password", PASSWORD]
        )
    assert excinfo.value.code != 0
    assert "--password" in capsys.readouterr().err


# --- rate limiting ------------------------------------------------------------------


def test_rate_limit_answer_is_detail_plus_retry_after(api_client, auth_db) -> None:
    """Exactly ``{"detail": ...}`` — the shared envelope — plus Retry-After."""
    url = "/api/v1/auth/forgot-password"
    for i in range(5):
        response = api_client.post(
            url, json={"email": f"probe-{uuid.uuid4().hex[:8]}@example.com"}
        )
        assert response.status_code == 204, response.text

    throttled = api_client.post(url, json={"email": "probe@example.com"})
    assert throttled.status_code == 429, throttled.text
    body = throttled.json()
    assert set(body) == {"detail"}
    assert body["detail"].startswith("Rate limit exceeded")
    retry_after = throttled.headers.get("retry-after")
    assert retry_after is not None, dict(throttled.headers)
    assert int(retry_after) >= 1


# --- verify_database.py -------------------------------------------------------------


def test_verify_database_structural_and_live_checks_pass(auth_db) -> None:
    """Migration/model drift is zero: structural AND live checks agree."""
    from app.core.config import get_settings
    from scripts.verify_database import live_checks, structural_checks

    assert structural_checks() is True
    assert live_checks(get_settings().database_url) is True
