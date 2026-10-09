"""Slice 3D integration tests (PostgreSQL, guarded test DB).

End-to-end contract for the durable classroom message surface against
``super_teacher_db_test``:

- **recovery routes**: the two ``/messages`` cursors are role-scoped
  (401 anonymous, 403 wrong role from the guard), answer 409 for a class
  that never happened, keep the L6 rule (unknown ids, foreign teachers
  and missing enrollments all answer the SAME 404), page by the
  EXCLUSIVE ``after_sequence`` cursor with a bounded ``limit``, and expose
  presentation-safe records only — no email address, no retry key;
- **the live classroom**: one ``message.send`` reaches EVERY socket of
  the class exactly once (the sender's echo included), with the §19 wire
  shape (no ``class_id`` routing key, no user id), while invalid frames
  are merely advisory, an idempotent retry is acknowledged ONLY to the
  requester, a conflicting retry is refused without rewriting history,
  and the quota refuses the overflow without touching anyone else's
  turn or closing the socket;
- **lifecycle races**: a send after the class ended is refused and
  closes 4008, a send from a de-authorized account is refused and closes
  4003 (the room hears ``presence.left``), and messages never cross
  class boundaries;
- **recoverability**: a message committed while a participant was
  disconnected is retrievable through the cursor read (no permanent
  gap), and the publish rides the caller's transaction — no COMMIT, no
  NOTIFY, no broadcast (§11);
- **PostgreSQL concurrency**: parallel senders allocate CONTIGUOUS
  sequences through the class-row lock, concurrent identical retries
  yield exactly one row, parallel classes keep independent sequences,
  and a rolled-back send consumes no sequence.

Scaffolding mirrors ``test_classroom_websocket.py`` (one module-scoped
app client so every socket shares ONE portal event loop) but every test
here is self-contained. Like that suite it runs under ``-m ""``.
"""
from __future__ import annotations

import json
import logging
import threading
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
    with TestClient(app) as client:  # one portal event loop for the module
        yield client
    app.dependency_overrides.clear()


def _session(engine):
    from app.core.database import SessionLocal

    return SessionLocal(bind=engine)


def _seed_catalog(engine) -> dict:
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
        "catalog": catalog,
    }


@pytest.fixture(autouse=True)
def _registry_idle():
    """No live connection may leak between tests (teardown is asynchronous)."""
    from app.realtime.connections import registry

    _wait_until(lambda: len(registry) == 0, message="registry busy before test")
    yield
    _wait_until(lambda: len(registry) == 0, message="registry busy after test")


@pytest.fixture(autouse=True)
def _fresh_quota():
    """Every test starts with a full message quota (the limiter is global)."""
    from app.services.message_rate_limiter import message_rate_limiter

    message_rate_limiter.reset()
    yield
    message_rate_limiter.reset()


# --- classroom helpers ----------------------------------------------------------------


def _teacher_base(classroom: dict, class_id: str) -> str:
    return f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}"


def _student_messages_url(class_id: str) -> str:
    return f"/api/v1/me/classes/{class_id}/messages"


def _teacher_messages_url(classroom: dict, class_id: str) -> str:
    return f"{_teacher_base(classroom, class_id)}/messages"


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


# --- ticket / websocket helpers ---------------------------------------------------------


def _mint_teacher(api_client: TestClient, classroom: dict, class_id: str) -> dict:
    response = api_client.post(
        f"{_teacher_base(classroom, class_id)}/ws-ticket",
        headers=classroom["teacher_header"],
    )
    assert response.status_code == 201, response.text
    assert response.json()["ws_path"] == f"/ws/classes/{class_id}"
    return response.json()


def _mint_student(
    api_client: TestClient, classroom: dict, class_id: str, header: dict | None = None
) -> dict:
    response = api_client.post(
        f"/api/v1/me/classes/{class_id}/ws-ticket",
        headers=header or classroom["student_header"],
    )
    assert response.status_code == 201, response.text
    assert response.json()["ws_path"] == f"/ws/classes/{class_id}"
    return response.json()


def _ws_url(class_id: str, ticket: str) -> str:
    return f"/ws/classes/{class_id}?ticket={ticket}"


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
    from app.realtime.connections import registry

    def _gone() -> bool:
        return not any(
            connection.participant_ref == ref
            for connection in registry.connections_for_class(uuid.UUID(class_id))
        )

    _wait_until(_gone, message=f"connection {ref} still registered")


# --- database helpers ------------------------------------------------------------------


def _messages(engine, class_id: str) -> list:
    from app.models.class_message import ClassMessage

    session = _session(engine)
    try:
        rows = list(
            session.scalars(
                select(ClassMessage)
                .where(ClassMessage.class_session_id == uuid.UUID(class_id))
                .order_by(ClassMessage.sequence)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


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


def _send_direct(engine, class_id: str, user_id: str, client_message_id: str, body: str):
    """Commit one message through the REAL service (no socket needed)."""
    from app.models.user import User
    from app.services import classroom_service

    session = _session(engine)
    try:
        user = session.get(User, uuid.UUID(user_id))
        outcome, event = classroom_service.send_class_message(
            session, user, uuid.UUID(class_id), client_message_id, body
        )
        session.commit()
        return outcome, event
    finally:
        session.close()


def _drive(engine, class_id: str, user_id: str, jobs: list[tuple[str, str]]) -> list[str]:
    """Run one thread's (client_message_id, body) jobs in its OWN session."""
    from app.models.user import User
    from app.services import classroom_service

    session = _session(engine)
    try:
        user = session.get(User, uuid.UUID(user_id))
        outcomes = []
        for client_message_id, body in jobs:
            outcome, _ = classroom_service.send_class_message(
                session, user, uuid.UUID(class_id), client_message_id, body
            )
            session.commit()
            outcomes.append(outcome)
        return outcomes
    finally:
        session.close()


def _parallel(engine, assignments: list[tuple[str, str, list[tuple[str, str]]]]) -> list[str]:
    """Threads, one session each, released together against a barrier."""
    barrier = threading.Barrier(len(assignments))
    results: dict[int, list[str]] = {}
    failures: dict[int, BaseException] = {}

    def run(index: int, class_id: str, user_id: str, jobs: list[tuple[str, str]]) -> None:
        barrier.wait()
        try:
            results[index] = _drive(engine, class_id, user_id, jobs)
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            failures[index] = exc

    threads = [
        threading.Thread(target=run, args=(index, class_id, user_id, jobs))
        for index, (class_id, user_id, jobs) in enumerate(assignments)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    if failures:
        raise AssertionError(f"worker failed: {failures!r}")
    return [outcome for index in sorted(results) for outcome in results[index]]


# --- recovery routes (HTTP) -------------------------------------------------------------


def test_recovery_routes_are_role_scoped_and_cursored(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)  # SCHEDULED
    _enroll(api_client, classroom)
    student_url = _student_messages_url(class_id)
    teacher_url = _teacher_messages_url(classroom, class_id)
    student_h = classroom["student_header"]
    teacher_h = classroom["teacher_header"]

    # Role split from the guard, before the handler: 401 anonymous,
    # 403 for the wrong role on either route.
    assert api_client.get(student_url).status_code == 401
    assert api_client.get(teacher_url).status_code == 401
    assert api_client.get(teacher_url, headers=student_h).status_code == 403
    assert api_client.get(student_url, headers=teacher_h).status_code == 403

    # A class that never happened has no classroom history (409).
    assert api_client.get(student_url, headers=student_h).status_code == 409
    assert api_client.get(teacher_url, headers=teacher_h).status_code == 409

    _start_class(api_client, classroom, class_id)
    # LIVE: both cursors answer — this read exists for reconnects (§14).
    assert api_client.get(student_url, headers=student_h).json() == []
    assert api_client.get(teacher_url, headers=teacher_h).json() == []

    _send_direct(engine, class_id, classroom["student"]["user_id"], "cid-1", "one")
    _send_direct(engine, class_id, classroom["teacher"]["user_id"], "cid-2", "two")
    _send_direct(engine, class_id, classroom["student"]["user_id"], "cid-3", "three")

    page = api_client.get(student_url, headers=student_h).json()
    assert [m["sequence"] for m in page] == [1, 2, 3]
    assert [m["body"] for m in page] == ["one", "two", "three"]
    assert page[1]["sender"] == {"role": "teacher", "display_name": "Classroom Teacher"}

    # Exactly the presentation-safe record — no retry key, no address,
    # no user id, no routing key.
    assert set(page[0]) == {"message_id", "sequence", "sender", "body", "sent_at"}
    assert set(page[0]["sender"]) == {"role", "display_name"}
    dump = json.dumps(page)
    assert "@test.example" not in dump
    assert "client_message_id" not in dump
    assert "class_id" not in dump
    assert "user_id" not in dump

    # EXCLUSIVE cursor, bounded page, sequence ASC — identical on both routes.
    for url in (student_url, teacher_url):
        headers = student_h if url == student_url else teacher_h
        assert [m["sequence"] for m in api_client.get(
            f"{url}?after_sequence=1&limit=1", headers=headers
        ).json()] == [2]
        assert api_client.get(f"{url}?after_sequence=3", headers=headers).json() == []
        assert api_client.get(f"{url}?after_sequence=-1", headers=headers).status_code == 422
        assert api_client.get(f"{url}?limit=0", headers=headers).status_code == 422
        assert api_client.get(f"{url}?limit=201", headers=headers).status_code == 422

    # L6: an unenrolled student's class id confirms NOTHING — the SAME
    # 404 as an id that never existed (never 403, never 200).
    outsider = _seed_account(engine, role="student")
    outsider_h = _header(_login(api_client, outsider))
    known = api_client.get(student_url, headers=outsider_h)
    unknown = api_client.get(f"/api/v1/me/classes/{uuid.uuid4()}/messages", headers=outsider_h)
    assert known.status_code == unknown.status_code == 404

    # L6: another teacher gets the SAME 404 as an unknown class id.
    other_teacher = _seed_account(engine, role="teacher")
    other_h = _header(_login(api_client, other_teacher))
    foreign = api_client.get(teacher_url, headers=other_h)
    missing = api_client.get(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/"
        f"{uuid.uuid4()}/messages",
        headers=other_h,
    )
    assert foreign.status_code == missing.status_code == 404

    # ENDED: the owner still reads (3B policy unchanged for students —
    # enrollment alone never grants the historical read).
    _end_class(api_client, classroom, class_id)
    ended_page = api_client.get(teacher_url, headers=teacher_h).json()
    assert [m["sequence"] for m in ended_page] == [1, 2, 3]
    assert api_client.get(student_url, headers=student_h).status_code == 404


def test_participation_preserves_the_student_cursor_after_the_end(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    never_joined = _seed_second_student(api_client, classroom)  # enrolled, absent
    _start_class(api_client, classroom, class_id)

    # The student actually attends (a durable segment), then leaves.
    with api_client.websocket_connect(
        _ws_url(class_id, _mint_student(api_client, classroom, class_id)["ticket"])
    ) as websocket:
        websocket.receive_json()  # snapshot
    _wait_gone(class_id, _ref(class_id, classroom["student"]["user_id"]))

    _send_direct(engine, class_id, classroom["student"]["user_id"], "cid-1", "said live")
    _end_class(api_client, classroom, class_id)

    # Participation — not current enrollment — preserves the read (§14).
    joined = api_client.get(
        _student_messages_url(class_id), headers=classroom["student_header"]
    )
    assert joined.status_code == 200, joined.text
    assert [m["body"] for m in joined.json()] == ["said live"]

    # ...while the enrolled classmate who was never inside gets the SAME
    # 404 as an unknown id: enrollment alone never grants history.
    absent = api_client.get(
        _student_messages_url(class_id), headers=never_joined["header"]
    )
    assert absent.status_code == 404


def test_a_scheduled_class_exposes_no_cursor(
    api_client: TestClient, classroom: dict
) -> None:
    """SCHEDULED: no messages, and the cursor answers 409 on both routes."""
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    assert (
        api_client.get(
            _student_messages_url(class_id), headers=classroom["student_header"]
        ).status_code
        == 409
    )
    assert (
        api_client.get(
            _teacher_messages_url(classroom, class_id), headers=classroom["teacher_header"]
        ).status_code
        == 409
    )
    assert _class_message_count(classroom["engine"], class_id) == 0


# --- the live classroom (send + fan-out) -------------------------------------------------


def test_a_send_reaches_every_socket_exactly_once_with_a_safe_payload(
    api_client: TestClient, classroom: dict, caplog
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    second = _seed_second_student(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    with api_client.websocket_connect(
        _ws_url(class_id, _mint_teacher(api_client, classroom, class_id)["ticket"])
    ) as teacher_ws, api_client.websocket_connect(
        _ws_url(class_id, _mint_student(api_client, classroom, class_id)["ticket"])
    ) as student_ws, api_client.websocket_connect(
        _ws_url(class_id, _mint_student(api_client, classroom, class_id, header=second["header"])["ticket"])
    ) as second_ws:
        # Presence housekeeping first: snapshots and join broadcasts.
        teacher_ws.receive_json()  # self
        student_ws.receive_json()  # others..., self
        student_ws.receive_json()
        teacher_ws.receive_json()  # student join
        second_ws.receive_json()  # others..., self
        second_ws.receive_json()
        second_ws.receive_json()
        teacher_ws.receive_json()  # second join
        student_ws.receive_json()  # second join

        caplog.set_level(logging.DEBUG)
        student_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-1", "body": "alpha-body"}
        )
        frames = [student_ws.receive_json(), teacher_ws.receive_json(), second_ws.receive_json()]
        assert frames[0] == frames[1] == frames[2]  # one identical frame per socket
        created = frames[0]
        assert set(created) == {
            "type",
            "message_id",
            "sequence",
            "sender",
            "body",
            "sent_at",
            "client_message_id",
        }
        assert created["type"] == "message.created"
        assert created["sequence"] == 1
        assert created["body"] == "alpha-body"
        assert created["client_message_id"] == "cid-1"
        assert created["sender"] == {"role": "student", "display_name": "Classroom Student"}
        assert "class_id" not in created  # routing keys never reach the wire (§19)
        assert "@test.example" not in json.dumps(created)

        # The teacher speaks: the same shape, role labeled, sequence 2.
        teacher_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-2", "body": "beta-body"}
        )
        for socket in (student_ws, teacher_ws, second_ws):
            frame = socket.receive_json()
            assert frame["sequence"] == 2
            assert frame["sender"]["role"] == "teacher"
            assert frame["body"] == "beta-body"

    rows = _messages(engine, class_id)
    assert [r.sequence for r in rows] == [1, 2]
    assert rows[0].client_message_id == "cid-1"

    # §20/§45: bodies are content, never log lines.
    assert "alpha-body" not in caplog.text
    assert "beta-body" not in caplog.text


def test_an_invalid_send_is_advisory_and_persists_nothing(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        websocket.receive_json()  # snapshot
        # Missing retry key → INVALID_MESSAGE, advisory, nothing written.
        websocket.send_json({"type": "message.send", "body": "hi"})
        assert websocket.receive_json() == {"type": "error", "code": "INVALID_MESSAGE"}
        # Blank / oversized / wrong-typed bodies are refused the same way.
        for body in ("   ", 123, "\n"):
            websocket.send_json(
                {"type": "message.send", "client_message_id": "cid-x", "body": body}
            )
            assert websocket.receive_json() == {"type": "error", "code": "INVALID_MESSAGE"}
        assert _class_message_count(engine, class_id) == 0

        # The socket survives every advisory refusal (§18).
        websocket.send_json(
            {"type": "message.send", "client_message_id": "cid-ok", "body": "still here"}
        )
        assert websocket.receive_json()["sequence"] == 1

    assert _class_message_count(engine, class_id) == 1


def test_a_retry_is_acknowledged_only_to_the_requester(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    with api_client.websocket_connect(
        _ws_url(class_id, _mint_teacher(api_client, classroom, class_id)["ticket"])
    ) as teacher_ws, api_client.websocket_connect(
        _ws_url(class_id, _mint_student(api_client, classroom, class_id)["ticket"])
    ) as student_ws:
        teacher_ws.receive_json()
        student_ws.receive_json()
        student_ws.receive_json()
        teacher_ws.receive_json()

        payload = {"type": "message.send", "client_message_id": "cid-1", "body": "one"}
        student_ws.send_json(payload)
        assert student_ws.receive_json()["sequence"] == 1
        assert teacher_ws.receive_json()["sequence"] == 1

        # The identical retry: the canonical message comes back to the
        # REQUESTER only — no second room-wide broadcast (§8).
        student_ws.send_json(payload)
        ack = student_ws.receive_json()
        assert ack["sequence"] == 1
        assert ack["body"] == "one"

        # Prove the teacher heard NOTHING from the retry: its very next
        # frame is the NEW message (sequence 2) — a broadcast retry of
        # sequence 1 would sit in front of it.
        student_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-2", "body": "two"}
        )
        assert teacher_ws.receive_json()["sequence"] == 2
        assert student_ws.receive_json()["sequence"] == 2

    rows = _messages(engine, class_id)
    assert [(r.client_message_id, r.sequence) for r in rows] == [("cid-1", 1), ("cid-2", 2)]


def test_a_conflicting_retry_is_refused_and_rewrites_nothing(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    with api_client.websocket_connect(
        _ws_url(class_id, _mint_teacher(api_client, classroom, class_id)["ticket"])
    ) as teacher_ws, api_client.websocket_connect(
        _ws_url(class_id, _mint_student(api_client, classroom, class_id)["ticket"])
    ) as student_ws:
        teacher_ws.receive_json()
        student_ws.receive_json()
        student_ws.receive_json()
        teacher_ws.receive_json()

        student_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-1", "body": "the original"}
        )
        assert student_ws.receive_json()["sequence"] == 1
        assert teacher_ws.receive_json()["sequence"] == 1

        # Same key, different text: refuse (advisory), never replace (§8).
        student_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-1", "body": "a rewrite"}
        )
        assert student_ws.receive_json() == {
            "type": "error",
            "code": "CLIENT_MESSAGE_ID_CONFLICT",
        }

        # Still open: a fresh key continues the class at sequence 2.
        student_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-2", "body": "next"}
        )
        assert student_ws.receive_json()["sequence"] == 2
        assert teacher_ws.receive_json()["sequence"] == 2

    rows = _messages(engine, class_id)
    assert [(r.client_message_id, r.body, r.sequence) for r in rows] == [
        ("cid-1", "the original", 1),
        ("cid-2", "next", 2),
    ]


def test_the_quota_flood_is_refused_advisory_and_scoped(
    api_client: TestClient, classroom: dict
) -> None:
    from app.core.config import get_settings

    engine = classroom["engine"]
    limit = get_settings().WS_MESSAGE_RATE_LIMIT
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    with api_client.websocket_connect(
        _ws_url(class_id, _mint_teacher(api_client, classroom, class_id)["ticket"])
    ) as teacher_ws, api_client.websocket_connect(
        _ws_url(class_id, _mint_student(api_client, classroom, class_id)["ticket"])
    ) as student_ws:
        teacher_ws.receive_json()
        student_ws.receive_json()
        student_ws.receive_json()
        teacher_ws.receive_json()

        for index in range(limit):
            student_ws.send_json(
                {"type": "message.send", "client_message_id": f"cid-{index}", "body": f"m{index}"}
            )
            assert student_ws.receive_json()["sequence"] == index + 1
            assert teacher_ws.receive_json()["sequence"] == index + 1

        # The overflow is refused — advisory, socket stays open (§41).
        student_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-overflow", "body": "too much"}
        )
        assert student_ws.receive_json() == {"type": "error", "code": "RATE_LIMITED"}

        # The quota is THIS participant's: the teacher still speaks and
        # everyone hears it as the next sequence.
        teacher_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-t", "body": "teacher turn"}
        )
        assert teacher_ws.receive_json()["sequence"] == limit + 1
        assert student_ws.receive_json()["sequence"] == limit + 1

    assert _class_message_count(engine, class_id) == limit + 1
    assert _messages(engine, class_id)[0].body == "m0"  # the flood was persisted


def test_a_send_after_the_class_ended_is_refused_and_closes_4008(
    api_client: TestClient, classroom: dict
) -> None:
    """§17 end-vs-send race: the transition always wins, no row enters."""
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        websocket.receive_json()  # snapshot

        # The class flips to ENDED without an event reaching this socket:
        # exactly the race a real end can create under load.
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE online_class_sessions SET status = 'ended' WHERE id = :id"),
                {"id": class_id},
            )

        websocket.send_json(
            {"type": "message.send", "client_message_id": "cid-1", "body": "one last thing"}
        )
        assert websocket.receive_json() == {"type": "error", "code": "CLASS_NOT_LIVE"}
        _expect_close(websocket, 4008)

    assert _class_message_count(engine, class_id) == 0


def test_a_send_without_authorization_closes_4003(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    with api_client.websocket_connect(
        _ws_url(class_id, _mint_teacher(api_client, classroom, class_id)["ticket"])
    ) as teacher_ws, api_client.websocket_connect(
        _ws_url(class_id, _mint_student(api_client, classroom, class_id)["ticket"])
    ) as student_ws:
        teacher_ws.receive_json()
        student_ws.receive_json()
        student_ws.receive_json()
        teacher_ws.receive_json()

        # The account is deactivated behind the socket's back: the ticket
        # was minted earlier and must not become permanent authorization.
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET status = 'suspended' WHERE id = :id"),
                {"id": classroom["student"]["user_id"]},
            )

        student_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-1", "body": "am i still in?"}
        )
        assert student_ws.receive_json() == {"type": "error", "code": "NOT_AUTHORIZED"}
        _expect_close(student_ws, 4003)
        # The room learns the participant left, nothing more (§29).
        assert teacher_ws.receive_json()["type"] == "presence.left"

    assert _class_message_count(engine, class_id) == 0


def test_messages_never_cross_class_boundaries(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_a = _create_class(api_client, classroom, offset=0.0)
    class_b = _create_class(api_client, classroom, offset=2.0)
    _enroll(api_client, classroom)
    second = _seed_second_student(api_client, classroom)
    _start_class(api_client, classroom, class_a)
    _start_class(api_client, classroom, class_b)

    with api_client.websocket_connect(
        _ws_url(class_a, _mint_student(api_client, classroom, class_a)["ticket"])
    ) as ws_a, api_client.websocket_connect(
        _ws_url(class_b, _mint_student(api_client, classroom, class_b, header=second["header"])["ticket"])
    ) as ws_b:
        ws_a.receive_json()
        ws_b.receive_json()

        # A message in class B reaches only class B...
        ws_b.send_json(
            {"type": "message.send", "client_message_id": "cid-1", "body": "class b only"}
        )
        assert ws_b.receive_json()["sequence"] == 1

        # ...and nothing leaked into A: A's very next frame is its own
        # class end, so no foreign message could be queued ahead of it.
        _end_class(api_client, classroom, class_a)
        assert ws_a.receive_json() == {"type": "class.ended", "class_id": class_a}
        _expect_close(ws_a, 1000)

    assert _class_message_count(engine, class_a) == 0
    assert _class_message_count(engine, class_b) == 1


def test_a_missed_message_is_retrievable_through_the_cursor(
    api_client: TestClient, classroom: dict
) -> None:
    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    with api_client.websocket_connect(
        _ws_url(class_id, _mint_teacher(api_client, classroom, class_id)["ticket"])
    ) as teacher_ws:
        teacher_ws.receive_json()  # snapshot (alone)

        with api_client.websocket_connect(
            _ws_url(class_id, _mint_student(api_client, classroom, class_id)["ticket"])
        ) as student_ws:
            student_ws.receive_json()  # others..., self
            student_ws.receive_json()
            teacher_ws.receive_json()  # student join
        # The student's socket is gone while the class keeps running...
        _wait_gone(class_id, _ref(class_id, classroom["student"]["user_id"]))
        # ...and the teacher is told, exactly once (§36).
        assert teacher_ws.receive_json()["type"] == "presence.left"

        # ...and the teacher sends into that gap.
        teacher_ws.send_json(
            {"type": "message.send", "client_message_id": "cid-1", "body": "missed while away"}
        )
        assert teacher_ws.receive_json()["sequence"] == 1

        # Recovery: the reconnecting student fetches the gap with the
        # exclusive cursor — nothing is permanently lost (§14).
        gap = api_client.get(
            _student_messages_url(class_id), headers=classroom["student_header"]
        )
        assert [m["body"] for m in gap.json()] == ["missed while away"]
        assert gap.json()[0]["sequence"] == 1

        # A fresh socket does NOT replay history: its next message frame
        # is the live one (sequence 2), proving subscribe-then-cursor.
        reconnect_ticket = _mint_student(api_client, classroom, class_id)["ticket"]
        with api_client.websocket_connect(_ws_url(class_id, reconnect_ticket)) as again:
            again.receive_json()  # others (teacher), self
            again.receive_json()
            teacher_ws.receive_json()  # reconnect join

            teacher_ws.send_json(
                {"type": "message.send", "client_message_id": "cid-2", "body": "live again"}
            )
            assert again.receive_json()["sequence"] == 2
            assert teacher_ws.receive_json()["sequence"] == 2

    assert _class_message_count(engine, class_id) == 2


# --- transaction coupling (§11) -----------------------------------------------------------


def test_a_send_without_commit_publishes_nothing(
    api_client: TestClient, classroom: dict
) -> None:
    import psycopg

    from app.core.config import get_settings
    from app.models.user import User
    from app.services import classroom_service

    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    dsn = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    session = _session(engine)
    try:
        user = session.get(User, uuid.UUID(classroom["student"]["user_id"]))
        with psycopg.connect(dsn, autocommit=True) as listener:
            listener.execute("LISTEN realtime_events")

            # The row is prepared but NOT durable: no NOTIFY may exist.
            classroom_service.send_class_message(
                session, user, uuid.UUID(class_id), "cid-1", "not committed yet"
            )
            assert list(listener.notifies(timeout=0.7, stop_after=1)) == []

            session.commit()  # ...and now it fires with the COMMIT.
            received = list(listener.notifies(timeout=5.0, stop_after=1))
    finally:
        session.close()

    assert received, "no NOTIFY arrived for message.created"
    payload = json.loads(received[0].payload)
    assert payload["type"] == "message.created"
    assert payload["class_id"] == class_id
    assert payload["sequence"] == 1
    assert payload["body"] == "not committed yet"
    assert _class_message_count(engine, class_id) == 1


# --- PostgreSQL concurrency -----------------------------------------------------------------


def test_concurrent_senders_allocate_contiguous_sequences(
    api_client: TestClient, classroom: dict, monkeypatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "WS_MESSAGE_RATE_LIMIT", 1000)

    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_id = classroom["student"]["user_id"]

    # 6 threads x 5 messages: under the class-row lock the sequences
    # must come out exactly 1..30 — no gaps, no duplicates, no reuse.
    assignments = [
        (
            class_id,
            student_id,
            [(f"t{index}-m{offset}", f"thread {index} message {offset}") for offset in range(5)],
        )
        for index in range(6)
    ]
    outcomes = _parallel(engine, assignments)
    assert outcomes == ["created"] * 30

    sequences = [row.sequence for row in _messages(engine, class_id)]
    assert sequences == list(range(1, 31))


def test_concurrent_identical_retries_yield_exactly_one_row(
    api_client: TestClient, classroom: dict, monkeypatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "WS_MESSAGE_RATE_LIMIT", 1000)

    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    student_id = classroom["student"]["user_id"]

    # Every thread submits the SAME key and body, once, simultaneously.
    assignments = [(class_id, student_id, [("same-key", "the one and only body")]) for _ in range(8)]
    outcomes = _parallel(engine, assignments)

    # The class-row lock serializes the retries: one writer, seven
    # canonical acknowledgements — never eight rows.
    assert outcomes.count("created") == 1
    assert outcomes.count("duplicate") == 7

    rows = _messages(engine, class_id)
    assert len(rows) == 1
    assert rows[0].sequence == 1
    assert rows[0].client_message_id == "same-key"


def test_parallel_classes_keep_independent_sequences(
    api_client: TestClient, classroom: dict, monkeypatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "WS_MESSAGE_RATE_LIMIT", 1000)

    engine = classroom["engine"]
    class_a = _create_class(api_client, classroom, offset=0.0)
    class_b = _create_class(api_client, classroom, offset=2.0)
    _enroll(api_client, classroom)
    second = _seed_second_student(api_client, classroom)
    _start_class(api_client, classroom, class_a)
    _start_class(api_client, classroom, class_b)

    assignments = []
    for class_id, user_id, tag in (
        (class_a, classroom["student"]["user_id"], "a"),
        (class_b, second["user_id"], "b"),
    ):
        assignments.append(
            (
                class_id,
                user_id,
                [(f"cid-{tag}-{offset}", f"{tag}{offset}") for offset in range(10)],
            )
        )
    outcomes = _parallel(engine, assignments)
    assert outcomes == ["created"] * 20

    # Each class numbers from 1 on its own — sequences are PER CLASS.
    assert [row.sequence for row in _messages(engine, class_a)] == list(range(1, 11))
    assert [row.sequence for row in _messages(engine, class_b)] == list(range(1, 11))


def test_a_rolled_back_send_consumes_no_sequence(
    api_client: TestClient, classroom: dict
) -> None:
    from app.models.user import User
    from app.services import classroom_service

    engine = classroom["engine"]
    class_id = _create_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    session = _session(engine)
    try:
        user = session.get(User, uuid.UUID(classroom["student"]["user_id"]))
        outcome, event = classroom_service.send_class_message(
            session, user, uuid.UUID(class_id), "cid-1", "will be rolled back"
        )
        assert outcome == "created"
        assert event["sequence"] == 1
        session.rollback()  # the transaction the caller prepared dies
    finally:
        session.close()

    assert _class_message_count(engine, class_id) == 0

    # Nothing was consumed: the next committed send starts again at 1.
    outcome, event = _send_direct(
        engine, class_id, classroom["student"]["user_id"], "cid-2", "the real one"
    )
    assert outcome == "created"
    assert event["sequence"] == 1
    rows = _messages(engine, class_id)
    assert [(r.sequence, r.body) for r in rows] == [(1, "the real one")]
