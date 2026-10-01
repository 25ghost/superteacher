"""Login lockout, audit request metadata and the /health throttle (slice 7).

Three hardening guarantees, each pinned against the real routes:

- **Lockout**: the 5th refused login attempt within the window locks the
  account for 15 minutes (``LOGIN_LOCKOUT_THRESHOLD`` /
  ``LOGIN_LOCKOUT_MINUTES``). While locked, even the correct password is
  refused with the *same* generic 401 detail as a wrong password — the
  response never discloses the lock. A successful login clears the
  budget, an expired lock grants a fresh one, and an administrator can
  unlock early via ``POST /admin/users/{id}/unlock`` (admin-only, 409
  when nothing is locked, 404 unknown id).
- **Audit request metadata**: ``login``, ``login_failed``, ``login_locked``
  and ``logout`` events carry the caller's IP and User-Agent (truncated to
  the column widths), so the audit trail can answer "where from?".
- **Health throttle**: ``GET /health`` answers 429 once
  ``RATE_LIMIT_HEALTH`` requests land inside one minute, so a probe storm
  cannot pin the event loop.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.router import api_router
from app.core import security
from app.core.config import get_settings
from app.core.database import Base, get_db
from app.core.rate_limit import limiter
from app.models.auth_event import AuthEvent
from app.models.enums import UserRole, UserStatus
from app.models.user import User
import app.models  # noqa: F401  (registers every table)

PASSWORD = "correct horse battery staple"


# --- scaffolding ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def api_app(session: Session) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: session
    app.include_router(api_router, prefix="/api/v1")
    return app


@pytest.fixture()
def api_client(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


def _seed_user(
    session: Session,
    *,
    role: str = UserRole.STUDENT.value,
    status: str = UserStatus.ACTIVE.value,
    password: str | None = PASSWORD,
) -> User:
    user = User(
        email=f"{uuid.uuid4().hex[:12]}@lockout.example.com",
        role=role,
        status=status,
        password_hash=security.hash_password(password) if password else None,
    )
    session.add(user)
    session.commit()
    return user


def _login(client: TestClient, user: User, password: str = PASSWORD, **headers):
    return client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": password},
        **({"headers": headers} if headers else {}),
    )


def _events(session: Session, user: User, event_type: str) -> list[AuthEvent]:
    return list(
        session.scalars(
            select(AuthEvent).where(
                AuthEvent.user_id == user.id,
                AuthEvent.event_type == event_type,
            )
        ).all()
    )


# --- lockout -------------------------------------------------------------------------


def test_fifth_refused_login_locks_the_account(session: Session, api_client: TestClient) -> None:
    user = _seed_user(session)
    settings = get_settings()

    for attempt in range(1, settings.LOGIN_LOCKOUT_THRESHOLD + 1):
        response = _login(api_client, user, password="wrong password")
        assert response.status_code == 401, response.text
        # The refusal detail must be stable: nothing about the attempt
        # (or the approaching lock) may be disclosed.
        assert response.json()["detail"] == "invalid email or password"

    session.expire_all()
    locked = session.get(User, user.id)
    assert locked.failed_login_count == settings.LOGIN_LOCKOUT_THRESHOLD
    assert locked.locked_until is not None
    until = locked.locked_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert now < until <= now + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES + 1)

    assert len(_events(session, user, "login_locked")) == 1

    # While locked, the CORRECT password is refused with the same generic
    # detail — the response must not disclose the lock.
    refused = _login(api_client, user, password=PASSWORD)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "invalid email or password"
    assert len(_events(session, user, "login_failed")) == settings.LOGIN_LOCKOUT_THRESHOLD + 1


def test_successful_login_clears_the_failure_budget(session: Session, api_client: TestClient) -> None:
    user = _seed_user(session)
    threshold = get_settings().LOGIN_LOCKOUT_THRESHOLD

    for _ in range(threshold - 1):
        assert _login(api_client, user, password="wrong").status_code == 401

    session.expire_all()
    assert session.get(User, user.id).failed_login_count == threshold - 1

    assert _login(api_client, user).status_code == 200

    session.expire_all()
    unlocked = session.get(User, user.id)
    assert unlocked.failed_login_count == 0
    assert unlocked.locked_until is None


def test_an_expired_lock_grants_a_fresh_budget(session: Session, api_client: TestClient) -> None:
    user = _seed_user(session)
    threshold = get_settings().LOGIN_LOCKOUT_THRESHOLD
    user.failed_login_count = threshold
    user.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()

    assert _login(api_client, user).status_code == 200

    session.expire_all()
    fresh = session.get(User, user.id)
    assert fresh.failed_login_count == 0
    assert fresh.locked_until is None


def test_lockout_settings_match_the_stated_policy() -> None:
    settings = get_settings()
    assert settings.LOGIN_LOCKOUT_THRESHOLD == 5
    assert settings.LOGIN_LOCKOUT_MINUTES == 15


# --- administrative unlock ------------------------------------------------------------


def _lock(session: Session, user: User) -> None:
    user.failed_login_count = get_settings().LOGIN_LOCKOUT_THRESHOLD
    user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
    session.commit()


def test_admin_can_unlock_a_locked_account(session: Session, api_client: TestClient) -> None:
    admin = _seed_user(session, role=UserRole.ADMIN.value)
    locked_user = _seed_user(session)
    _lock(session, locked_user)
    header = {"Authorization": f"Bearer {security.create_access_token(admin.id, admin.role)}"}

    response = api_client.post(
        f"/api/v1/admin/users/{locked_user.id}/unlock", headers=header
    )
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == str(locked_user.id)

    session.expire_all()
    cleared = session.get(User, locked_user.id)
    assert cleared.failed_login_count == 0
    assert cleared.locked_until is None
    assert len(_events(session, locked_user, "user_unlocked")) == 1
    assert _events(session, locked_user, "user_unlocked")[0].actor_user_id == admin.id

    # The account can authenticate again immediately.
    assert _login(api_client, locked_user).status_code == 200


def test_unlock_refuses_anonymous_and_non_administrators(
    session: Session, api_client: TestClient
) -> None:
    locked_user = _seed_user(session)
    _lock(session, locked_user)
    path = f"/api/v1/admin/users/{locked_user.id}/unlock"

    assert api_client.post(path).status_code == 401

    for role in (UserRole.STUDENT.value, UserRole.TEACHER.value):
        caller = _seed_user(session, role=role)
        header = {
            "Authorization": f"Bearer {security.create_access_token(caller.id, caller.role)}"
        }
        response = api_client.post(path, headers=header)
        assert response.status_code == 403, f"{role} -> {response.status_code}"
        assert response.json()["detail"] == "administrator role required for this operation"


def test_unlock_unknown_id_is_404_and_unlocked_account_is_409(
    session: Session, api_client: TestClient
) -> None:
    admin = _seed_user(session, role=UserRole.ADMIN.value)
    header = {"Authorization": f"Bearer {security.create_access_token(admin.id, admin.role)}"}

    unknown = api_client.post(f"/api/v1/admin/users/{uuid.uuid4()}/unlock", headers=header)
    assert unknown.status_code == 404, unknown.text

    healthy = _seed_user(session)
    conflict = api_client.post(f"/api/v1/admin/users/{healthy.id}/unlock", headers=header)
    assert conflict.status_code == 409, conflict.text


# --- audit request metadata ------------------------------------------------------------


def test_login_and_logout_events_record_ip_and_user_agent(
    session: Session, api_client: TestClient
) -> None:
    user = _seed_user(session)
    # TestClient identifies itself as host "testclient" and UA
    # "testclient"; an explicit UA proves the two fields are populated
    # from different sources (client address vs header) and not swapped.
    ua = "RahuraAuditTest/1.0"

    assert (
        _login(api_client, user, password="wrong", **{"user-agent": ua}).status_code
        == 401
    )
    failed = _events(session, user, "login_failed")[-1]
    assert failed.ip_address == "testclient"
    assert failed.user_agent == ua

    assert _login(api_client, user, **{"user-agent": ua}).status_code == 200
    login = _events(session, user, "login")[-1]
    assert login.ip_address == "testclient"
    assert login.user_agent == ua

    tokens = _login(api_client, user).json()
    logout = api_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={
            "Authorization": f"Bearer {tokens['access_token']}",
            "User-Agent": ua,
        },
    )
    assert logout.status_code == 204, logout.text
    event = _events(session, user, "logout")[-1]
    assert event.ip_address == "testclient"
    assert event.user_agent == ua


def test_a_very_long_user_agent_is_truncated_to_the_column_width(
    session: Session, api_client: TestClient
) -> None:
    user = _seed_user(session)
    long_agent = "u" * 900

    response = _login(api_client, user, **{"user-agent": long_agent})
    assert response.status_code == 200, response.text

    login = _events(session, user, "login")[-1]
    assert login.user_agent is not None
    assert len(login.user_agent) == 255


# --- health throttle --------------------------------------------------------------------


def test_health_endpoint_is_rate_limited(session: Session, api_client: TestClient) -> None:
    limit = int(get_settings().RATE_LIMIT_HEALTH.split("/")[0])

    for attempt in range(limit):
        response = api_client.get("/api/v1/health")
        assert response.status_code == 200, f"attempt {attempt}: {response.status_code}"

    throttled = api_client.get("/api/v1/health")
    assert throttled.status_code == 429, throttled.text
