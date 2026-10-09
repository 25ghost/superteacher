"""Classroom WebSocket integration tests (PostgreSQL, guarded test DB).

Slice 3C end-to-end contract (§38-§45, §54) against ``super_teacher_db_test``:

- **ticket routes**: role split (anonymous 401, wrong role 403 from the
  guard), LIVE-only minting (409 for scheduled/cancelled/ended classes),
  and the L6 rule — unknown ids, foreign teachers and missing
  enrollments all answer the SAME 404 template as an unknown id;
- **the handshake**: accept first, then close — 4001 for missing/garbage/
  expired/used/wrong-class/non-ticket credentials, 4003 for an identity
  that may not enter (revoked enrollment), 4008 when the class ended
  between mint and handshake, 1008 for a duplicate connection. A refused
  handshake creates NO attendance segment, NO presence event, NO audit;
- **ticket semantics**: single-use (second connect 4001), TTL, class
  scoping, and only the SHA-256 digest is ever persisted — the raw value
  never appears in any column;
- **the live classroom (§54)**: snapshot ``[others..., self]``, the join
  broadcast reaching everyone else (never yourself), ``presence.left``
  only while the class is live, silent ``presence.heartbeat`` control
  frames (never a reply, never a ClassMessage), ``class.ended`` closing
  every socket 1000 with segments finalized at ``actual_ended_at``;
- **teardown paths**: stale sockets (1008 + segment closed), invalid
  frames (advisory ``error`` then class.ended still arrives), enrollment
  revocation (``error NOT_AUTHORIZED`` + 4003 for the student and
  ``presence.left`` for the teacher), heartbeat auto-end of an overdue
  class (class.ended + 1000), and ``class.started`` reaching a raw
  PostgreSQL LISTEN consumer (§25).

The app client is ENTERED (``with TestClient``): HTTP requests and every
WebSocket then share ONE portal event loop. The realtime bus binds the
loop of the newest live socket (single binding), so sockets spawned on
separate TestClient portals would race each other out of event delivery.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


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
    # Enter the client: one portal event loop for the whole module (see
    # the module docstring) — and lifespan, which has no handlers.
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _session(engine):
    from app.core.database import SessionLocal

    return SessionLocal(bind=engine)


def _seed_catalog(engine) -> dict:
    """A minimal coherent catalog (year, pathway, level, program, subject)."""
    from app.models.academic_year import AcademicYear
    from app.models.education_level import EducationLevel
    from app.models.enums import AcademicYearStatus
    from app.models.pathway import Pathway
    from app.models.pathway_level import PathwayLevel
    from app.models.program import Program
    from app.models.program_subject import ProgramSubject
    from app.models.program_version import ProgramVersion
    from app.models.subject import Subject

    session = _session(engine)
    try:
        year = AcademicYear(
            name=f"IA-{uuid.uuid4().hex[:6]}",
            start_date=datetime(2026, 9, 1).date(),
            end_date=datetime(2027, 6, 30).date(),
            status=AcademicYearStatus.ACTIVE.value,
        )
        pathway = Pathway(code=f"P-{uuid.uuid4().hex[:6]}", name="O Level")
        level = EducationLevel(code=f"L-{uuid.uuid4().hex[:6]}", name="Senior 3", level_number=3)
        session.add_all([year, pathway, level])
        session.flush()
        session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

        program = Program(
            code=f"C-{uuid.uuid4().hex[:6]}", name="Combination", program_type="combination"
        )
        session.add(program)
        session.flush()
        program_version = ProgramVersion(
            program_id=program.id,
            academic_year_id=year.id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            code=f"CV-{uuid.uuid4().hex[:6]}",
            name="Combination 2026",
        )
        session.add(program_version)

        maths = Subject(code=f"S-{uuid.uuid4().hex[:6]}", name="Mathematics")
        session.add(maths)
        session.flush()
        session.add(
            ProgramSubject(program_version_id=program_version.id, subject_id=maths.id)
        )
        session.commit()
        return {
            "academic_year_id": str(year.id),
            "pathway_id": str(pathway.id),
            "education_level_id": str(level.id),
            "program_version_id": str(program_version.id),
            "subject_id": str(maths.id),
        }
    finally:
        session.close()


def _seed_account(engine, *, role: str) -> dict:
    """An ACTIVE account for the role, with the profile the role needs."""
    from app.core.security import hash_password
    from app.models.student import Student
    from app.models.teacher import Teacher
    from app.models.user import User

    session = _session(engine)
    try:
        email = f"{role}-{uuid.uuid4().hex[:8]}@test.example"
        account = User(
            email=email,
            role=role,
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(account)
        session.flush()
        if role == "teacher":
            session.add(
                Teacher(
                    user_id=account.id,
                    full_name="Classroom Teacher",
                    subject="Mathematics",
                    verification_status="approved",
                )
            )
        elif role == "student":
            session.add(Student(user_id=account.id, full_name="Classroom Student"))
        session.commit()
        return {"email": email, "password": PASSWORD, "user_id": str(account.id)}
    finally:
        session.close()


def _login(api_client: TestClient, account: dict) -> dict:
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _header(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _window(offset_hours: float = 0.0, duration_hours: float = 1.0) -> tuple[datetime, datetime]:
    """A future window (start aligned to the hour) - 'start' stays legal."""
    base = datetime.now(timezone.utc) + timedelta(days=1)
    start = base.replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset_hours)
    return start, start + timedelta(hours=duration_hours)


@pytest.fixture()
def classroom(clean_db, api_client: TestClient):
    """Seeded catalog + logged-in teacher/student + one owned offering."""
    engine = clean_db
    catalog = _seed_catalog(engine)
    teacher = _seed_account(engine, role="teacher")
    student = _seed_account(engine, role="student")
    teacher_header = _header(_login(api_client, teacher))
    student_header = _header(_login(api_client, student))

    offering = api_client.post(
        "/api/v1/me/teacher/offerings", json=catalog, headers=teacher_header
    )
    assert offering.status_code == 201, offering.text
    return {
        "engine": engine,
        "teacher": teacher,
        "student": student,
        "teacher_header": teacher_header,
        "student_header": student_header,
        "offering_id": offering.json()["offering_id"],
        # The catalog the offering was built from: a second teacher may
        # publish ANOTHER offering of the very same subject (§39).
        "catalog": catalog,
    }


@pytest.fixture()
def stale_settings():
    """Shrink the stale-socket timeout so §33 fires inside one test."""
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.WS_STALE_TIMEOUT_SECONDS
    settings.WS_STALE_TIMEOUT_SECONDS = 0.3
    try:
        yield settings
    finally:
        settings.WS_STALE_TIMEOUT_SECONDS = original


@pytest.fixture(autouse=True)
def _registry_idle():
    """No live connection may leak between tests (teardown is asynchronous)."""
    from app.realtime.connections import registry

    _wait_until(lambda: len(registry) == 0, message="registry busy before test")
    yield
    _wait_until(lambda: len(registry) == 0, message="registry busy after test")


# --- classroom helpers ----------------------------------------------------------------


def _teacher_base(classroom: dict, class_id: str) -> str:
    return f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}"


def _create_class(api_client: TestClient, classroom: dict, offset: float = 0.0) -> str:
    start, end = _window(offset)
    created = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json={"scheduled_start_at": start.isoformat(), "scheduled_end_at": end.isoformat()},
        headers=classroom["teacher_header"],
    )
    assert created.status_code == 201, created.text
    return created.json()["class_id"]


def _start_class(api_client: TestClient, classroom: dict, class_id: str) -> None:
    started = api_client.post(
        f"{_teacher_base(classroom, class_id)}/start",
        headers=classroom["teacher_header"],
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "live"


def _end_class(api_client: TestClient, classroom: dict, class_id: str) -> None:
    ended = api_client.post(
        f"{_teacher_base(classroom, class_id)}/end",
        headers=classroom["teacher_header"],
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "ended"


def _cancel_class(api_client: TestClient, classroom: dict, class_id: str) -> None:
    cancelled = api_client.post(
        f"{_teacher_base(classroom, class_id)}/cancel",
        headers=classroom["teacher_header"],
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


def _enroll(api_client: TestClient, classroom: dict, header: dict | None = None) -> dict:
    enrolled = api_client.post(
        f"/api/v1/marketplace/offerings/{classroom['offering_id']}/enroll",
        headers=header or classroom["student_header"],
    )
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json()


def _seed_second_student(api_client: TestClient, classroom: dict) -> dict:
    account = _seed_account(classroom["engine"], role="student")
    header = _header(_login(api_client, account))
    _enroll(api_client, classroom, header=header)
    return {**account, "header": header}


# --- ticket helpers -------------------------------------------------------------------


def _teacher_ticket_request(
    api_client: TestClient, classroom: dict, class_id: str, header: dict | None = None
):
    return api_client.post(
        f"{_teacher_base(classroom, class_id)}/ws-ticket",
        headers=header or classroom["teacher_header"],
    )


def _mint_teacher(api_client: TestClient, classroom: dict, class_id: str) -> dict:
    response = _teacher_ticket_request(api_client, classroom, class_id)
    assert response.status_code == 201, response.text
    return _assert_ticket_shape(response.json(), class_id)


def _mint_student(
    api_client: TestClient, classroom: dict, class_id: str, header: dict | None = None
) -> dict:
    response = api_client.post(
        f"/api/v1/me/classes/{class_id}/ws-ticket",
        headers=header or classroom["student_header"],
    )
    assert response.status_code == 201, response.text
    return _assert_ticket_shape(response.json(), class_id)


def _assert_ticket_shape(body: dict, class_id: str) -> dict:
    assert set(body) == {"ticket", "expires_at", "ws_path"}
    assert body["ws_path"] == f"/ws/classes/{class_id}"
    # Opaque, unguessable, never the class id (§5).
    assert len(body["ticket"]) >= 40
    assert body["ticket"] != class_id
    return body


def _ws_url(class_id: str, ticket: str | None = None) -> str:
    path = f"/ws/classes/{class_id}"
    return path if ticket is None else f"{path}?ticket={ticket}"


# --- websocket / synchronization helpers ----------------------------------------------


def _ref(class_id: str, user_id: str) -> str:
    from app.realtime.connections import participant_ref

    return participant_ref(uuid.UUID(class_id), uuid.UUID(user_id))


def _expect_close(websocket, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as excinfo:
        websocket.receive_json()
    assert excinfo.value.code == code


def _wait_until(predicate, *, timeout: float = 5.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {message}")


def _wait_gone(class_id: str, ref: str) -> None:
    """Wait until the async cleanup unregistered that participant."""
    from app.realtime.connections import registry

    def _gone() -> bool:
        return not any(
            connection.participant_ref == ref
            for connection in registry.connections_for_class(uuid.UUID(class_id))
        )

    _wait_until(_gone, message=f"connection {ref} still registered")


def registry_count(class_id: str) -> int:
    from app.realtime.connections import registry

    return len(registry.connections_for_class(uuid.UUID(class_id)))


# --- database helpers ------------------------------------------------------------------


def _segments(engine, class_id: str):
    from app.models.class_attendance_segment import ClassAttendanceSegment

    session = _session(engine)
    try:
        rows = list(
            session.scalars(
                select(ClassAttendanceSegment).where(
                    ClassAttendanceSegment.class_session_id == uuid.UUID(class_id)
                )
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def _class_row(engine, class_id: str):
    from app.models.online_class_session import OnlineClassSession

    session = _session(engine)
    try:
        row = session.get(OnlineClassSession, uuid.UUID(class_id))
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def _last_seen(engine, class_id: str, student_id: uuid.UUID):
    from app.models.class_attendance_segment import ClassAttendanceSegment

    session = _session(engine)
    try:
        return session.scalar(
            select(ClassAttendanceSegment.last_seen_at).where(
                ClassAttendanceSegment.class_session_id == uuid.UUID(class_id),
                ClassAttendanceSegment.student_id == student_id,
            )
        )
    finally:
        session.close()


def _student_profile_id(engine, user_id: str) -> uuid.UUID:
    from app.models.student import Student

    session = _session(engine)
    try:
        student_id = session.scalar(
            select(Student.id).where(Student.user_id == uuid.UUID(user_id))
        )
        assert student_id is not None
        return student_id
    finally:
        session.close()


def _expire_ticket(engine, raw_ticket: str) -> None:
    """Push one minted ticket past its TTL (simulates the 120 s passing)."""
    digest = hashlib.sha256(raw_ticket.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE class_ws_tickets SET expires_at = :past "
                "WHERE token_hash = :digest"
            ),
            {
                "past": datetime.now(timezone.utc) - timedelta(seconds=5),
                "digest": digest,
            },
        )


def _class_message_count(engine, class_id: str) -> int:
    from app.models.class_message import ClassMessage

    session = _session(engine)
    try:
        return session.scalar(
            select(func.count())
            .select_from(ClassMessage)
            .where(ClassMessage.class_session_id == uuid.UUID(class_id))
        )
    finally:
        session.close()


# --- ticket route guards (§39) ----------------------------------------------------------


def test_ticket_routes_role_split(api_client: TestClient, classroom: dict) -> None:
    class_id = _create_class(api_client, classroom)
    teacher_url = f"{_teacher_base(classroom, class_id)}/ws-ticket"
    student_url = f"/api/v1/me/classes/{class_id}/ws-ticket"
    admin = _seed_account(classroom["engine"], role="admin")
    admin_header = _header(_login(api_client, admin))

    # Anonymous -> 401 from the guard, never a handler response.
    assert api_client.post(teacher_url).status_code == 401
    assert api_client.post(student_url).status_code == 401

    # Wrong role -> 403 from the guard.
    assert (
        api_client.post(teacher_url, headers=classroom["student_header"]).status_code
        == 403
    )
    assert api_client.post(teacher_url, headers=admin_header).status_code == 403
    assert (
        api_client.post(student_url, headers=classroom["teacher_header"]).status_code
        == 403
    )
    assert api_client.post(student_url, headers=admin_header).status_code == 403


def test_teacher_ticket_requires_live_owned_class(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]

    # Scheduled -> 409: only a LIVE class is ticketable.
    scheduled = _create_class(api_client, classroom, offset=0.0)
    assert _teacher_ticket_request(api_client, classroom, scheduled).status_code == 409

    # Cancelled -> 409 (cancelled can never go live).
    cancelled = _create_class(api_client, classroom, offset=2.0)
    _cancel_class(api_client, classroom, cancelled)
    assert _teacher_ticket_request(api_client, classroom, cancelled).status_code == 409

    # LIVE -> 201 with the opaque ticket and the socket path.
    live = _create_class(api_client, classroom, offset=4.0)
    _start_class(api_client, classroom, live)
    ticket = _mint_teacher(api_client, classroom, live)
    assert ticket["ws_path"] == f"/ws/classes/{live}"

    # Ended -> 409 (terminal states are never ticketable).
    _end_class(api_client, classroom, live)
    assert _teacher_ticket_request(api_client, classroom, live).status_code == 409

    # Unknown class id on your own offering -> 404, exact L6 template.
    unknown = str(uuid.uuid4())
    response = _teacher_ticket_request(api_client, classroom, unknown)
    assert response.status_code == 404
    assert response.json()["detail"] == f"no online class with id {unknown}"

    # A foreign teacher (approved, but not the offering's) gets the SAME
    # 404 template for our offering as for an unknown one — no existence
    # leak either way (L6).
    foreign = _seed_account(engine, role="teacher")
    foreign_header = _header(_login(api_client, foreign))
    response = _teacher_ticket_request(api_client, classroom, live, header=foreign_header)
    assert response.status_code == 404
    assert (
        response.json()["detail"]
        == f"no teaching offering with id {classroom['offering_id']}"
    )
    unknown_offering = str(uuid.uuid4())
    response = api_client.post(
        f"/api/v1/me/teacher/offerings/{unknown_offering}/classes/{live}/ws-ticket",
        headers=classroom["teacher_header"],
    )
    assert response.status_code == 404
    assert response.json()["detail"] == f"no teaching offering with id {unknown_offering}"


def test_student_ticket_requires_active_enrollment(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    # Unknown class id -> 404 with the L6 template.
    unknown = str(uuid.uuid4())
    response = api_client.post(
        f"/api/v1/me/classes/{unknown}/ws-ticket",
        headers=classroom["student_header"],
    )
    assert response.status_code == 404
    assert response.json()["detail"] == f"no online class with id {unknown}"

    # A real class without an ACTIVE enrollment answers the SAME template.
    stranger = _seed_account(engine, role="student")
    stranger_header = _header(_login(api_client, stranger))
    response = api_client.post(
        f"/api/v1/me/classes/{class_id}/ws-ticket", headers=stranger_header
    )
    assert response.status_code == 404
    assert response.json()["detail"] == f"no online class with id {class_id}"

    # Enrolled -> 201.
    _mint_student(api_client, classroom, class_id)


def test_a_student_of_another_offering_gets_the_same_404(
    api_client: TestClient, classroom: dict
) -> None:
    """§39: enrolled in the SAME subject under another teacher (§39).

    Both classes exist, both are real, but they belong to different
    offerings: the student's enrollment there grants nothing here, and
    the answer is byte-identical to an id nobody has ever seen.
    """
    engine = classroom["engine"]
    other_teacher = _seed_account(engine, role="teacher")
    other_header = _header(_login(api_client, other_teacher))
    published = api_client.post(
        "/api/v1/me/teacher/offerings",
        json=classroom["catalog"],
        headers=other_header,
    )
    assert published.status_code == 201, published.text
    other_offering_id = published.json()["offering_id"]
    assert other_offering_id != classroom["offering_id"]

    enrolled = api_client.post(
        f"/api/v1/marketplace/offerings/{other_offering_id}/enroll",
        headers=classroom["student_header"],
    )
    assert enrolled.status_code == 201, enrolled.text

    # Our offering's class — the student is NOT enrolled in it.
    class_id = _create_class(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    unknown = str(uuid.uuid4())
    r_unknown = api_client.post(
        f"/api/v1/me/classes/{unknown}/ws-ticket",
        headers=classroom["student_header"],
    )
    r_other = api_client.post(
        f"/api/v1/me/classes/{class_id}/ws-ticket",
        headers=classroom["student_header"],
    )
    assert r_unknown.status_code == r_other.status_code == 404
    # Same template, same silence: the id itself is echoed, never the
    # reason (nothing reveals that one id exists and the other does not).
    assert r_unknown.json() == {"detail": f"no online class with id {unknown}"}
    assert r_other.json() == {"detail": f"no online class with id {class_id}"}


# --- handshake refusals (§38/§39/§41) ----------------------------------------------------


def test_class_ends_before_handshake_closes_4008(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    teacher_ticket = _mint_teacher(api_client, classroom, class_id)["ticket"]
    student_ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    _end_class(api_client, classroom, class_id)

    # Accept first, then close with the application code — and a refused
    # handshake leaves no attendance segment behind (§11).
    with api_client.websocket_connect(_ws_url(class_id, student_ticket)) as student_ws:
        _expect_close(student_ws, 4008)
    with api_client.websocket_connect(_ws_url(class_id, teacher_ticket)) as teacher_ws:
        _expect_close(teacher_ws, 4008)

    assert _segments(engine, class_id) == []


def test_class_ending_during_the_handshake_is_refused_before_presence(
    api_client: TestClient, classroom: dict, monkeypatch
) -> None:
    """§38: the class ends between authorization and registration.

    The window is made deterministic by ending the class from inside the
    authorization step itself. The registration-time LIVE re-check must
    then refuse the socket 4008 before any snapshot or broadcast — no
    presence event, no registry entry, no attendance that outlives the
    class end.
    """
    from app.api.v1.endpoints import classroom_ws
    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services import online_class_service

    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    # No pg_notify from this transition: the class.ended delivery is
    # asynchronous, so it could otherwise land AFTER registration and
    # close the socket 1000 — the very race this test must not race.
    suppressed: list[dict] = []

    class _SilentBus:
        def publish(self, session, event: dict) -> None:
            suppressed.append(event)

    published: list[dict] = []
    real_authorize = classroom_ws._authorize

    def _authorize_then_end(user_id, cid, connection_id):
        identity = real_authorize(user_id, cid, connection_id)  # commits the join
        # The class ends in the window §38 describes: right after the
        # segment opened, before the endpoint could register the socket.
        session = SessionLocal()
        try:
            teacher = session.get(User, uuid.UUID(classroom["teacher"]["user_id"]))
            online_class_service.end_my_class(
                session,
                teacher,
                uuid.UUID(classroom["offering_id"]),
                cid,
            )
            session.commit()
        finally:
            session.close()
        return identity

    monkeypatch.setattr(classroom_ws, "_authorize", _authorize_then_end)
    monkeypatch.setattr(classroom_ws, "_publish", published.append)
    monkeypatch.setattr(online_class_service, "get_event_bus", lambda: _SilentBus())

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        _expect_close(websocket, 4008)

    # The transition really happened — it just never reached this socket.
    assert [event["type"] for event in suppressed] == ["class.ended"]
    # Nothing about this refused handshake ever reached a client (§19).
    assert published == []
    _wait_gone(class_id, student_ref)
    assert registry_count(class_id) == 0

    # The join the authorization opened was finalized by that same class
    # end — participation stops at actual_ended_at, never later.
    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].left_at == class_row.actual_ended_at


def test_an_inactive_account_cannot_connect_with_a_valid_ticket(
    api_client: TestClient, classroom: dict
) -> None:
    """§11 step 5: the account gate runs after the ticket is redeemed."""
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET status = 'disabled' WHERE id = :id"),
            {"id": classroom["student"]["user_id"]},
        )

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        _expect_close(websocket, 4003)

    assert _segments(engine, class_id) == []
    _wait_gone(class_id, student_ref)
    assert registry_count(class_id) == 0


def test_duplicate_connections_refused_1008(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    teacher_ref = _ref(class_id, classroom["teacher"]["user_id"])
    student_ref = _ref(class_id, classroom["student"]["user_id"])

    teacher_ticket = _mint_teacher(api_client, classroom, class_id)["ticket"]
    student_ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, teacher_ticket)) as teacher_ws:
        assert teacher_ws.receive_json()["participant"]["participant_ref"] == teacher_ref
        with api_client.websocket_connect(_ws_url(class_id, student_ticket)) as student_ws:
            # snapshot [others..., self]
            assert (
                student_ws.receive_json()["participant"]["participant_ref"] == teacher_ref
            )
            assert student_ws.receive_json()["participant"]["participant_ref"] == student_ref
            assert (
                teacher_ws.receive_json()["participant"]["participant_ref"] == student_ref
            )

            # A second student handshake: the segment's unique open index
            # refuses it with 1008 — no second segment, no presence event.
            second_student_ticket = _mint_student(api_client, classroom, class_id)["ticket"]
            with api_client.websocket_connect(
                _ws_url(class_id, second_student_ticket)
            ) as duplicate:
                _expect_close(duplicate, 1008)
            open_now = [s for s in _segments(engine, class_id) if s.left_at is None]
            assert len(open_now) == 1

        # The first student left normally (class still live): segment closed.
        _wait_gone(class_id, student_ref)
        assert [s for s in _segments(engine, class_id) if s.left_at is None] == []

        # A second TEACHER handshake while the first is still registered:
        # refused at the registry (§15) — same 1008, first connection intact.
        second_teacher_ticket = _mint_teacher(api_client, classroom, class_id)["ticket"]
        with api_client.websocket_connect(
            _ws_url(class_id, second_teacher_ticket)
        ) as duplicate:
            _expect_close(duplicate, 1008)
        assert registry_count(class_id) == 1


def test_handshake_rejects_bad_credentials(
    api_client: TestClient, classroom: dict
) -> None:
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    tokens = _login(api_client, classroom["student"])

    for ticket in (
        None,  # no ticket at all
        "garbage-not-a-ticket",
        tokens["access_token"],  # an access token is NOT a ticket (§5)
        tokens["refresh_token"],  # neither is the refresh JWT (§45)
        class_id,  # the class id alone (§41)
    ):
        with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
            _expect_close(websocket, 4001)


# --- ticket semantics (§40) ---------------------------------------------------------------


def test_ticket_is_single_use(api_client: TestClient, classroom: dict) -> None:
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        assert websocket.receive_json()["participant"]["participant_ref"] == student_ref
    _wait_gone(class_id, student_ref)

    # The very same ticket: consumed by the first handshake (§10).
    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        _expect_close(websocket, 4001)


def test_expired_ticket_refused_4001(api_client: TestClient, classroom: dict) -> None:
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]
    _expire_ticket(classroom["engine"], ticket)

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        _expect_close(websocket, 4001)


def test_ticket_is_class_scoped(api_client: TestClient, classroom: dict) -> None:
    engine = classroom["engine"]
    class_a = _create_class(api_client, classroom, offset=0.0)
    class_b = _create_class(api_client, classroom, offset=2.0)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_a)
    _start_class(api_client, classroom, class_b)
    student_ref = _ref(class_a, classroom["student"]["user_id"])

    ticket_a = _mint_student(api_client, classroom, class_a)["ticket"]

    # Minted for A, presented at B: 4001 — and A's ticket is NOT burned.
    with api_client.websocket_connect(_ws_url(class_b, ticket_a)) as websocket:
        _expect_close(websocket, 4001)

    with api_client.websocket_connect(_ws_url(class_a, ticket_a)) as websocket:
        assert websocket.receive_json()["participant"]["participant_ref"] == student_ref
    _wait_gone(class_a, student_ref)


def test_raw_ticket_is_never_persisted(api_client: TestClient, classroom: dict) -> None:
    from app.models.class_ws_ticket import ClassWsTicket

    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    raw = _mint_teacher(api_client, classroom, class_id)["ticket"]
    digest = hashlib.sha256(raw.encode()).hexdigest()

    session = _session(engine)
    try:
        rows = list(
            session.scalars(
                select(ClassWsTicket).where(
                    ClassWsTicket.class_session_id == uuid.UUID(class_id)
                )
            )
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.token_hash == digest
        assert row.token_hash != raw
        assert len(row.token_hash) == 64
        # No column of any ticket row ever contains the raw value.
        for ticket in rows:
            for value in (ticket.token_hash, str(ticket.id), str(ticket.user_id)):
                assert raw not in value
    finally:
        session.close()


def test_the_raw_ticket_never_reaches_logs_or_audit(
    api_client: TestClient, classroom: dict, caplog
) -> None:
    """§5: only the SHA-256 digest is ever persisted — and only the digest
    may ever be logged or audited. The raw value lives in exactly two
    places, both transient: the minting HTTP response and the one URL of
    the handshake that redeemed it.
    """
    import logging

    from app.models.auth_event import AuthEvent

    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_id = _student_profile_id(engine, classroom["student"]["user_id"])
    student_ref = _ref(class_id, classroom["student"]["user_id"])

    with caplog.at_level(logging.DEBUG):
        raw = _mint_student(api_client, classroom, class_id)["ticket"]
        with api_client.websocket_connect(_ws_url(class_id, raw)) as websocket:
            assert websocket.receive_json()["participant"]["participant_ref"] == student_ref
            # Let the heartbeat land BEFORE the class ends: a probe that
            # races the transition would legitimately answer with an
            # error frame, and this test is about the ticket, not that.
            before = _last_seen(engine, class_id, student_id)
            websocket.send_json({"type": "presence.heartbeat"})
            if before is None:
                _wait_until(
                    lambda: _last_seen(engine, class_id, student_id) is not None,
                    message="heartbeat never advanced last_seen_at",
                )
            else:
                _wait_until(
                    lambda: _last_seen(engine, class_id, student_id) > before,
                    message="heartbeat never advanced last_seen_at",
                )
            _end_class(api_client, classroom, class_id)
            assert websocket.receive_json() == {"type": "class.ended", "class_id": class_id}
            _expect_close(websocket, 1000)

    # Application loggers only: httpx logs the full request URL (which
    # carries the ticket by design) under its own logger name, and that is
    # the HTTP client's own bookkeeping — never a line the app emitted.
    for record in caplog.records:
        if not record.name.startswith("app"):
            continue
        rendered = " ".join(
            str(value)
            for key, value in record.__dict__.items()
            if key not in ("message", "asctime")
        )
        assert raw not in rendered, f"{record.name} logged the raw ticket"

    # And no audit row (any column of any row) ever carries it either.
    session = _session(engine)
    try:
        rows = list(session.scalars(select(AuthEvent)))
        assert rows, "expected audit rows for this session"
        for row in rows:
            for column in AuthEvent.__table__.columns.keys():
                assert raw not in str(getattr(row, column))
    finally:
        session.close()


def test_a_ticket_always_connects_as_the_user_it_was_minted_for(
    api_client: TestClient, classroom: dict
) -> None:
    """§40/§11: the ticket alone decides WHO — an ``Authorization``
    header riding along on the upgrade request is never consulted, so it
    can neither steal nor donate an identity.
    """
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    second = _seed_second_student(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    first_ref = _ref(class_id, classroom["student"]["user_id"])
    second_ref = _ref(class_id, second["user_id"])
    assert first_ref != second_ref

    # Student A's ticket, presented while claiming to be B over HTTP.
    ticket_a = _mint_student(api_client, classroom, class_id)["ticket"]
    with api_client.websocket_connect(
        _ws_url(class_id, ticket_a), headers=second["header"]
    ) as websocket:
        participant = websocket.receive_json()["participant"]
        assert participant["participant_ref"] == first_ref
        assert participant["participant_ref"] != second_ref
    _wait_gone(class_id, first_ref)

    # And the mirror: B's ticket under A's credentials still connects B.
    ticket_b = _mint_student(api_client, classroom, class_id, header=second["header"])[
        "ticket"
    ]
    with api_client.websocket_connect(
        _ws_url(class_id, ticket_b), headers=classroom["student_header"]
    ) as websocket:
        participant = websocket.receive_json()["participant"]
        assert participant["participant_ref"] == second_ref
        assert participant["participant_ref"] != first_ref
    _wait_gone(class_id, second_ref)


def test_one_ticket_has_exactly_one_concurrent_winner(
    api_client: TestClient, classroom: dict
) -> None:
    """§10: redemption is one atomic ``UPDATE ... WHERE ... RETURNING``,
    so two handshakes racing over the same ticket leave exactly one
    holder — never two, never zero.
    """
    import threading

    from app.services import classroom_service

    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    raw = _mint_student(api_client, classroom, class_id)["ticket"]
    holder_id = uuid.UUID(classroom["student"]["user_id"])

    barrier = threading.Barrier(2, timeout=10)
    results: list = []

    def redeem() -> None:
        session = _session(engine)
        try:
            barrier.wait()  # both transactions start from the same instant
            holder = classroom_service.consume_ws_ticket(
                session, uuid.UUID(class_id), raw
            )
            session.commit()
            # list.append is atomic under the GIL: no lock needed here.
            results.append(holder.id if holder is not None else None)
        finally:
            session.close()

    threads = [threading.Thread(target=redeem) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(results) == 2, f"both redeemers must report, got {results!r}"
    winners = [result for result in results if result is not None]
    losers = [result for result in results if result is None]
    assert len(winners) == 1, f"expected one winner, got {results!r}"
    assert len(losers) == 1, f"expected one loser, got {results!r}"
    assert winners[0] == holder_id


# --- presence (§42/§20) --------------------------------------------------------------------


def test_presence_snapshot_broadcast_and_left(
    api_client: TestClient, classroom: dict
) -> None:
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    second = _seed_second_student(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    first_ref = _ref(class_id, classroom["student"]["user_id"])
    second_ref = _ref(class_id, second["user_id"])

    second_ticket = _mint_student(api_client, classroom, class_id, header=second["header"])[
        "ticket"
    ]
    with api_client.websocket_connect(_ws_url(class_id, second_ticket)) as second_ws:
        # Alone: the snapshot is exactly self (§20).
        assert second_ws.receive_json() == {
            "type": "presence.joined",
            "participant": {
                "role": "student",
                "display_name": "Classroom Student",
                "participant_ref": second_ref,
            },
        }

        first_ticket = _mint_student(api_client, classroom, class_id)["ticket"]
        with api_client.websocket_connect(_ws_url(class_id, first_ticket)) as first_ws:
            # Newcomer: others first, self last.
            assert first_ws.receive_json()["participant"]["participant_ref"] == second_ref
            assert first_ws.receive_json()["participant"]["participant_ref"] == first_ref
            # The join broadcast reaches the others — never yourself.
            assert second_ws.receive_json()["participant"]["participant_ref"] == first_ref

        # First student disconnects while the class is live -> presence.left
        # for whoever remains (§36: only while live).
        _wait_gone(class_id, first_ref)
        assert second_ws.receive_json() == {
            "type": "presence.left",
            "participant": {
                "role": "student",
                "display_name": "Classroom Student",
                "participant_ref": first_ref,
            },
        }

        # Reconnect: fresh snapshot, fresh broadcast — one connection each (§15).
        reconnect_ticket = _mint_student(api_client, classroom, class_id)["ticket"]
        with api_client.websocket_connect(_ws_url(class_id, reconnect_ticket)) as first_ws:
            assert first_ws.receive_json()["participant"]["participant_ref"] == second_ref
            assert first_ws.receive_json()["participant"]["participant_ref"] == first_ref
            assert second_ws.receive_json()["participant"]["participant_ref"] == first_ref


def test_presence_events_never_cross_classes(
    api_client: TestClient, classroom: dict
) -> None:
    """§13/§19: routing is per class id — a socket hears only its own.

    Two LIVE classes of the same offering, one student in each. Ending
    the second class must leave the first socket completely silent: its
    very next frame is ITS OWN class end, so neither a presence event
    nor a lifecycle event from the other class can have been delivered.
    """
    class_a = _create_class(api_client, classroom, offset=0.0)
    class_b = _create_class(api_client, classroom, offset=2.0)
    _enroll(api_client, classroom)
    second = _seed_second_student(api_client, classroom)
    _start_class(api_client, classroom, class_a)
    _start_class(api_client, classroom, class_b)

    ref_a = _ref(class_a, classroom["student"]["user_id"])
    ref_b = _ref(class_b, second["user_id"])

    ticket_a = _mint_student(api_client, classroom, class_a)["ticket"]
    ticket_b = _mint_student(api_client, classroom, class_b, header=second["header"])[
        "ticket"
    ]

    with api_client.websocket_connect(_ws_url(class_a, ticket_a)) as ws_a:
        # A alone in class A: snapshot is exactly self.
        assert ws_a.receive_json()["participant"]["participant_ref"] == ref_a

        with api_client.websocket_connect(_ws_url(class_b, ticket_b)) as ws_b:
            # B alone in class B — and A hears nothing of that join.
            assert ws_b.receive_json()["participant"]["participant_ref"] == ref_b

            _end_class(api_client, classroom, class_b)
            assert ws_b.receive_json() == {"type": "class.ended", "class_id": class_b}
            _expect_close(ws_b, 1000)

        # Class B is gone; class A is untouched and still LIVE.
        assert _class_row(classroom["engine"], class_a).status == "live"
        _wait_gone(class_b, ref_b)
        assert registry_count(class_b) == 0
        assert registry_count(class_a) == 1

        # Anything leaked from B would sit in A's queue FIRST.
        _end_class(api_client, classroom, class_a)
        assert ws_a.receive_json() == {"type": "class.ended", "class_id": class_a}
        _expect_close(ws_a, 1000)

    assert _segments(classroom["engine"], class_a)[0].left_at is not None
    assert _segments(classroom["engine"], class_b)[0].left_at is not None


# --- heartbeat (§43) ------------------------------------------------------------------------


def test_student_heartbeat_advances_last_seen(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_id = _student_profile_id(engine, classroom["student"]["user_id"])
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        assert websocket.receive_json()["participant"]["participant_ref"] == student_ref
        before = _last_seen(engine, class_id, student_id)
        time.sleep(0.05)
        websocket.send_json({"type": "presence.heartbeat"})
        if before is None:
            _wait_until(
                lambda: _last_seen(engine, class_id, student_id) is not None,
                message="last_seen_at was never written",
            )
        else:
            _wait_until(
                lambda: _last_seen(engine, class_id, student_id) > before,
                message="last_seen_at did not advance",
            )
        # The heartbeat is a silent control frame: the next frame the
        # client sees must be the class end, never a heartbeat reply.
        _end_class(api_client, classroom, class_id)
        assert websocket.receive_json() == {"type": "class.ended", "class_id": class_id}
        _expect_close(websocket, 1000)


def test_heartbeat_never_writes_a_class_message(
    api_client: TestClient, classroom: dict
) -> None:
    """§43/§32: a heartbeat is a silent control frame.

    It advances ``last_seen_at`` and NOTHING else — no ``ClassMessage``
    row, so the transcript this class leaves behind is empty on BOTH
    read surfaces once the class ends.
    """
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_id = _student_profile_id(engine, classroom["student"]["user_id"])
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        assert websocket.receive_json()["participant"]["participant_ref"] == student_ref
        # One heartbeat at a time, each proven applied before the next is
        # sent: nothing may still be in flight when the class ends (a
        # heartbeat processed after the end would legitimately answer
        # with an error frame instead of silence).
        for _ in range(3):
            before = _last_seen(engine, class_id, student_id)
            websocket.send_json({"type": "presence.heartbeat"})
            if before is None:
                _wait_until(
                    lambda: _last_seen(engine, class_id, student_id) is not None,
                    message="heartbeats never advanced last_seen_at",
                )
            else:
                _wait_until(
                    lambda: _last_seen(engine, class_id, student_id) > before,
                    message="a heartbeat did not advance last_seen_at",
                )
        assert _class_message_count(engine, class_id) == 0

        _end_class(api_client, classroom, class_id)
        assert websocket.receive_json() == {"type": "class.ended", "class_id": class_id}
        _expect_close(websocket, 1000)

    # Still zero after the class ended — heartbeats are not history.
    assert _class_message_count(engine, class_id) == 0

    # The transcript exists, is readable, and is empty (§46 historical).
    student_transcript = api_client.get(
        f"/api/v1/me/classes/{class_id}/transcript",
        headers=classroom["student_header"],
    )
    assert student_transcript.status_code == 200, student_transcript.text
    assert student_transcript.json() == []

    teacher_transcript = api_client.get(
        f"{_teacher_base(classroom, class_id)}/transcript",
        headers=classroom["teacher_header"],
    )
    assert teacher_transcript.status_code == 200, teacher_transcript.text
    assert teacher_transcript.json() == []


def test_stale_connection_terminated_1008(
    api_client: TestClient, classroom: dict, stale_settings
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        assert websocket.receive_json()["participant"]["participant_ref"] == student_ref
        # Send nothing at all: stale_settings makes 0.3 s the limit.
        time.sleep(1.0)
        _expect_close(websocket, 1008)
        _wait_gone(class_id, student_ref)

    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].left_at is not None
    # Closed by the socket's stale watchdog — the class itself is untouched.
    assert _class_row(engine, class_id).status == "live"


# --- control frames (§34) ---------------------------------------------------------------------


def test_invalid_frames_are_refused(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        assert websocket.receive_json()["participant"]["participant_ref"] == student_ref

        # A frame that is not a recognized control frame is refused...
        websocket.send_text("this is not json")
        assert websocket.receive_json() == {
            "type": "error",
            "code": "INVALID_CONTROL_MESSAGE",
        }

        # ...but message.send IS the classroom's own frame (slice 3D):
        # this payload is merely invalid (no client_message_id), so the
        # refusal is the advisory INVALID_MESSAGE — never executed,
        # never persisted (§5).
        websocket.send_json({"type": "message.send", "body": "hello"})
        assert websocket.receive_json() == {
            "type": "error",
            "code": "INVALID_MESSAGE",
        }
        assert _class_message_count(engine, class_id) == 0

        # The refusal was advisory: the socket stays live until the end.
        _end_class(api_client, classroom, class_id)
        assert websocket.receive_json() == {"type": "class.ended", "class_id": class_id}
        _expect_close(websocket, 1000)


# --- revocation (§29) -------------------------------------------------------------------------


def test_enrollment_leave_revokes_the_socket(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    enrollment = _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    teacher_ticket = _mint_teacher(api_client, classroom, class_id)["ticket"]
    student_ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, teacher_ticket)) as teacher_ws:
        assert teacher_ws.receive_json()["participant"]["participant_ref"] == _ref(
            class_id, classroom["teacher"]["user_id"]
        )
        with api_client.websocket_connect(
            _ws_url(class_id, student_ticket)
        ) as student_ws:
            assert student_ws.receive_json()["participant"]["participant_ref"] == _ref(
                class_id, classroom["teacher"]["user_id"]
            )
            assert student_ws.receive_json()["participant"]["participant_ref"] == student_ref
            # The teacher hears that join before anything else can follow it.
            assert teacher_ws.receive_json()["participant"]["participant_ref"] == student_ref

            left = api_client.post(
                f"/api/v1/me/learning-enrollments/{enrollment['enrollment_id']}/leave",
                headers=classroom["student_header"],
            )
            assert left.status_code in (200, 204), left.text

            # The student hears a safe error, then 4003 (§29) ...
            assert student_ws.receive_json() == {
                "type": "error",
                "code": "NOT_AUTHORIZED",
            }
            _expect_close(student_ws, 4003)
            # ... the teacher sees exactly presence.left — nothing else.
            assert teacher_ws.receive_json() == {
                "type": "presence.left",
                "participant": {
                    "role": "student",
                    "display_name": "Classroom Student",
                    "participant_ref": student_ref,
                },
            }

    # The segment was finalized by the socket's cleanup (class still live).
    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].left_at is not None

    # Leaving is the revocation point: no new ticket, SAME 404 as unknown (§29).
    response = api_client.post(
        f"/api/v1/me/classes/{class_id}/ws-ticket",
        headers=classroom["student_header"],
    )
    assert response.status_code == 404
    assert response.json()["detail"] == f"no online class with id {class_id}"


# --- lifecycle (§22/§36/§54) --------------------------------------------------------------------


def test_full_classroom_lifecycle(api_client: TestClient, classroom: dict) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_id = _student_profile_id(engine, classroom["student"]["user_id"])
    teacher_ref = _ref(class_id, classroom["teacher"]["user_id"])
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    teacher_ticket = _mint_teacher(api_client, classroom, class_id)["ticket"]
    student_ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, teacher_ticket)) as teacher_ws:
        # Alone: only self in the snapshot.
        assert teacher_ws.receive_json() == {
            "type": "presence.joined",
            "participant": {
                "role": "teacher",
                "display_name": "Classroom Teacher",
                "participant_ref": teacher_ref,
            },
        }
        with api_client.websocket_connect(
            _ws_url(class_id, student_ticket)
        ) as student_ws:
            # Student snapshot: others first (the teacher), then self.
            assert student_ws.receive_json()["participant"]["participant_ref"] == teacher_ref
            assert student_ws.receive_json()["participant"]["participant_ref"] == student_ref
            # Teacher receives the student's join broadcast.
            assert teacher_ws.receive_json()["participant"]["participant_ref"] == student_ref

            # Heartbeats on both sides: control frames, no replies (§32/§43).
            before_seen = _last_seen(engine, class_id, student_id)
            student_ws.send_json({"type": "presence.heartbeat"})
            teacher_ws.send_json({"type": "presence.heartbeat"})

            def _advanced() -> bool:
                value = _last_seen(engine, class_id, student_id)
                return value is not None and (before_seen is None or value > before_seen)

            _wait_until(_advanced, message="student heartbeat not applied")
            time.sleep(0.2)  # let the teacher probe land while the class is live

            _end_class(api_client, classroom, class_id)

            # §22 order: class.ended, then close 1000 — and NEVER a
            # presence.left at class end (§36): this is the teacher's
            # full frame sequence.
            assert teacher_ws.receive_json() == {"type": "class.ended", "class_id": class_id}
            _expect_close(teacher_ws, 1000)
            assert student_ws.receive_json() == {"type": "class.ended", "class_id": class_id}
            _expect_close(student_ws, 1000)

    # Segments finalized by the transition, at actual_ended_at (§22).
    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].student_id == student_id
    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    assert segments[0].left_at == class_row.actual_ended_at


def test_heartbeat_auto_ends_overdue_class(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_id = _student_profile_id(engine, classroom["student"]["user_id"])
    student_ref = _ref(class_id, classroom["student"]["user_id"])
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        assert websocket.receive_json()["participant"]["participant_ref"] == student_ref

        # The window passes while the socket is live (§36): the student's
        # heartbeat IS the sweep — it ends the class, persists, and the
        # class.ended event closes every socket.
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE online_class_sessions "
                    "SET scheduled_start_at = :start, scheduled_end_at = :end "
                    "WHERE id = :id"
                ),
                {
                    "start": now - timedelta(hours=2),
                    "end": now - timedelta(hours=1),
                    "id": class_id,
                },
            )
        websocket.send_json({"type": "presence.heartbeat"})
        assert websocket.receive_json() == {"type": "class.ended", "class_id": class_id}
        _expect_close(websocket, 1000)

    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    assert class_row.actual_ended_at is not None
    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].left_at == class_row.actual_ended_at


# --- the event bus (§13/§25) ---------------------------------------------------------------------


def test_class_started_reaches_a_raw_notify_listener(
    api_client: TestClient, classroom: dict
) -> None:
    import psycopg

    from app.core.config import get_settings

    class_id = _create_class(api_client, classroom)
    dsn = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    # A third-party LISTEN consumer on the app's channel — proves §25:
    # class.started fires as a real PostgreSQL NOTIFY on commit.
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("LISTEN realtime_events")
        _start_class(api_client, classroom, class_id)
        received = list(connection.notifies(timeout=5.0, stop_after=1))

    assert received, "no NOTIFY arrived for class.started"
    payload = json.loads(received[0].payload)
    assert payload["type"] == "class.started"
    assert payload["class_id"] == class_id
