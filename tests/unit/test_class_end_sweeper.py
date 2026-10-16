"""Unit tests: automatic scheduled class ending (Phase 3, slice 3E).

In-memory SQLite, no HTTP — the lifecycle contract behind "nobody has to
press End":

- :func:`online_class_service.end_due_classes` ends a bounded batch of
  overdue ``live`` classes through the SAME transition the teacher's End
  button uses (one audit row, one ``class.ended`` publication per class);
- the automatic cutoff is the class's ``scheduled_end_at`` — a sweep that
  runs late never stretches the class or anybody's attendance — while a
  manual End keeps recording the actual early-end time;
- scheduled / ended / cancelled classes are never touched, and repeated
  sweeps are idempotent (no second transition, no duplicate audit);
- the integrity floor: an automatic end never precedes the ``joined_at``
  of a segment the same transition closes (the schema's
  ``left_at >= joined_at`` CHECK), so a window rewritten underneath a
  live segment cannot break the end;
- :class:`app.services.class_end_sweeper.ClassEndSweeper` starts with the
  application lifespan (immediate startup pass), keeps one fresh
  short-lived session per pass, survives a failing pass, and joins its
  thread on stop.

PostgreSQL locking behaviour (several workers sweeping the same class) is
covered by the integration suite, where real row locks exist.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.class_attendance_segment import ClassAttendanceSegment
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    TeacherVerificationStatus,
    UserRole,
    UserStatus,
)
from app.models.online_class_session import OnlineClassSession
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.student import Student
from app.models.subject import Subject
from app.models.teacher import Teacher
from app.models.user import User
from app.schemas.online_class import OnlineClassSessionCreate
from app.services import (
    classroom_service,
    learning_enrollment_service,
    online_class_service,
    teaching_offering_service,
)
from app.services import class_end_sweeper as sweeper_module
from app.services.class_end_sweeper import ClassEndSweeper
from app.services.learning_context_service import LearningConflictError
from app.schemas.learning import TeachingOfferingCreate

PASSWORD = "correct horse battery staple"

#: A far-future window so starting the class is always legal.
START = datetime(2027, 3, 1, 10, 0, tzinfo=timezone.utc)
END = datetime(2027, 3, 1, 11, 0, tzinfo=timezone.utc)


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
def catalog(session: Session) -> dict:
    year = AcademicYear(
        name="2026/2027",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 6, 30),
        status=AcademicYearStatus.ACTIVE.value,
    )
    pathway = Pathway(code="O_LEVEL", name="O Level")
    level = EducationLevel(code="S3", name="Senior 3", level_number=3)
    session.add_all([year, pathway, level])
    session.flush()
    session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

    program = Program(code="COMB", name="Combination", program_type="combination")
    session.add(program)
    session.flush()
    program_version = ProgramVersion(
        program_id=program.id,
        academic_year_id=year.id,
        pathway_id=pathway.id,
        education_level_id=level.id,
        code="COMB-2026",
        name="Combination 2026",
    )
    session.add(program_version)

    maths = Subject(code="MATH", name="Mathematics")
    session.add(maths)
    session.flush()
    session.add(ProgramSubject(program_version_id=program_version.id, subject_id=maths.id))
    session.commit()
    return {
        "year": year,
        "pathway": pathway,
        "level": level,
        "program_version": program_version,
        "maths": maths,
    }


@pytest.fixture()
def approved_teacher(session: Session) -> User:
    user = User(
        email=f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(user_id=user.id, full_name="Sweep Teacher", verification_status=TeacherVerificationStatus.APPROVED.value)
    )
    session.commit()
    return user


@pytest.fixture()
def enrolled_student(session: Session, approved_teacher: User, catalog: dict) -> User:
    from tests.conftest import approve_teacher_for_payloads

    payload = TeachingOfferingCreate(
        academic_year_id=catalog["year"].id,
        pathway_id=catalog["pathway"].id,
        education_level_id=catalog["level"].id,
        program_version_id=catalog["program_version"].id,
        subject_id=catalog["maths"].id,
    )
    approve_teacher_for_payloads(session, approved_teacher, [payload])
    read = teaching_offering_service.create_offering(
        session,
        approved_teacher,
        payload,
    )
    offering_id = read.offering_id
    student = User(
        email=f"student-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(student)
    session.flush()
    profile = Student(user_id=student.id, full_name="Sweep Student")
    session.add(profile)
    session.flush()
    learning_enrollment_service.enroll(session, profile, offering_id)
    session.commit()
    student.offering_id = offering_id  # type: ignore[attr-defined]
    return student


@pytest.fixture()
def offering_id(session: Session, enrolled_student: User) -> uuid.UUID:
    return enrolled_student.offering_id  # type: ignore[attr-defined]


def _create(
    session: Session,
    teacher: User,
    offering: uuid.UUID,
    start: datetime = START,
    end: datetime = END,
):
    return online_class_service.create_my_class(
        session,
        teacher,
        offering,
        OnlineClassSessionCreate(scheduled_start_at=start, scheduled_end_at=end),
    )


def _start(
    session: Session,
    teacher: User,
    offering: uuid.UUID,
    start: datetime = START,
    end: datetime = END,
) -> uuid.UUID:
    created = _create(session, teacher, offering, start, end)
    online_class_service.start_my_class(session, teacher, offering, created.class_id)
    session.commit()
    return created.class_id


def _force_overdue(session: Session, class_id: uuid.UUID, end: datetime) -> None:
    """Rewrite the scheduled window into the past — the class is overdue."""
    row = session.get(OnlineClassSession, class_id)
    row.scheduled_start_at = end - timedelta(hours=1)
    row.scheduled_end_at = end
    session.commit()


def _row(session: Session, class_id: uuid.UUID) -> OnlineClassSession:
    session.expire_all()
    return session.get(OnlineClassSession, class_id)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive UTC datetimes; compare them as aware ones."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _end_events(session: Session, user: User) -> list[AuthEvent]:
    rows = session.scalars(
        select(AuthEvent).where(
            AuthEvent.user_id == user.id,
            AuthEvent.event_type == "online_class_ended",
        )
    ).all()
    return list(rows)


class _RecordingBus:
    """Stand-in for the realtime bus: records what would be published."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, _session, event: dict) -> None:
        self.published.append(event)


@pytest.fixture()
def recording_bus(monkeypatch) -> _RecordingBus:
    bus = _RecordingBus()
    monkeypatch.setattr(online_class_service, "get_event_bus", lambda: bus)
    return bus


# --- end_due_classes: the batch primitive ----------------------------------------------


def test_one_overdue_live_class_is_ended_by_the_batch(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    scheduled_end = datetime(2020, 1, 1, 11, 0, tzinfo=timezone.utc)
    _force_overdue(session, class_id, scheduled_end)

    ended = online_class_service.end_due_classes(session)
    session.commit()

    assert ended == 1
    row = _row(session, class_id)
    assert row.status == "ended"
    # The automatic cutoff is the scheduled end — never the sweep time.
    assert _aware(row.actual_ended_at) == scheduled_end
    assert len(_end_events(session, approved_teacher)) == 1


def test_a_late_sweep_still_records_the_scheduled_cutoff(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    """A sweep that runs ten minutes late must not stretch the class."""
    class_id = _start(session, approved_teacher, offering_id)
    scheduled_end = datetime.now(timezone.utc) - timedelta(minutes=10)
    _force_overdue(session, class_id, scheduled_end)

    sweep_time = datetime.now(timezone.utc)
    assert online_class_service.end_due_classes(session, now=sweep_time) == 1
    session.commit()

    row = _row(session, class_id)
    assert _aware(row.actual_ended_at) == scheduled_end
    assert _aware(row.actual_ended_at) < sweep_time


def test_classes_outside_the_live_overdue_shape_are_never_touched(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    past = START - timedelta(days=30)
    scheduled = _create(
        session, approved_teacher, offering_id, past, past + timedelta(hours=1)
    )
    live_now = _start(
        session,
        approved_teacher,
        offering_id,
        START + timedelta(hours=2),
        START + timedelta(hours=3),
    )
    cancelled = _create(
        session, approved_teacher, offering_id, START + timedelta(hours=4),
        START + timedelta(hours=5),
    )
    online_class_service.cancel_my_class(
        session, approved_teacher, offering_id, cancelled.class_id
    )
    ended = _start(
        session, approved_teacher, offering_id, START + timedelta(hours=6),
        START + timedelta(hours=7),
    )
    online_class_service.end_my_class(session, approved_teacher, offering_id, ended)
    session.commit()

    assert online_class_service.end_due_classes(session) == 0
    session.commit()
    assert _row(session, scheduled.class_id).status == "scheduled"
    assert _row(session, live_now).status == "live"
    assert _row(session, cancelled.class_id).status == "cancelled"
    assert _row(session, ended).status == "ended"


def test_repeated_sweeps_never_duplicate_the_transition(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, recording_bus
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _force_overdue(session, class_id, datetime(2020, 1, 1, 11, 0, tzinfo=timezone.utc))

    assert online_class_service.end_due_classes(session) == 1
    session.commit()
    for _ in range(3):
        assert online_class_service.end_due_classes(session) == 0
    session.commit()

    assert len(_end_events(session, approved_teacher)) == 1
    # ``_start`` published class.started too; only ONE class.ended, ever.
    ended = [
        event for event in recording_bus.published if event["type"] == "class.ended"
    ]
    assert [event["class_id"] for event in ended] == [str(class_id)]


def test_the_batch_limit_bounds_one_pass(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    window = datetime(2020, 1, 1, 11, 0, tzinfo=timezone.utc)
    class_ids = [
        _start(
            session,
            approved_teacher,
            offering_id,
            START + timedelta(hours=offset),
            START + timedelta(hours=offset + 1),
        )
        for offset in (10, 12, 14)
    ]
    for class_id in class_ids:
        _force_overdue(session, class_id, window)

    assert online_class_service.end_due_classes(session, limit=1) == 1
    session.commit()
    remaining = sum(
        1 for class_id in class_ids if _row(session, class_id).status == "live"
    )
    assert remaining == 2

    # Later passes finish the queue (ordered by scheduled_end_at).
    while online_class_service.end_due_classes(session) > 0:
        session.commit()
    assert [_row(session, class_id).status for class_id in class_ids] == ["ended"] * 3


def test_an_automatic_end_precedes_no_open_segment(
    session: Session, approved_teacher: User, enrolled_student: User, offering_id: uuid.UUID
) -> None:
    """Integrity floor: never end before a segment the close would write.

    A window rewritten underneath an already-open segment must not make
    the transition violate ``left_at >= joined_at``: the recorded end
    moment is floored at that segment's join, participation stays
    consistent, and the class still ends exactly once.
    """
    class_id = _start(session, approved_teacher, offering_id)
    classroom_service.join_class(
        session, enrolled_student, class_id, "conn-floor"
    )
    session.commit()
    _force_overdue(session, class_id, datetime(2020, 1, 1, 11, 0, tzinfo=timezone.utc))

    assert online_class_service.end_due_classes(session) == 1
    session.commit()

    row = _row(session, class_id)
    segment = session.scalar(
        select(ClassAttendanceSegment).where(
            ClassAttendanceSegment.class_session_id == class_id
        )
    )
    assert row.status == "ended"
    assert segment.left_at == row.actual_ended_at
    assert segment.left_at >= segment.joined_at


def test_a_participant_joining_before_the_window_closes_is_fully_counted(
    session: Session, approved_teacher: User, enrolled_student: User, offering_id: uuid.UUID
) -> None:
    """The production shape: join inside the window, end at its close.

    ``actual_ended_at`` equals ``scheduled_end_at`` (not the later sweep
    time), and the segment closes at that same authoritative moment —
    participation can never run past the timetable.
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=5)
    window_end = window_start + timedelta(minutes=10)
    class_id = _start(
        session, approved_teacher, offering_id, window_start, window_end
    )
    classroom_service.join_class(session, enrolled_student, class_id, "conn-real")
    session.commit()

    # The window passes; the sweep runs a minute late.
    late_sweep = window_end + timedelta(minutes=1)
    assert (
        online_class_service.end_due_classes(session, now=late_sweep) == 1
    )
    session.commit()

    row = _row(session, class_id)
    segment = session.scalar(
        select(ClassAttendanceSegment).where(
            ClassAttendanceSegment.class_session_id == class_id
        )
    )
    assert _aware(row.actual_ended_at) == window_end
    assert _aware(row.actual_ended_at) < late_sweep
    assert segment.left_at == row.actual_ended_at


# --- the sweeper's lifecycle --------------------------------------------------------------


def _wait_until(predicate, *, timeout: float = 5.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for: {message}")


def _shrink_the_sweep_interval(monkeypatch) -> None:
    """The sweeper re-reads settings each pass, so tests can make it quick."""
    monkeypatch.setattr(
        sweeper_module,
        "get_settings",
        lambda: SimpleNamespace(
            CLASS_END_SWEEP_INTERVAL_SECONDS=0.05, CLASS_END_SWEEP_BATCH_LIMIT=1
        ),
    )


def test_start_sweeps_immediately_and_stop_joins_the_thread(monkeypatch) -> None:
    """Startup = first pass now (restart recovery), then a clean stop."""
    _shrink_the_sweep_interval(monkeypatch)
    sweeper = ClassEndSweeper()
    calls: list[float] = []
    monkeypatch.setattr(sweeper, "sweep_once", lambda: calls.append(time.monotonic()) or 0)

    sweeper.start()
    sweeper.start()  # idempotent: one thread only
    _wait_until(lambda: len(calls) >= 2, message="sweeper never swept")
    assert sweeper.is_running
    thread = sweeper._thread

    sweeper.stop()
    sweeper.stop()  # idempotent
    assert not sweeper.is_running
    assert not thread.is_alive()
    stopped_at = len(calls)
    time.sleep(0.2)
    assert len(calls) == stopped_at  # nothing ran after stop


def test_the_first_pass_happens_before_any_wait(monkeypatch) -> None:
    sweeper = ClassEndSweeper()
    seen: list[float] = []
    started = time.monotonic()
    monkeypatch.setattr(sweeper, "sweep_once", lambda: seen.append(time.monotonic()) or 0)
    sweeper.start()
    try:
        _wait_until(lambda: seen, message="startup sweep never ran")
        assert seen[0] - started < 1.0
    finally:
        sweeper.stop()


def test_a_failing_pass_never_kills_the_loop(monkeypatch) -> None:
    _shrink_the_sweep_interval(monkeypatch)
    sweeper = ClassEndSweeper()
    attempts: list[int] = []
    lock = threading.Lock()

    def _flaky() -> int:
        with lock:
            attempts.append(1)
            count = len(attempts)
        if count <= 2:
            raise RuntimeError("database hiccup")
        return 0

    monkeypatch.setattr(sweeper, "sweep_once", _flaky)
    sweeper.start()
    try:
        _wait_until(lambda: len(attempts) >= 3, message="loop died after failures")
        assert sweeper.is_running
    finally:
        sweeper.stop()
    assert not sweeper.is_running


def test_sweep_once_uses_one_fresh_short_lived_session(monkeypatch) -> None:
    """One pass = open session -> batch -> commit -> close. No session leak."""
    events: list[str] = []

    class _RecordingSession:
        def commit(self) -> None:
            events.append("commit")

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    def _factory():
        events.append("open")
        return _RecordingSession()

    seen: dict[str, object] = {}

    def _end_due(session, *, now=None, limit=None):
        seen["session"] = session
        seen["limit"] = limit
        return 3

    monkeypatch.setattr(sweeper_module, "SessionLocal", _factory)
    monkeypatch.setattr(online_class_service, "end_due_classes", _end_due)

    assert sweeper_module.class_end_sweeper.sweep_once() == 3
    assert events == ["open", "commit", "close"]
    assert isinstance(seen["session"], _RecordingSession)
    assert seen["limit"] == 50  # the documented batch default


def test_sweep_once_rolls_back_and_still_closes_on_failure(monkeypatch) -> None:
    events: list[str] = []

    class _RecordingSession:
        def commit(self) -> None:
            events.append("commit")

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(sweeper_module, "SessionLocal", _RecordingSession)

    def _boom(session, *, now=None, limit=None):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(online_class_service, "end_due_classes", _boom)

    with pytest.raises(RuntimeError):
        sweeper_module.class_end_sweeper.sweep_once()
    assert events == ["rollback", "close"]


def test_the_process_sweeper_is_stopped_between_tests() -> None:
    """The suite's autouse guard leaves no background thread behind."""
    assert sweeper_module.class_end_sweeper.is_running is False


# --- lifespan wiring ----------------------------------------------------------------------


def test_the_application_lifespan_starts_and_stops_the_sweeper(monkeypatch) -> None:
    from app.main import app

    actions: list[str] = []
    monkeypatch.setattr(
        sweeper_module.class_end_sweeper, "start", lambda: actions.append("start")
    )
    monkeypatch.setattr(
        sweeper_module.class_end_sweeper, "stop", lambda **_: actions.append("stop")
    )

    async def _round_trip() -> None:
        async with app.router.lifespan_context(app):
            assert actions == ["start"]
        assert actions == ["start", "stop"]

    asyncio.run(_round_trip())


def test_the_sweep_defaults_target_at_most_five_seconds() -> None:
    from app.core.config import get_settings

    settings = get_settings()
    assert 0 < settings.CLASS_END_SWEEP_INTERVAL_SECONDS <= 5.0
    assert settings.CLASS_END_SWEEP_BATCH_LIMIT >= 1


# --- the automatic end remains one authoritative transition -------------------------------


def test_manual_end_and_sweep_leave_exactly_one_audit_row(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, recording_bus
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    online_class_service.end_my_class(session, approved_teacher, offering_id, class_id)
    session.commit()
    manual_ended_at = _row(session, class_id).actual_ended_at

    # The sweep later finds nothing to do — no second audit, no re-open.
    assert online_class_service.end_due_classes(session) == 0
    session.commit()

    row = _row(session, class_id)
    assert row.status == "ended"
    assert row.actual_ended_at == manual_ended_at
    assert len(_end_events(session, approved_teacher)) == 1
    ended = [
        event for event in recording_bus.published if event["type"] == "class.ended"
    ]
    assert [event["class_id"] for event in ended] == [str(class_id)]


def test_a_late_heartbeat_after_the_sweep_is_refused(
    session: Session, approved_teacher: User, enrolled_student: User, offering_id: uuid.UUID
) -> None:
    """A heartbeat arriving after the automatic end can extend nothing."""
    window_start = datetime.now(timezone.utc) - timedelta(minutes=5)
    window_end = window_start + timedelta(minutes=10)
    class_id = _start(session, approved_teacher, offering_id, window_start, window_end)
    classroom_service.join_class(session, enrolled_student, class_id, "conn-hb")
    session.commit()

    assert online_class_service.end_due_classes(session, now=window_end + timedelta(seconds=30)) == 1
    session.commit()
    session.expire_all()
    ended_at = session.get(OnlineClassSession, class_id).actual_ended_at

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.heartbeat(session, enrolled_student, class_id, "conn-hb")
    session.rollback()
    assert "requires a live class" in str(excinfo.value)

    session.expire_all()
    segment = session.scalar(
        select(ClassAttendanceSegment).where(
            ClassAttendanceSegment.class_session_id == class_id
        )
    )
    assert segment.left_at == ended_at
    assert segment.last_seen_at <= ended_at
