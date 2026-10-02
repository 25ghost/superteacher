"""PATCH /api/v1/admin/registrations/{id}/status — transitions, audit, locking.

Covers the administrative status workflow over the real router (role
guards, the approved transitions, refusals naming both statuses, body
validation) plus the invariants HTTP alone cannot show:

- every ordered pair of the 6-state matrix: the six approved moves
  succeed with exactly one audit row each, the other 24 are 409s that
  change nothing,
- ``ended_at`` is stamped by the database clock on terminal moves only,
  never violating the ``ended_at >= started_at`` CHECK,
- two real sessions racing on one row (barrier + lock observation, no
  sleeps): exactly one transition wins,
- re-registration after a terminal status is still a 409 — the
  documented ``find_existing`` behavior, pinned here as agreed.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.registration_service import (
    ALLOWED_STATUS_TRANSITIONS,
    TERMINAL_ENROLLMENT_STATUSES,
    RegistrationConflictError,
)

from tests.integration.test_admin_registrations_list_api import (
    Context,
    _seed_enrollments,
    _student_header,
    _teacher_header,
)

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

LIST_PATH = "/api/v1/admin/registrations"

ALLOWED_PAIRS = [
    (source, target)
    for source, targets in sorted(ALLOWED_STATUS_TRANSITIONS.items())
    for target in sorted(targets)
]
DISALLOWED_PAIRS = [
    (source, target)
    for source in sorted(ALLOWED_STATUS_TRANSITIONS)
    for target in sorted(ALLOWED_STATUS_TRANSITIONS)
    if target != source and target not in ALLOWED_STATUS_TRANSITIONS[source]
]


# --- fixtures -----------------------------------------------------------------------


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
def status_db(clean_db):
    """Empty test database seeded with the verified-minimum catalog."""
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry

    with clean_db.begin() as connection:
        name = connection.execute(text("SELECT current_database()")).scalar_one()
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


@pytest.fixture()
def ctx(status_db) -> Context:
    return Context(status_db)


# --- principals & helpers ------------------------------------------------------------


def _admin_and_header(api_client: TestClient, engine) -> tuple[uuid.UUID, dict]:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    email = f"status-admin-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        user = User(
            email=email,
            role="admin",
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        session.commit()
        user_id = user.id
    finally:
        session.close()
    login = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return user_id, {"Authorization": f"Bearer {login.json()['access_token']}"}


def _make_admin(engine) -> uuid.UUID:
    """Admin row only (service-level tests never log in)."""
    from app.core.database import SessionLocal
    from app.models.user import User

    session = SessionLocal(bind=engine)
    try:
        user = User(
            email=f"svc-admin-{uuid.uuid4().hex[:8]}@test.example",
            role="admin",
            status="active",
            password_hash=None,
        )
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


def _enrollment_row(
    engine: Engine, enrollment_id: uuid.UUID
) -> tuple[str, datetime | None, datetime]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT status, ended_at, started_at "
                "FROM student_enrollments WHERE id = :id"
            ),
            {"id": enrollment_id},
        ).one()


def _audit_rows(engine: Engine, subject_user_id: str) -> list:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT actor_user_id, event_type, metadata_json "
                "FROM auth_events WHERE user_id = :user_id "
                "ORDER BY created_at, id"
            ),
            {"user_id": subject_user_id},
        ).fetchall()


def _wait_for_waiting_backend(engine: Engine, pid: int, timeout: float) -> bool:
    """Poll ``pg_stat_activity`` until ``pid`` waits on a lock.

    Every poll is a real round-trip (the loop paces itself — no
    sleeps); the deadline only guards against a broken test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            wait_type = connection.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).scalar_one_or_none()
        if wait_type == "Lock":
            return True
    return False


# --- authorization -------------------------------------------------------------------


def test_anonymous_gets_401(api_client, status_db) -> None:
    response = api_client.patch(
        f"{LIST_PATH}/{uuid.uuid4()}/status", json={"status": "active"}
    )
    assert response.status_code == 401


def test_student_token_gets_403(api_client, status_db) -> None:
    response = api_client.patch(
        f"{LIST_PATH}/{uuid.uuid4()}/status",
        headers=_student_header(api_client),
        json={"status": "active"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "administrator role required for this operation"


def test_teacher_token_gets_403(api_client, status_db) -> None:
    header = _teacher_header(api_client, status_db)
    response = api_client.patch(
        f"{LIST_PATH}/{uuid.uuid4()}/status",
        headers=header,
        json={"status": "active"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "administrator role required for this operation"


# --- happy path + audit --------------------------------------------------------------


def test_transition_returns_updated_summary_and_one_audit_row(
    api_client, status_db, ctx
) -> None:
    rows = _seed_enrollments(status_db, ctx, 1, status="pending")
    actor_id, admin = _admin_and_header(api_client, status_db)
    enrollment_id = uuid.UUID(rows[0]["enrollment_id"])

    response = api_client.patch(
        f"{LIST_PATH}/{enrollment_id}/status",
        headers=admin,
        json={"status": "active"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["enrollment_id"] == rows[0]["enrollment_id"]
    assert {
        "enrollment_id",
        "student_id",
        "status",
        "started_at",
        "academic_year",
        "school_name",
        "subjects",
    } <= set(body)

    status_now, ended_at, _started = _enrollment_row(status_db, enrollment_id)
    assert status_now == "active"
    assert ended_at is None  # active is not a terminal status

    events = _audit_rows(status_db, rows[0]["user_id"])
    assert len(events) == 1
    actor, event_type, metadata = events[0]
    assert actor == actor_id
    assert event_type == "registration_status_changed"
    assert json.loads(metadata) == {"from": "pending", "to": "active"}


def test_terminal_transition_stamps_ended_at(api_client, status_db, ctx) -> None:
    rows = _seed_enrollments(status_db, ctx, 1, status="pending")
    _, admin = _admin_and_header(api_client, status_db)
    path = f"{LIST_PATH}/{rows[0]['enrollment_id']}/status"

    activate = api_client.patch(path, headers=admin, json={"status": "active"})
    assert activate.status_code == 200, activate.text

    complete = api_client.patch(path, headers=admin, json={"status": "completed"})
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "completed"

    status_now, ended_at, started_at = _enrollment_row(
        status_db, uuid.UUID(rows[0]["enrollment_id"])
    )
    assert status_now == "completed"
    assert ended_at is not None
    assert ended_at >= started_at  # the DB date-order CHECK was never violated
    assert len(_audit_rows(status_db, rows[0]["user_id"])) == 2


# --- refusals ------------------------------------------------------------------------


def test_disallowed_transition_is_409_naming_both_statuses(
    api_client, status_db, ctx
) -> None:
    rows = _seed_enrollments(status_db, ctx, 1, status="pending")
    _, admin = _admin_and_header(api_client, status_db)

    response = api_client.patch(
        f"{LIST_PATH}/{rows[0]['enrollment_id']}/status",
        headers=admin,
        json={"status": "completed"},
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "pending" in detail and "completed" in detail

    status_now, ended_at, _ = _enrollment_row(
        status_db, uuid.UUID(rows[0]["enrollment_id"])
    )
    assert status_now == "pending"
    assert ended_at is None
    assert _audit_rows(status_db, rows[0]["user_id"]) == []


def test_same_status_is_409(api_client, status_db, ctx) -> None:
    rows = _seed_enrollments(status_db, ctx, 1, status="pending")
    _, admin = _admin_and_header(api_client, status_db)

    response = api_client.patch(
        f"{LIST_PATH}/{rows[0]['enrollment_id']}/status",
        headers=admin,
        json={"status": "pending"},
    )
    assert response.status_code == 409, response.text
    assert "already" in response.json()["detail"]
    assert _audit_rows(status_db, rows[0]["user_id"]) == []


def test_unknown_enrollment_is_404(api_client, status_db) -> None:
    _, admin = _admin_and_header(api_client, status_db)
    response = api_client.patch(
        f"{LIST_PATH}/{uuid.uuid4()}/status",
        headers=admin,
        json={"status": "active"},
    )
    assert response.status_code == 404, response.text


def test_body_validation_is_422(api_client, status_db, ctx) -> None:
    rows = _seed_enrollments(status_db, ctx, 1, status="pending")
    _, admin = _admin_and_header(api_client, status_db)
    path = f"{LIST_PATH}/{rows[0]['enrollment_id']}/status"

    unknown_value = api_client.patch(path, headers=admin, json={"status": "exploded"})
    assert unknown_value.status_code == 422, unknown_value.text
    missing = api_client.patch(path, headers=admin, json={})
    assert missing.status_code == 422, missing.text
    extra_key = api_client.patch(
        path, headers=admin, json={"status": "active", "note": "why not"}
    )
    assert extra_key.status_code == 422, extra_key.text

    status_now, _ended, _started = _enrollment_row(
        status_db, uuid.UUID(rows[0]["enrollment_id"])
    )
    assert status_now == "pending"  # nothing got through
    assert _audit_rows(status_db, rows[0]["user_id"]) == []


# --- the whole transition matrix (service level, no rate limit) ----------------------


@pytest.mark.parametrize(("source", "target"), ALLOWED_PAIRS)
def test_every_allowed_transition_succeeds_with_exactly_one_audit_row(
    status_db, ctx, source: str, target: str
) -> None:
    from app.core.database import SessionLocal
    from app.services.registration_service import update_registration_status

    rows = _seed_enrollments(status_db, ctx, 1, status=source)
    actor_id = _make_admin(status_db)
    enrollment_id = uuid.UUID(rows[0]["enrollment_id"])

    session = SessionLocal(bind=status_db)
    try:
        updated = update_registration_status(
            session, enrollment_id, target, actor_id=actor_id
        )
        session.commit()
    finally:
        session.close()

    assert updated.status == target
    status_now, ended_at, started_at = _enrollment_row(status_db, enrollment_id)
    assert status_now == target
    if target in TERMINAL_ENROLLMENT_STATUSES:
        assert ended_at is not None
        assert ended_at >= started_at  # date-order CHECK never violated
    else:
        assert ended_at is None

    events = _audit_rows(status_db, rows[0]["user_id"])
    assert len(events) == 1
    actor, event_type, metadata = events[0]
    assert actor == actor_id
    assert event_type == "registration_status_changed"
    assert json.loads(metadata) == {"from": source, "to": target}


def test_every_disallowed_pair_is_409_and_writes_nothing(status_db, ctx) -> None:
    from app.core.database import SessionLocal
    from app.services.registration_service import update_registration_status

    assert len(DISALLOWED_PAIRS) == 24, "the refusal matrix must be exhaustive"
    actor_id = _make_admin(status_db)

    for source, target in DISALLOWED_PAIRS:
        rows = _seed_enrollments(
            status_db, ctx, 1, status=source, name_prefix=f"Block {source}"
        )
        enrollment_id = uuid.UUID(rows[0]["enrollment_id"])

        session = SessionLocal(bind=status_db)
        try:
            with pytest.raises(RegistrationConflictError) as excinfo:
                update_registration_status(
                    session, enrollment_id, target, actor_id=actor_id
                )
            session.rollback()
        finally:
            session.close()

        detail = str(excinfo.value)
        assert source in detail and target in detail, (source, target, detail)
        status_now, ended_at, _started = _enrollment_row(status_db, enrollment_id)
        assert status_now == source, (source, target)
        assert ended_at is None, (source, target)
        assert _audit_rows(status_db, rows[0]["user_id"]) == [], (source, target)


# --- concurrency (two real sessions, deterministic, no sleeps) -----------------------


def test_concurrent_double_transition_cannot_both_succeed(status_db, ctx) -> None:
    from app.core.database import SessionLocal
    from app.services.registration_service import update_registration_status

    rows = _seed_enrollments(status_db, ctx, 1, status="pending")
    enrollment_id = uuid.UUID(rows[0]["enrollment_id"])
    actor_id = _make_admin(status_db)

    session_a = SessionLocal(bind=status_db)
    session_b = SessionLocal(bind=status_db)
    outcomes: dict[str, str] = {}

    # A locks the row and changes it — transaction deliberately left open.
    updated_a = update_registration_status(
        session_a, enrollment_id, "active", actor_id=actor_id
    )
    assert updated_a.status == "active"

    pid_b = session_b.execute(text("SELECT pg_backend_pid()")).scalar_one()
    barrier = threading.Barrier(2)

    def run_b() -> None:
        try:
            barrier.wait(timeout=15)
            update_registration_status(
                session_b, enrollment_id, "active", actor_id=actor_id
            )
            session_b.commit()
            outcomes["b"] = "success"
        except RegistrationConflictError:
            session_b.rollback()
            outcomes["b"] = "conflict"
        except Exception as exc:  # pragma: no cover - diagnostic escape hatch
            session_b.rollback()
            outcomes["b"] = f"error: {exc!r}"

    thread = threading.Thread(target=run_b, name="transition-b")
    thread.start()
    barrier.wait(timeout=15)  # both transitions are now in flight

    # Commit A only once B is observably waiting on a lock: with the
    # FOR UPDATE in place B waits on its row read; without it B reads
    # "pending", decides the move is allowed and waits on its UPDATE
    # instead. Either way, seeing B wait proves the race really happened.
    assert _wait_for_waiting_backend(status_db, pid_b, timeout=15.0), (
        "session B never waited on a lock — the transition was not serialized"
    )
    session_a.commit()
    thread.join(timeout=15)
    assert not thread.is_alive(), "session B never finished"

    session_a.close()
    session_b.close()

    assert outcomes.get("b") == "conflict", outcomes
    status_now, ended_at, _started = _enrollment_row(status_db, enrollment_id)
    assert status_now == "active"
    assert ended_at is None
    events = _audit_rows(status_db, rows[0]["user_id"])
    assert len(events) == 1, "only the winning transition may write an audit row"
    assert json.loads(events[0][2]) == {"from": "pending", "to": "active"}


# --- documented re-registration behavior (amendment) ---------------------------------


def test_re_registration_after_a_terminal_status_is_still_409(
    api_client, status_db, ctx
) -> None:
    """``find_existing`` matches ANY status on purpose: a cancelled (or
    withdrawn/transferred/completed) enrollment keeps blocking a new
    registration for the same (student, academic year) — the UNIQUE
    constraint agrees. Pinned here so a future "helpful" change has to
    consciously break this test."""
    rows = _seed_enrollments(status_db, ctx, 1, status="cancelled")
    _, admin = _admin_and_header(api_client, status_db)

    response = api_client.post(
        LIST_PATH,
        headers=admin,
        json={
            "student_id": rows[0]["student_id"],
            "academic_year_id": str(ctx.year_one.id),
            "pathway": "O_LEVEL",
            "education_level": "S1",
            "school_id": str(ctx.school_one.id),
        },
    )
    assert response.status_code == 409, response.text
    assert "already enrolled" in response.json()["detail"]
