"""Classroom lifecycle recovery integration tests (PostgreSQL, slice 3E).

The final reliability pass, end to end against ``super_teacher_db_test``:

- **startup recovery**: a class left ``live`` by a crash or redeploy is
  ended by the sweeper's immediate first pass — no client request needed;
- **the background sweep**: with the real thread running, an overdue live
  class ends within one interval without any participant sending a frame,
  and the recorded duration stops at ``scheduled_end_at`` however late
  the sweep runs;
- **multi-worker safety**: concurrent sweeps (separate sessions, as two
  workers would run them) of the same class produce ONE transition, ONE
  audit row and ONE ``class.ended`` NOTIFY — PostgreSQL's row lock is the
  guarantee, not this process's memory;
- **manual End vs sweep**: whichever wins, the loser is a 409 / a no-op —
  never a second transition, never a rewritten ``actual_ended_at``;
- **attendance finalization**: open segments close at the authoritative
  end moment inside the same transaction, and a join after the automatic
  end is refused with attendance left exactly as finalized;
- **propagation**: every socket of the class hears ``class.ended`` exactly
  once and closes 1000; a heartbeat racing the sweep still leaves the
  class ended exactly once.

The app client is ENTERED (``with TestClient``) so HTTP and every
WebSocket share ONE portal event loop, exactly like
``test_classroom_websocket.py``. The background sweeper is stopped around
every test by the shared autouse fixture; the tests that need the thread
start it deliberately with a shrunken interval.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


# --- scaffolding (mirrors test_classroom_websocket.py) --------------------------------------


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
    with TestClient(app) as client:
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
        level = EducationLevel(
            code=f"L-{uuid.uuid4().hex[:6]}", name="Senior 3", level_number=3
        )
        session.add_all([year, pathway, level])
        session.flush()
        session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

        program = Program(
            code=f"C-{uuid.uuid4().hex[:6]}",
            name="Combination",
            program_type="combination",
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
                    full_name="Lifecycle Teacher",
                    subject="Mathematics",
                    verification_status="approved",
                )
            )
        elif role == "student":
            session.add(Student(user_id=account.id, full_name="Lifecycle Student"))
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


@pytest.fixture()
def classroom(clean_db, api_client: TestClient):
    """Seeded catalog + logged-in teacher/student + one owned offering."""
    from app.models.user import User
    from app.schemas.learning import TeachingOfferingCreate
    from tests.conftest import approve_teacher_for_payloads

    engine = clean_db
    catalog = _seed_catalog(engine)
    teacher = _seed_account(engine, role="teacher")
    student = _seed_account(engine, role="student")
    teacher_header = _header(_login(api_client, teacher))
    student_header = _header(_login(api_client, student))

    session = _session(engine)
    try:
        teacher_user = session.get(User, uuid.UUID(teacher["user_id"]))
        approve_teacher_for_payloads(
            session, teacher_user, [TeachingOfferingCreate(**catalog)]
        )
        session.commit()
    finally:
        session.close()

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


@pytest.fixture()
def fast_sweeper():
    """The real background sweeper thread, with a 0.2 s interval."""
    from app.core.config import get_settings
    from app.services.class_end_sweeper import class_end_sweeper

    settings = get_settings()
    original = settings.CLASS_END_SWEEP_INTERVAL_SECONDS
    settings.CLASS_END_SWEEP_INTERVAL_SECONDS = 0.2
    class_end_sweeper.start()
    try:
        yield class_end_sweeper
    finally:
        class_end_sweeper.stop()
        settings.CLASS_END_SWEEP_INTERVAL_SECONDS = original


# --- classroom helpers ----------------------------------------------------------------------


def _teacher_base(classroom: dict, class_id: str) -> str:
    return f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}"


def _create_short_lived_class(
    api_client: TestClient, classroom: dict, *, grace_seconds: float = 2.5
) -> tuple[str, datetime]:
    """A class whose scheduled window closes moments from now.

    The honest overdue shape: participants join INSIDE the window and the
    timetable passes on its own. Rewriting the window underneath an
    already-open segment is the impossible case the service floors (an
    end can never precede a ``joined_at`` it closes), so tests exercise
    the production path, not that guard.
    """
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    end = datetime.now(timezone.utc) + timedelta(seconds=grace_seconds)
    created = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json={
            "scheduled_start_at": start.isoformat(),
            "scheduled_end_at": end.isoformat(),
        },
        headers=classroom["teacher_header"],
    )
    assert created.status_code == 201, created.text
    return created.json()["class_id"], end


def _wait_for_the_window_to_pass(scheduled_end: datetime) -> None:
    """Sleep until the class's own timetable has closed (no rewriting)."""
    _wait_until(
        lambda: datetime.now(timezone.utc) > scheduled_end,
        timeout=10.0,
        message="the scheduled window never closed",
    )


def _start_class(api_client: TestClient, classroom: dict, class_id: str) -> None:
    started = api_client.post(
        f"{_teacher_base(classroom, class_id)}/start",
        headers=classroom["teacher_header"],
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "live"


def _enroll(api_client: TestClient, classroom: dict, header: dict | None = None) -> None:
    enrolled = api_client.post(
        f"/api/v1/marketplace/offerings/{classroom['offering_id']}/enroll",
        headers=header or classroom["student_header"],
    )
    assert enrolled.status_code == 201, enrolled.text


def _seed_second_student(api_client: TestClient, classroom: dict) -> dict:
    account = _seed_account(classroom["engine"], role="student")
    header = _header(_login(api_client, account))
    _enroll(api_client, classroom, header=header)
    return {**account, "header": header}


def _mint_student(
    api_client: TestClient, classroom: dict, class_id: str, header: dict | None = None
) -> dict:
    response = api_client.post(
        f"/api/v1/me/classes/{class_id}/ws-ticket",
        headers=header or classroom["student_header"],
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mint_teacher(api_client: TestClient, classroom: dict, class_id: str) -> dict:
    response = api_client.post(
        f"{_teacher_base(classroom, class_id)}/ws-ticket",
        headers=classroom["teacher_header"],
    )
    assert response.status_code == 201, response.text
    return response.json()


def _ws_url(class_id: str, ticket: str) -> str:
    return f"/ws/classes/{class_id}?ticket={ticket}"


def _wait_until(predicate, *, timeout: float = 8.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {message}")


def _expect_close(websocket, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as excinfo:
        websocket.receive_json()
    assert excinfo.value.code == code


def _receive_until_class_ended(websocket, class_id: str) -> None:
    """Consume frames until class.ended (presence broadcasts queue first)."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        frame = websocket.receive_json()
        if frame == {"type": "class.ended", "class_id": class_id}:
            return
        assert frame.get("type") in {"presence.joined", "presence.left"}, frame
    raise AssertionError("class.ended never arrived on this socket")


def _expect_close_tolerant(websocket, codes) -> None:
    """Wait for the close; an advisory ``error`` frame may arrive first.

    A frame that raced the ending (a refused heartbeat, a straggler
    message) is answered with a best-effort ``error`` before the socket
    closes — the close code is what must stay exactly right. ``codes``
    may be one code or several (a race whose loser is allowed to close
    with its own honest code).
    """
    allowed = {codes} if isinstance(codes, int) else set(codes)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            frame = websocket.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code in allowed, exc.code
            return
        assert frame.get("type") in {"error", "presence.left"}, frame
    raise AssertionError("socket never closed")


def _receive_until_class_ended_or_closed(
    websocket, class_id: str, *, allowed_close=(1000, 4008)
) -> None:
    """class.ended, or the socket already closed by its legitimate loser.

    Both outcomes of a race are honest: the ``class.ended`` event closes
    every socket 1000, while a heartbeat that lost the race is refused
    and closes 4008 — whichever lands first wins the socket, and the
    class still ends exactly once.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            frame = websocket.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code in allowed_close, exc.code
            return
        if frame == {"type": "class.ended", "class_id": class_id}:
            _expect_close_tolerant(websocket, allowed_close)
            return
        assert frame.get("type") in {"error", "presence.left", "presence.joined"}, frame
    raise AssertionError("class.ended / socket closure never arrived")


# --- database helpers -----------------------------------------------------------------------


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


def _audit_count(engine, user_id: str) -> int:
    from app.models.auth_event import AuthEvent

    session = _session(engine)
    try:
        return session.scalar(
            select(func.count())
            .select_from(AuthEvent)
            .where(
                AuthEvent.user_id == uuid.UUID(user_id),
                AuthEvent.event_type == "online_class_ended",
            )
        )
    finally:
        session.close()


def _open_a_segment(engine, class_id: str, student_user_id: str) -> uuid.UUID:
    """Open one attendance segment directly (no socket needed)."""
    from app.models.student import Student
    from app.models.user import User
    from app.services import classroom_service

    session = _session(engine)
    try:
        student_id = session.scalar(
            select(Student.id).where(Student.user_id == uuid.UUID(student_user_id))
        )
        assert student_id is not None
        user = session.get(User, uuid.UUID(student_user_id))
        assert user is not None
        classroom_service.join_class(
            session, user, uuid.UUID(class_id), f"conn-{uuid.uuid4().hex[:8]}"
        )
        session.commit()
        return student_id
    finally:
        session.close()


def _sweep() -> int:
    """One sweep pass — the exact code path the lifespan startup runs."""
    from app.services.class_end_sweeper import class_end_sweeper

    return class_end_sweeper.sweep_once()


# --- startup recovery ------------------------------------------------------------------------


def test_the_startup_sweep_recovers_a_class_left_live_by_a_crash(
    api_client: TestClient, classroom: dict
) -> None:
    """A crashed worker's LIVE class ends with no client request at all.

    The sweep's first pass at startup is what makes restart recovery
    possible: the class row, its attendance segments and the connected
    socket are all finished by that one pass.
    """
    engine = classroom["engine"]
    class_id, scheduled_end = _create_short_lived_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        websocket.receive_json()  # the join snapshot

        # The process "dies" here: the window passes with the class live.
        _wait_for_the_window_to_pass(scheduled_end)

        # Nobody sends a frame and nobody presses End — only the sweep.
        assert _sweep() == 1
        assert websocket.receive_json() == {"type": "class.ended", "class_id": class_id}
        _expect_close(websocket, 1000)

    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    # The cutoff is the scheduled end, not the recovery execution time.
    assert class_row.actual_ended_at == scheduled_end
    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].left_at == class_row.actual_ended_at
    assert _audit_count(engine, classroom["teacher"]["user_id"]) == 1
    # Idempotent: a second startup pass finds nothing left to do.
    assert _sweep() == 0


# --- the background thread -------------------------------------------------------------------


def test_the_background_sweeper_ends_an_overdue_class_without_any_request(
    api_client: TestClient, classroom: dict, fast_sweeper
) -> None:
    """The live thread ends the class between two idle heartbeats.

    No participant sends another frame after the window passes: the
    sweeper is the only thing that can finish the class, and the
    recorded duration still stops at the timetable, not at the sweep.
    """
    engine = classroom["engine"]
    class_id, scheduled_end = _create_short_lived_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        websocket.receive_json()
        # Deliberately send NOTHING after the window closes: the socket
        # just waits, and the background thread is the only thing that
        # can finish the class.
        assert websocket.receive_json() == {"type": "class.ended", "class_id": class_id}
        _expect_close(websocket, 1000)

    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    assert class_row.actual_ended_at == scheduled_end
    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].left_at == class_row.actual_ended_at
    assert _audit_count(engine, classroom["teacher"]["user_id"]) == 1


# --- multi-worker safety ----------------------------------------------------------------------


def test_concurrent_sweeps_of_the_same_class_produce_exactly_one_transition(
    api_client: TestClient, classroom: dict
) -> None:
    """Two workers sweeping the same class: one winner, one NOTIFY.

    Each thread opens its OWN session, exactly as a second worker
    process would; the ``SELECT ... FOR UPDATE`` on the class row (with
    its authoritative re-read) is what leaves a single transition.
    """
    from app.core.database import SessionLocal
    from app.services.online_class_service import end_due_classes
    from app.core.config import get_settings

    engine = classroom["engine"]
    class_id, scheduled_end = _create_short_lived_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    _open_a_segment(engine, class_id, classroom["student"]["user_id"])
    _wait_for_the_window_to_pass(scheduled_end)

    dsn = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    def _one_worker() -> int:
        session = SessionLocal()
        try:
            ended = end_due_classes(session)
            session.commit()
            return ended
        finally:
            session.close()

    with psycopg.connect(dsn, autocommit=True) as listener:
        listener.execute("LISTEN realtime_events")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: _one_worker(), range(2)))
        notifications = list(listener.notifies(timeout=2.0))

    # Exactly one worker performed the transition; the loser saw ENDED.
    assert sorted(results) == [0, 1]
    class_ended = [
        notify
        for notify in notifications
        if json.loads(notify.payload).get("type") == "class.ended"
        and json.loads(notify.payload).get("class_id") == class_id
    ]
    assert len(class_ended) == 1

    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    assert class_row.actual_ended_at == scheduled_end
    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].left_at == class_row.actual_ended_at
    assert segments[0].left_at >= segments[0].joined_at
    assert _audit_count(engine, classroom["teacher"]["user_id"]) == 1


# --- manual End versus sweep -------------------------------------------------------------------


def test_a_manual_end_after_the_sweep_conflicts(
    api_client: TestClient, classroom: dict
) -> None:
    """The sweep won the row lock; the teacher's late End is a 409."""
    engine = classroom["engine"]
    class_id, scheduled_end = _create_short_lived_class(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    _wait_for_the_window_to_pass(scheduled_end)
    assert _sweep() == 1

    late_end = api_client.post(
        f"{_teacher_base(classroom, class_id)}/end",
        headers=classroom["teacher_header"],
    )
    assert late_end.status_code == 409, late_end.text

    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    # The loser's End may not rewrite the winning transition's timestamp.
    assert class_row.actual_ended_at == scheduled_end
    assert _audit_count(engine, classroom["teacher"]["user_id"]) == 1


def test_a_sweep_after_the_manual_end_changes_nothing(
    api_client: TestClient, classroom: dict
) -> None:
    """The teacher ended early; a later sweep keeps that early timestamp."""
    engine = classroom["engine"]
    class_id, scheduled_end = _create_short_lived_class(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    ended = api_client.post(
        f"{_teacher_base(classroom, class_id)}/end",
        headers=classroom["teacher_header"],
    )
    assert ended.status_code == 200, ended.text
    early_end = _class_row(engine, class_id).actual_ended_at
    assert early_end is not None
    assert early_end < scheduled_end

    # The timetable passes afterwards, but the class is already ENDED.
    _wait_for_the_window_to_pass(scheduled_end)
    assert _sweep() == 0

    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    assert class_row.actual_ended_at == early_end
    assert _audit_count(engine, classroom["teacher"]["user_id"]) == 1


# --- attendance finalization ---------------------------------------------------------------------


def test_a_join_after_the_automatic_end_is_refused_and_attendance_stays_final(
    api_client: TestClient, classroom: dict
) -> None:
    """No segment may open after the class ended; closed means closed."""
    engine = classroom["engine"]
    class_id, scheduled_end = _create_short_lived_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    _open_a_segment(engine, class_id, classroom["student"]["user_id"])
    _wait_for_the_window_to_pass(scheduled_end)
    assert _sweep() == 1

    finalized = _segments(engine, class_id)
    assert len(finalized) == 1
    assert finalized[0].left_at is not None

    late_ticket = api_client.post(
        f"/api/v1/me/classes/{class_id}/ws-ticket",
        headers=classroom["student_header"],
    )
    assert late_ticket.status_code == 409, late_ticket.text

    after = _segments(engine, class_id)
    assert len(after) == 1
    assert after[0].left_at == finalized[0].left_at


# --- propagation to every socket -------------------------------------------------------------------


def test_every_connected_socket_hears_the_end_once_and_closes(
    api_client: TestClient, classroom: dict
) -> None:
    """One class.ended per socket, then close 1000 — never a second frame."""
    engine = classroom["engine"]
    second = _seed_second_student(api_client, classroom)
    class_id, scheduled_end = _create_short_lived_class(
        api_client, classroom, grace_seconds=4.0
    )
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)

    teacher_ticket = _mint_teacher(api_client, classroom, class_id)["ticket"]
    student_ticket = _mint_student(api_client, classroom, class_id)["ticket"]
    second_ticket = _mint_student(
        api_client, classroom, class_id, header=second["header"]
    )["ticket"]

    with (
        api_client.websocket_connect(_ws_url(class_id, teacher_ticket)) as teacher_ws,
        api_client.websocket_connect(_ws_url(class_id, student_ticket)) as student_ws,
        api_client.websocket_connect(_ws_url(class_id, second_ticket)) as second_ws,
    ):
        teacher_ws.receive_json()
        student_ws.receive_json()
        second_ws.receive_json()

        _wait_for_the_window_to_pass(scheduled_end)
        assert _sweep() == 1

        for websocket in (teacher_ws, student_ws, second_ws):
            # The later joins queued presence.joined on the earlier
            # sockets; class.ended still arrives exactly once each.
            _receive_until_class_ended(websocket, class_id)
            _expect_close(websocket, 1000)

    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    segments = _segments(engine, class_id)
    assert len(segments) == 2  # both students, none for the teacher
    for segment in segments:
        assert segment.left_at == class_row.actual_ended_at
    assert _audit_count(engine, classroom["teacher"]["user_id"]) == 1


# --- cross-slice races -------------------------------------------------------------------------------


def test_a_heartbeat_racing_the_sweep_ends_the_class_exactly_once(
    api_client: TestClient, classroom: dict
) -> None:
    """Slice 3C's heartbeat and slice 3E's sweep, same instant.

    Either may perform the transition — both run the same guarded
    ending path — but the class ends once: one audit row, one
    ``class.ended`` on the socket, segments closed at the single
    ``actual_ended_at``.
    """
    engine = classroom["engine"]
    class_id, scheduled_end = _create_short_lived_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        websocket.receive_json()
        _wait_for_the_window_to_pass(scheduled_end)

        websocket.send_json({"type": "presence.heartbeat"})
        with ThreadPoolExecutor(max_workers=1) as pool:
            sweep_result = pool.submit(_sweep).result(timeout=10)

        # Whoever lost the race may have its heartbeat refused (advisory
        # error frame, then close 4008) instead of hearing class.ended
        # and closing 1000 — either way the socket is finished exactly
        # once and the class ended exactly once.
        _receive_until_class_ended_or_closed(websocket, class_id)

    # The heartbeat may have won (sweep 0) or the sweep (heartbeat refused,
    # sweep 1) — both leave exactly ONE ended transition at the cutoff.
    assert sweep_result in (0, 1)
    class_row = _class_row(engine, class_id)
    assert class_row.status == "ended"
    assert class_row.actual_ended_at == scheduled_end
    segments = _segments(engine, class_id)
    assert len(segments) == 1
    assert segments[0].left_at == class_row.actual_ended_at
    assert _audit_count(engine, classroom["teacher"]["user_id"]) == 1


def test_a_late_heartbeat_after_the_sweep_cannot_extend_the_class(
    api_client: TestClient, classroom: dict
) -> None:
    """A straggler heartbeat after the sweep extends nothing, silently.

    The socket is already closed by ``class.ended``; a reconnect attempt
    cannot mint a ticket for an ended class, and the closed segment keeps
    the sweep's authoritative ``left_at``.
    """
    engine = classroom["engine"]
    class_id, scheduled_end = _create_short_lived_class(api_client, classroom)
    _enroll(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    ticket = _mint_student(api_client, classroom, class_id)["ticket"]

    with api_client.websocket_connect(_ws_url(class_id, ticket)) as websocket:
        websocket.receive_json()
        _wait_for_the_window_to_pass(scheduled_end)
        assert _sweep() == 1
        assert websocket.receive_json() == {"type": "class.ended", "class_id": class_id}
        _expect_close(websocket, 1000)

    finalized_left_at = _segments(engine, class_id)[0].left_at

    late_ticket = api_client.post(
        f"/api/v1/me/classes/{class_id}/ws-ticket",
        headers=classroom["student_header"],
    )
    assert late_ticket.status_code == 409

    after = _segments(engine, class_id)
    assert len(after) == 1
    assert after[0].left_at == finalized_left_at
    assert _audit_count(engine, classroom["teacher"]["user_id"]) == 1
