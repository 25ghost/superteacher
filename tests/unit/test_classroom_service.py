"""Unit tests: classroom participation, attendance and reads (Phase 3, slice 3B).

In-memory SQLite, no HTTP — the classroom service's own contract, before
any WebSocket exists:

- join opens exactly ONE participation segment (partial unique backstop),
  only inside a ``live`` class of an ACTIVE enrollment; unknown, foreign
  and not-enrolled ids answer the SAME 404 (L6), a duplicate open join is
  one 409 whether caught by the check or by the unique index;
- leave closes YOUR segment at ``now`` (found by connection id), twice is
  a 409, an unknown connection the same 404 shape, and a reconnect records
  a second historical segment instead of colliding;
- heartbeat moves liveness ONLY and doubles as the auto-end sweep: an
  overdue class ends inside the call (``auto_ended=True``) with every open
  segment finalized at ``actual_ended_at``;
- ending a class — manually or mechanically — bulk-closes every open
  segment in the same transaction, exactly at the end moment, with no
  audit row per participant (the class end is audited once);
- attendance is DERIVED on every read: segments clamped to the class
  window, ``attended`` iff the class ran, cumulative >= 10 minutes and
  cumulative >= 50% of the real duration (ties qualify);
- teacher reads ride ``get_my_class`` (403 role / 404 unknown-or-foreign);
  the roster unions ACTIVE enrollments with segment-having students so a
  dropped enrollment never erases history; student attendance authorizes
  through enrollment OR prior participation (409 before the class has
  started), never-present students get the SAME 404 as unknown ids;
- the transcript is HISTORICAL: ENDED classes only (409 scheduled/live/
  cancelled — the live classroom belongs to the WebSocket), readable by
  the owning teacher (offering ownership, no segment needed) and by a
  student if and only if a participation segment exists — past
  participation, never current enrollment and never the 50% attendance
  rule, grants and preserves that read;
- exactly one ``auth_events`` row per join/leave; heartbeats, class-end
  bulk closes and reads stay silent.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.config import get_settings
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.class_attendance_segment import ClassAttendanceSegment
from app.models.class_message import ClassMessage
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
from app.realtime import close_codes as cc
from app.schemas.learning import TeachingOfferingCreate
from app.schemas.online_class import OnlineClassSessionCreate
from app.services import (
    classroom_service,
    learning_enrollment_service,
    online_class_service,
    teaching_offering_service,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)
from app.services.message_rate_limiter import message_rate_limiter

PASSWORD = "correct horse battery staple"

#: Windows are far in the future so "start" is legal against the real clock.
START = datetime(2027, 3, 1, 10, 0, tzinfo=timezone.utc)
END = datetime(2027, 3, 1, 11, 0, tzinfo=timezone.utc)


def _window(offset_hours: float, duration_hours: float = 1.0) -> tuple[datetime, datetime]:
    start = START + timedelta(hours=offset_hours)
    return start, start + timedelta(hours=duration_hours)


def _utc(value: datetime) -> datetime:
    """SQLite round-trips drop the tz marker — normalize before comparing."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


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
    """A minimal coherent catalog: year + pathway + level + program + subject."""
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
    session.add(
        ProgramSubject(program_version_id=program_version.id, subject_id=maths.id)
    )
    session.commit()
    return {
        "year": year,
        "pathway": pathway,
        "level": level,
        "program_version": program_version,
        "maths": maths,
    }


def _teacher_user(session: Session, verification: str) -> User:
    user = User(
        email=f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(user_id=user.id, full_name="Test Teacher", verification_status=verification)
    )
    session.commit()
    return user


@pytest.fixture()
def approved_teacher(session: Session) -> User:
    return _teacher_user(session, TeacherVerificationStatus.APPROVED.value)


@pytest.fixture()
def other_teacher(session: Session) -> User:
    return _teacher_user(session, TeacherVerificationStatus.APPROVED.value)


def _student_user(session: Session, label: str) -> User:
    user = User(
        email=f"student-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(Student(user_id=user.id, full_name=label))
    session.commit()
    return user


@pytest.fixture()
def student(session: Session) -> User:
    return _student_user(session, "Test Student")


@pytest.fixture()
def other_student(session: Session) -> User:
    return _student_user(session, "Outsider Student")


@pytest.fixture()
def quiet_student(session: Session) -> User:
    return _student_user(session, "Quiet Student")


def _offering(session: Session, catalog: dict, teacher: User) -> uuid.UUID:
    read = teaching_offering_service.create_offering(
        session,
        teacher,
        TeachingOfferingCreate(
            academic_year_id=catalog["year"].id,
            pathway_id=catalog["pathway"].id,
            education_level_id=catalog["level"].id,
            program_version_id=catalog["program_version"].id,
            subject_id=catalog["maths"].id,
        ),
    )
    session.commit()
    return read.offering_id


@pytest.fixture()
def offering_id(session: Session, catalog: dict, approved_teacher: User) -> uuid.UUID:
    return _offering(session, catalog, approved_teacher)


@pytest.fixture()
def other_offering_id(session: Session, catalog: dict, other_teacher: User) -> uuid.UUID:
    return _offering(session, catalog, other_teacher)


def _student_profile(session: Session, user: User) -> Student:
    return session.scalar(select(Student).where(Student.user_id == user.id))


def _enroll(session: Session, user: User, offering: uuid.UUID) -> None:
    learning_enrollment_service.enroll(session, _student_profile(session, user), offering)
    session.commit()


@pytest.fixture()
def enrolled_student(session: Session, student: User, offering_id: uuid.UUID) -> User:
    """A student with an ACTIVE enrollment in the tested offering."""
    _enroll(session, student, offering_id)
    return student


@pytest.fixture()
def other_enrolled_student(session: Session, other_student: User, offering_id: uuid.UUID) -> User:
    """A second ACTIVE student — the roster needs more than one body."""
    _enroll(session, other_student, offering_id)
    return other_student


@pytest.fixture()
def quiet_enrolled_student(session: Session, quiet_student: User, offering_id: uuid.UUID) -> User:
    """An enrolled student who never joins — attendance must still list them."""
    _enroll(session, quiet_student, offering_id)
    return quiet_student


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


def _start(session: Session, teacher: User, offering: uuid.UUID, offset: float = 0.0) -> uuid.UUID:
    """Create a class at ``START + offset`` hours and take it LIVE."""
    created = _create(session, teacher, offering, *_window(offset))
    online_class_service.start_my_class(session, teacher, offering, created.class_id)
    session.commit()
    return created.class_id


def _force_overdue(session: Session, class_id: uuid.UUID) -> None:
    """Move the scheduled window into the past — the class becomes overdue."""
    row = session.get(OnlineClassSession, class_id)
    row.scheduled_start_at = datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc)
    row.scheduled_end_at = datetime(2020, 1, 1, 11, 0, tzinfo=timezone.utc)
    session.commit()


def _join(session: Session, user: User, class_id: uuid.UUID, connection_id: str) -> ClassAttendanceSegment:
    segment = classroom_service.join_class(session, user, class_id, connection_id)
    session.commit()
    return segment


def _events(session: Session, user: User) -> list[str]:
    rows = session.scalars(select(AuthEvent).where(AuthEvent.user_id == user.id)).all()
    return [e.event_type for e in rows if e.event_type.startswith("online_class")]


def _segments(session: Session, class_id: uuid.UUID) -> list[ClassAttendanceSegment]:
    stmt = (
        select(ClassAttendanceSegment)
        .where(ClassAttendanceSegment.class_session_id == class_id)
        .order_by(ClassAttendanceSegment.connection_id)
    )
    return list(session.scalars(stmt))


def _seed_message(
    session: Session, class_id: uuid.UUID, sender: User, sequence: int, body: str
) -> ClassMessage:
    message = ClassMessage(
        class_session_id=class_id,
        sender_user_id=sender.id,
        client_message_id=f"seed-{sequence}",
        body=body,
        sequence=sequence,
    )
    session.add(message)
    session.flush()
    return message


# --- join -----------------------------------------------------------------------------


def test_join_opens_exactly_one_segment_and_is_audited(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    segment = _join(session, enrolled_student, class_id, "conn-1")

    assert segment.class_session_id == class_id
    assert segment.student_id == _student_profile(session, enrolled_student).id
    assert segment.connection_id == "conn-1"
    assert segment.joined_at == segment.last_seen_at
    assert segment.left_at is None  # open until leave or class end
    assert len(_segments(session, class_id)) == 1

    assert _events(session, enrolled_student) == ["online_class_student_joined"]
    event = session.scalar(
        select(AuthEvent).where(
            AuthEvent.user_id == enrolled_student.id,
            AuthEvent.event_type == "online_class_student_joined",
        )
    )
    metadata = json.loads(event.metadata_json)
    assert metadata == {
        "class_id": str(class_id),
        "student_id": str(_student_profile(session, enrolled_student).id),
    }
    # The class end/join audit trail of the teacher is untouched by a join.
    assert _events(session, approved_teacher) == [
        "online_class_created",
        "online_class_started",
    ]


def test_join_never_runs_before_the_class_is_live(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    created = _create(session, approved_teacher, offering_id)

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.join_class(session, enrolled_student, created.class_id, "conn-1")
    session.rollback()

    message = str(excinfo.value)
    assert "online class is 'scheduled'" in message
    assert "participant join requires a live class" in message
    assert _segments(session, created.class_id) == []
    assert _events(session, enrolled_student) == []


def test_join_of_unknown_or_not_enrolled_class_is_the_same_404(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, other_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    # Unknown id, and a REAL class this student is not enrolled in: one message.
    for target in (class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            classroom_service.join_class(session, other_student, target, "conn-x")
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {target}"


def test_join_requires_a_student_account(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    with pytest.raises(LearningForbiddenError) as excinfo:
        classroom_service.join_class(session, approved_teacher, class_id, "conn-1")
    session.rollback()

    assert excinfo.value.status_code == 403
    assert str(excinfo.value) == "this operation requires a student account"


def test_a_second_open_join_is_exactly_one_conflict(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")

    # Same student, a NEW connection while the first is still open.
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.join_class(session, enrolled_student, class_id, "conn-2")
    session.rollback()

    assert str(excinfo.value) == (
        "this student already has an open attendance segment in this class"
    )
    assert len(_segments(session, class_id)) == 1  # no phantom second row


def test_a_reused_connection_id_is_the_same_conflict(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")

    # Another student claims the SAME connection id: the unique index
    # refuses it, and the service maps that to the identical 409.
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.join_class(session, other_enrolled_student, class_id, "conn-1")
    session.rollback()

    assert str(excinfo.value) == (
        "this student already has an open attendance segment in this class"
    )
    owners = {s.student_id for s in _segments(session, class_id)}
    assert owners == {_student_profile(session, enrolled_student).id}


def test_an_overdue_live_class_is_ended_instead_of_admitting_anyone(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _force_overdue(session, class_id)

    # The sweep runs first, so the refusal names the REAL problem: it is over.
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.join_class(session, enrolled_student, class_id, "conn-2")
    session.rollback()

    message = str(excinfo.value)
    assert "online class is 'ended'" in message
    assert "participant join requires a live class" in message


# --- leave ----------------------------------------------------------------------------


def test_leave_closes_your_own_segment_at_now_and_is_audited(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    joined = _join(session, enrolled_student, class_id, "conn-1")

    closed = classroom_service.leave_class(session, enrolled_student, class_id, "conn-1")
    session.commit()

    assert closed.id == joined.id
    assert closed.left_at is not None
    assert closed.last_seen_at == closed.left_at  # final heartbeat == departure
    assert closed.joined_at == joined.joined_at  # history never moves
    assert _events(session, enrolled_student) == [
        "online_class_student_joined",
        "online_class_student_left",
    ]


def test_leaving_twice_is_a_conflict_and_an_unknown_connection_the_same_404(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    with pytest.raises(LearningNotFoundError) as excinfo:
        classroom_service.leave_class(session, enrolled_student, class_id, "conn-guess")
    session.rollback()
    assert str(excinfo.value) == "no attendance segment for connection conn-guess"

    _join(session, enrolled_student, class_id, "conn-1")
    classroom_service.leave_class(session, enrolled_student, class_id, "conn-1")
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.leave_class(session, enrolled_student, class_id, "conn-1")
    session.rollback()
    assert str(excinfo.value) == "this attendance segment is already closed"
    # The failed second leave added no audit row.
    assert _events(session, enrolled_student) == [
        "online_class_student_joined",
        "online_class_student_left",
    ]


def test_leave_is_refused_once_the_class_has_ended(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    online_class_service.end_my_class(session, approved_teacher, offering_id, class_id)
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.leave_class(session, enrolled_student, class_id, "conn-1")
    session.rollback()

    message = str(excinfo.value)
    assert "online class is 'ended'" in message
    assert "participant leave requires a live class" in message


def test_a_reconnect_records_a_second_history_segment(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    classroom_service.leave_class(session, enrolled_student, class_id, "conn-1")
    session.commit()

    # Reconnecting must ADD history, never collide with the closed row
    # (the partial unique index only guards the OPEN segment).
    _join(session, enrolled_student, class_id, "conn-2")

    segments = _segments(session, class_id)
    assert len(segments) == 2
    by_connection = {s.connection_id: s for s in segments}
    assert by_connection["conn-1"].left_at is not None  # closed first
    assert by_connection["conn-2"].left_at is None  # open again
    assert len(_events(session, enrolled_student)) == 3  # join, leave, join


# --- heartbeat ------------------------------------------------------------------------


def test_heartbeat_moves_liveness_only(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    segment = _join(session, enrolled_student, class_id, "conn-1")

    # Age the liveness stamp so "it moved" is observable.
    row = session.get(ClassAttendanceSegment, segment.id)
    joined_at = row.joined_at
    row.last_seen_at = joined_at - timedelta(seconds=60)
    session.commit()
    aged = row.last_seen_at

    updated, auto_ended = classroom_service.heartbeat(
        session, enrolled_student, class_id, "conn-1"
    )
    session.commit()

    assert auto_ended is False
    assert updated.last_seen_at > aged
    assert _utc(updated.joined_at) == _utc(joined_at)  # join time is history
    assert updated.left_at is None
    # Heartbeats are traffic, not decisions: no new audit row.
    assert _events(session, enrolled_student) == ["online_class_student_joined"]


def test_heartbeat_on_an_unknown_connection_is_the_same_404(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    with pytest.raises(LearningNotFoundError) as excinfo:
        classroom_service.heartbeat(session, enrolled_student, class_id, "conn-guess")
    session.rollback()
    assert str(excinfo.value) == "no attendance segment for connection conn-guess"


def test_heartbeat_ends_an_overdue_class_and_reports_it(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _force_overdue(session, class_id)

    segment, auto_ended = classroom_service.heartbeat(
        session, enrolled_student, class_id, "conn-1"
    )
    session.commit()  # the caller persists the mechanical transition

    assert auto_ended is True
    session.expire_all()
    class_session = session.get(OnlineClassSession, class_id)
    segment = session.get(ClassAttendanceSegment, segment.id)
    assert class_session.status == "ended"
    assert segment.left_at == class_session.actual_ended_at  # finalized at the end

    # One audit for the class end (reason auto), none per participant.
    teacher_events = _events(session, approved_teacher)
    assert teacher_events == [
        "online_class_created",
        "online_class_started",
        "online_class_ended",
    ]
    assert _events(session, enrolled_student) == ["online_class_student_joined"]


def test_a_closed_segment_never_heartbeats_again(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    segment = _join(session, enrolled_student, class_id, "conn-1")

    # Close it behind the service's back while the class is still live.
    row = session.get(ClassAttendanceSegment, segment.id)
    row.left_at = datetime.now(timezone.utc)
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.heartbeat(session, enrolled_student, class_id, "conn-1")
    session.rollback()
    assert str(excinfo.value) == "this attendance segment is already closed"


def test_a_heartbeat_after_the_class_ended_never_extends_participation(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    """The §38 race's other half: a class ended first cannot be probed alive.

    The end already finalized every open segment at ``actual_ended_at``.
    A late heartbeat must be refused as "not live" and must move nothing
    - attendance can never grow past the moment the class ended.
    """
    class_id = _start(session, approved_teacher, offering_id)
    segment = _join(session, enrolled_student, class_id, "conn-1")

    # Age the liveness stamp so "nothing moved" is observable.
    row = session.get(ClassAttendanceSegment, segment.id)
    row.last_seen_at = row.joined_at - timedelta(seconds=60)
    session.commit()
    aged = row.last_seen_at

    online_class_service.end_my_class(session, approved_teacher, offering_id, class_id)
    session.commit()
    session.expire_all()
    class_session = session.get(OnlineClassSession, class_id)
    ended_at = class_session.actual_ended_at
    assert class_session.status == "ended"

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.heartbeat(session, enrolled_student, class_id, "conn-1")
    session.rollback()
    assert "requires a live class" in str(excinfo.value)

    session.expire_all()
    frozen = session.get(ClassAttendanceSegment, segment.id)
    assert _utc(frozen.left_at) == _utc(ended_at)  # closed at the end moment
    assert _utc(frozen.last_seen_at) == _utc(aged)  # the refused probe touched nothing


# --- class end finalizes participation --------------------------------------------------


def test_ending_a_class_closes_every_open_segment_at_the_end_moment(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _join(session, other_enrolled_student, class_id, "conn-2")

    online_class_service.end_my_class(session, approved_teacher, offering_id, class_id)
    session.commit()
    session.expire_all()

    class_session = session.get(OnlineClassSession, class_id)
    segments = _segments(session, class_id)
    assert len(segments) == 2
    for segment in segments:
        assert segment.left_at == class_session.actual_ended_at
        assert segment.last_seen_at == segment.joined_at  # untouched by the bulk close

    # Bulk close is silent: only the class-end row exists, no leave per body.
    assert _events(session, approved_teacher) == [
        "online_class_created",
        "online_class_started",
        "online_class_ended",
    ]
    assert _events(session, enrolled_student) == ["online_class_student_joined"]
    assert _events(session, other_enrolled_student) == ["online_class_student_joined"]


def test_the_automatic_end_also_finalizes_open_segments(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _force_overdue(session, class_id)

    read, auto_ended = online_class_service.get_my_class(
        session, approved_teacher, offering_id, class_id
    )
    session.commit()
    assert auto_ended is True
    assert read.status == "ended"

    session.expire_all()
    class_session = session.get(OnlineClassSession, class_id)
    segment = _segments(session, class_id)[0]
    assert segment.left_at == class_session.actual_ended_at


# --- attendance arithmetic ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("cumulative_seconds", "actual_seconds", "expected"),
    [
        (0, 0, "not_attended"),  # class never ran
        (0, 3600, "not_attended"),  # never connected
        (3600, 0, "not_attended"),  # nothing to cover
        (599, 600, "not_attended"),  # one second short of the 10-minute floor
        (600, 600, "attended"),  # floor cleared, full coverage
        (1800, 3600, "attended"),  # exactly 50% qualifies
        (1799, 3600, "not_attended"),  # one second short of half
        (1800, 3601, "not_attended"),  # denominator one second larger
        (3599, 3600, "attended"),
        (3600, 3600, "attended"),
    ],
)
def test_attendance_status_is_exactly_the_documented_rule(
    cumulative_seconds: int, actual_seconds: int, expected: str
) -> None:
    assert (
        classroom_service.attendance_status(cumulative_seconds, actual_seconds)
        == expected
    )


def test_the_minimum_duration_floor_is_ten_connected_minutes() -> None:
    assert classroom_service.meets_minimum_duration(599) is False
    assert classroom_service.meets_minimum_duration(600) is True
    assert classroom_service.meets_minimum_duration(0) is False
    assert classroom_service.meets_minimum_duration(-30) is False


# --- teacher attendance read ------------------------------------------------------------


def test_attendance_roster_unions_enrollments_with_participants(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_enrolled_student: User,
    quiet_enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _join(session, other_enrolled_student, class_id, "conn-2")
    classroom_service.leave_class(session, other_enrolled_student, class_id, "conn-2")
    session.commit()

    # Attendance history survives a dropped enrollment (union, not filter).
    profile = _student_profile(session, other_enrolled_student)
    enrollment = learning_enrollment_service.list_my_enrollments(session, profile)[0]
    learning_enrollment_service.leave(session, profile, enrollment.enrollment_id)
    session.commit()

    rows, auto_ended = classroom_service.get_class_attendance(
        session, approved_teacher, offering_id, class_id
    )

    assert auto_ended is False
    assert [r.full_name for r in rows] == [
        "Outsider Student",  # has segments, enrollment dropped
        "Quiet Student",  # enrolled, never joined
        "Test Student",  # joined and still inside
    ]
    by_name = {r.full_name: r for r in rows}

    outsider = by_name["Outsider Student"]
    assert outsider.online is False
    assert len(outsider.segments) == 1
    assert outsider.segments[0].left_at is not None

    quiet = by_name["Quiet Student"]
    assert quiet.online is False
    assert quiet.cumulative_seconds == 0
    assert quiet.segments == []
    assert quiet.attendance_status == "not_attended"

    inside = by_name["Test Student"]
    assert inside.online is True
    assert len(inside.segments) == 1
    assert inside.segments[0].left_at is None
    assert inside.attendance_status == "not_attended"  # class has barely begun


def test_attendance_is_derived_from_segments_and_clamped_to_the_class_window(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_enrolled_student: User,
    quiet_enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _join(session, other_enrolled_student, class_id, "conn-2")
    online_class_service.end_my_class(session, approved_teacher, offering_id, class_id)
    session.commit()

    # Controlled reality: a class that ran exactly 60 minutes, three hours ago.
    session.expire_all()
    start_of_class = datetime.now(timezone.utc) - timedelta(hours=3)
    class_session = session.get(OnlineClassSession, class_id)
    class_session.actual_started_at = start_of_class
    class_session.actual_ended_at = start_of_class + timedelta(minutes=60)

    segments = {s.connection_id: s for s in _segments(session, class_id)}
    # Connected 10 minutes BEFORE the class started: credit starts at start.
    early = segments["conn-1"]
    early.joined_at = start_of_class - timedelta(minutes=10)
    early.last_seen_at = start_of_class + timedelta(minutes=30)
    early.left_at = start_of_class + timedelta(minutes=30)
    # Connected only AFTER the class ended: no credit at all.
    late = segments["conn-2"]
    late.joined_at = start_of_class + timedelta(minutes=60)
    late.last_seen_at = start_of_class + timedelta(minutes=70)
    late.left_at = start_of_class + timedelta(minutes=70)
    session.commit()

    early_row, auto_ended = classroom_service.get_attendance_for_student(
        session, _student_profile(session, enrolled_student), class_id
    )
    assert auto_ended is False
    assert early_row.cumulative_seconds == 1800  # clamped: [start, start+30min]
    assert early_row.actual_seconds == 3600
    assert early_row.attendance_status == "attended"  # exactly 50%, floor cleared
    assert early_row.online is False
    assert len(early_row.segments) == 1
    assert early_row.segments[0].left_at == early.left_at

    late_row, _ = classroom_service.get_attendance_for_student(
        session, _student_profile(session, other_enrolled_student), class_id
    )
    assert late_row.cumulative_seconds == 0  # everything was outside the window
    assert late_row.attendance_status == "not_attended"

    rows, _ = classroom_service.get_class_attendance(
        session, approved_teacher, offering_id, class_id
    )
    assert [r.full_name for r in rows] == [
        "Outsider Student",
        "Quiet Student",
        "Test Student",
    ]
    for row in rows:
        assert row.actual_seconds == 3600  # one denominator, recomputed per row


def test_attendance_reads_are_refused_before_the_class_has_started(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()

    expected = (
        "attendance is only available once the class has started; "
        "status is 'scheduled'"
    )
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.get_class_attendance(
            session, approved_teacher, offering_id, created.class_id
        )
    session.rollback()
    assert str(excinfo.value) == expected

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.get_attendance_for_student(
            session, _student_profile(session, enrolled_student), created.class_id
        )
    session.rollback()
    assert str(excinfo.value) == expected


def test_a_students_own_attendance_needs_enrollment_or_participation(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    profile = _student_profile(session, enrolled_student)

    # ACTIVE enrollment alone is enough (no segment yet).
    row, _ = classroom_service.get_attendance_for_student(session, profile, class_id)
    assert row.cumulative_seconds == 0
    assert row.attendance_status == "not_attended"

    # Participation keeps the history readable after the enrollment ends.
    _join(session, enrolled_student, class_id, "conn-1")
    classroom_service.leave_class(session, enrolled_student, class_id, "conn-1")
    session.commit()
    enrollment = learning_enrollment_service.list_my_enrollments(session, profile)[0]
    learning_enrollment_service.leave(session, profile, enrollment.enrollment_id)
    session.commit()

    row, _ = classroom_service.get_attendance_for_student(session, profile, class_id)
    assert len(row.segments) == 1

    # No enrollment, no participation: SAME 404 as a completely unknown id.
    outsider = _student_profile(session, other_student)
    for target in (class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            classroom_service.get_attendance_for_student(session, outsider, target)
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {target}"


# --- teacher participant roster ------------------------------------------------------------


def test_participant_roster_is_empty_until_someone_joins(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()

    participants, auto_ended = classroom_service.list_class_participants(
        session, approved_teacher, offering_id, created.class_id
    )
    assert participants == []
    assert auto_ended is False

    online_class_service.start_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    session.commit()
    class_id = created.class_id
    _join(session, enrolled_student, class_id, "conn-1")
    classroom_service.leave_class(session, enrolled_student, class_id, "conn-1")
    _join(session, enrolled_student, class_id, "conn-2")
    session.commit()

    participants, _ = classroom_service.list_class_participants(
        session, approved_teacher, offering_id, class_id
    )
    assert len(participants) == 1
    roster = participants[0]
    assert roster.student_id == _student_profile(session, enrolled_student).id
    assert roster.full_name == "Test Student"
    assert roster.online is True  # conn-2 is open again
    assert roster.segment_count == 2  # reconnect history is kept
    assert roster.first_joined_at is not None
    assert roster.last_seen_at is not None


def test_participant_roster_scopes_to_your_classes_with_one_404(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    student: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
) -> None:
    foreign = _create(session, other_teacher, other_offering_id)
    session.commit()

    for target in (foreign.class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            classroom_service.list_class_participants(
                session, approved_teacher, offering_id, target
            )
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {target}"

    # A student calling the teacher surface is refused by the role guard.
    with pytest.raises(LearningForbiddenError) as excinfo:
        classroom_service.list_class_participants(
            session, student, offering_id, foreign.class_id
        )
    session.rollback()
    assert excinfo.value.status_code == 403
    assert "teacher role required" in str(excinfo.value)


# --- transcript (historical, ENDED only) -----------------------------------------------


def _end(session: Session, teacher: User, offering: uuid.UUID, class_id: uuid.UUID) -> None:
    """Take a LIVE class to ENDED (commits) — the transcript's precondition."""
    online_class_service.end_my_class(session, teacher, offering, class_id)
    session.commit()


def test_transcript_pages_by_exclusive_cursor_and_never_exposes_an_email(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _seed_message(session, class_id, enrolled_student, 1, "hello")
    _seed_message(session, class_id, approved_teacher, 2, "welcome")
    _seed_message(session, class_id, enrolled_student, 3, "on my way")
    _end(session, approved_teacher, offering_id, class_id)
    session.expire_all()  # server-default created_at becomes readable

    profile = _student_profile(session, enrolled_student)
    messages, auto_ended = classroom_service.get_transcript_for_student(
        session, profile, class_id
    )
    assert auto_ended is False
    assert [m.sequence for m in messages] == [1, 2, 3]  # sequence ASC, the only order
    assert messages[0].sender.role == "student"
    assert messages[0].sender.display_name == "Test Student"
    assert messages[1].sender.role == "teacher"
    assert messages[1].sender.display_name == "Test Teacher"
    assert messages[0].body == "hello"

    # Exactly the presentation-safe payload — nothing else travels.
    assert set(messages[0].model_dump()) == {
        "message_id",
        "sequence",
        "sender",
        "body",
        "sent_at",
    }
    assert set(messages[0].sender.model_dump()) == {"role", "display_name"}

    # The cursor is EXCLUSIVE: replay the last sequence and nothing duplicates.
    page, _ = classroom_service.get_transcript_for_student(
        session, profile, class_id, after_sequence=1
    )
    assert [m.sequence for m in page] == [2, 3]
    tail, _ = classroom_service.get_transcript_for_student(
        session, profile, class_id, after_sequence=3
    )
    assert tail == []
    head, _ = classroom_service.get_transcript_for_student(
        session, profile, class_id, limit=1
    )
    assert [m.sequence for m in head] == [1]

    dump = json.dumps([m.model_dump(mode="json") for m in messages], default=str)
    assert "@example.com" not in dump  # display names only, never an address
    assert "connection" not in dump
    assert "client_message_id" not in dump  # the retry key is not part of the record


def test_enrollment_alone_never_grants_the_transcript(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_student: User,
) -> None:
    """§4/§12: ACTIVE enrollment without a segment is never enough."""
    class_id = _start(session, approved_teacher, offering_id)
    _end(session, approved_teacher, offering_id, class_id)

    # Enrolled, class ended, never joined: SAME 404 as an unknown id.
    never_joined = _student_profile(session, enrolled_student)
    for target in (class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            classroom_service.get_transcript_for_student(session, never_joined, target)
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {target}"

    # Not even enrolled: the id confirms nothing either.
    outsider = _student_profile(session, other_student)
    for target in (class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            classroom_service.get_transcript_for_student(session, outsider, target)
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {target}"


def test_student_transcript_is_refused_until_the_class_has_ended(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    """§5/§6: scheduled, live and cancelled classes have no HTTP transcript."""
    profile = _student_profile(session, enrolled_student)
    expected = "the class transcript is only available once the class has ended"

    # SCHEDULED: a participation row (only possible while live) still does
    # not make a class that has not occurred historical.
    scheduled = _create(session, approved_teacher, offering_id)
    session.add(
        ClassAttendanceSegment(
            class_session_id=scheduled.class_id,
            student_id=profile.id,
            connection_id="conn-early",
            joined_at=datetime.now(timezone.utc),
        )
    )
    session.commit()
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.get_transcript_for_student(session, profile, scheduled.class_id)
    session.rollback()
    assert expected in str(excinfo.value)
    assert "status is 'scheduled'" in str(excinfo.value)

    # LIVE: participation exists, but the live classroom reads through the
    # WebSocket (slices 3C/3D) — this endpoint is historical only.
    live_id = _start(session, approved_teacher, offering_id, offset=4)
    _join(session, enrolled_student, live_id, "conn-1")
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.get_transcript_for_student(session, profile, live_id)
    session.rollback()
    assert expected in str(excinfo.value)
    assert "status is 'live'" in str(excinfo.value)

    # CANCELLED: never admitted anyone, so no transcript either.
    cancelled = _create(session, approved_teacher, offering_id, *_window(8))
    online_class_service.cancel_my_class(
        session, approved_teacher, offering_id, cancelled.class_id
    )
    session.add(
        ClassAttendanceSegment(
            class_session_id=cancelled.class_id,
            student_id=profile.id,
            connection_id="conn-cancelled",
            joined_at=datetime.now(timezone.utc),
        )
    )
    session.commit()
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.get_transcript_for_student(
            session, profile, cancelled.class_id
        )
    session.rollback()
    assert expected in str(excinfo.value)
    assert "status is 'cancelled'" in str(excinfo.value)

    # ENDED: the very same call now succeeds (empty transcript is a 200).
    _end(session, approved_teacher, offering_id, live_id)
    messages, _ = classroom_service.get_transcript_for_student(session, profile, live_id)
    assert messages == []  # nothing seeded — authorized, just empty


def test_the_owning_teacher_reads_the_full_transcript_of_an_ended_class(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    """§2/§15: offering ownership IS the teacher's entitlement — no segment."""
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _seed_message(session, class_id, enrolled_student, 1, "hello")
    _seed_message(session, class_id, approved_teacher, 2, "welcome")
    _end(session, approved_teacher, offering_id, class_id)
    session.expire_all()

    messages, auto_ended = classroom_service.get_transcript_for_teacher(
        session, approved_teacher, offering_id, class_id
    )
    assert auto_ended is False
    assert [m.sequence for m in messages] == [1, 2]  # full transcript, both senders
    assert messages[0].sender.display_name == "Test Student"
    assert messages[1].sender.display_name == "Test Teacher"

    page, _ = classroom_service.get_transcript_for_teacher(
        session, approved_teacher, offering_id, class_id, after_sequence=1
    )
    assert [m.sequence for m in page] == [2]

    dump = json.dumps([m.model_dump(mode="json") for m in messages], default=str)
    assert "@example.com" not in dump


def test_teacher_transcript_access_is_scoped_and_role_guarded(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    student: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
) -> None:
    foreign = _create(session, other_teacher, other_offering_id)
    session.commit()

    # Foreign class under MY offering path, or an unknown id: SAME 404.
    for target in (foreign.class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            classroom_service.get_transcript_for_teacher(
                session, approved_teacher, offering_id, target
            )
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {target}"

    # A student never reaches the teacher surface: 403 from the role guard.
    with pytest.raises(LearningForbiddenError) as excinfo:
        classroom_service.get_transcript_for_teacher(
            session, student, offering_id, foreign.class_id
        )
    session.rollback()
    assert excinfo.value.status_code == 403
    assert "teacher role required" in str(excinfo.value)


def test_teacher_transcript_is_refused_before_the_class_has_ended(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    expected = "the class transcript is only available once the class has ended"

    scheduled = _create(session, approved_teacher, offering_id)
    session.commit()
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.get_transcript_for_teacher(
            session, approved_teacher, offering_id, scheduled.class_id
        )
    session.rollback()
    assert expected in str(excinfo.value)
    assert "status is 'scheduled'" in str(excinfo.value)

    live_id = _start(session, approved_teacher, offering_id, offset=4)
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.get_transcript_for_teacher(
            session, approved_teacher, offering_id, live_id
        )
    session.rollback()
    assert "status is 'live'" in str(excinfo.value)

    cancelled = _create(session, approved_teacher, offering_id, *_window(8))
    online_class_service.cancel_my_class(
        session, approved_teacher, offering_id, cancelled.class_id
    )
    session.commit()
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.get_transcript_for_teacher(
            session, approved_teacher, offering_id, cancelled.class_id
        )
    session.rollback()
    assert "status is 'cancelled'" in str(excinfo.value)

    # ENDED flips the gate: the same class reads fine (empty is a read).
    _end(session, approved_teacher, offering_id, live_id)
    messages, _ = classroom_service.get_transcript_for_teacher(
        session, approved_teacher, offering_id, live_id
    )
    assert messages == []


def test_participation_preserves_transcript_access_after_the_enrollment_ends(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    """§14: past participation preserves historical access; nothing revokes it."""
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    _end(session, approved_teacher, offering_id, class_id)

    profile = _student_profile(session, enrolled_student)
    enrollment = learning_enrollment_service.list_my_enrollments(session, profile)[0]
    learning_enrollment_service.leave(session, profile, enrollment.enrollment_id)
    session.commit()

    # The class they attended stays readable — enrollment is history now.
    messages, _ = classroom_service.get_transcript_for_student(session, profile, class_id)
    assert messages == []  # authorized (no exception), just nothing seeded

    # ...but a later class of the SAME offering they never joined stays closed.
    later = _start(session, approved_teacher, offering_id, offset=4)
    _end(session, approved_teacher, offering_id, later)
    with pytest.raises(LearningNotFoundError) as excinfo:
        classroom_service.get_transcript_for_student(session, profile, later)
    session.rollback()
    assert str(excinfo.value) == f"no online class with id {later}"


def test_a_sender_without_a_profile_degrades_to_a_placeholder(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")

    # A student account whose profile row is gone must not leak the address.
    ghost = User(
        email=f"ghost-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(ghost)
    session.flush()
    _seed_message(session, class_id, ghost, 1, "from the void")
    _end(session, approved_teacher, offering_id, class_id)
    session.expire_all()

    messages, _ = classroom_service.get_transcript_for_student(
        session, _student_profile(session, enrolled_student), class_id
    )
    assert messages[0].sender.role == "student"
    assert messages[0].sender.display_name == "Unknown participant"
    assert ghost.email not in json.dumps(
        [m.model_dump(mode="json") for m in messages], default=str
    )


# --- sending (slice 3D: durable, ordered, idempotent) --------------------------------


@pytest.fixture(autouse=True)
def _fresh_rate_quota():
    """Every test starts with a full message quota (the limiter is global)."""
    message_rate_limiter.reset()
    yield
    message_rate_limiter.reset()


def _rows(session: Session, class_id: uuid.UUID) -> list[ClassMessage]:
    stmt = (
        select(ClassMessage)
        .where(ClassMessage.class_session_id == class_id)
        .order_by(ClassMessage.sequence)
    )
    return list(session.scalars(stmt))


def test_a_live_class_commits_messages_in_sequence_and_returns_the_event(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    for expected, body in enumerate(("first", "second", "third"), start=1):
        outcome, event = classroom_service.send_class_message(
            session, enrolled_student, class_id, f"cid-{expected}", body
        )
        assert outcome == "created"
        assert event["type"] == "message.created"
        assert event["sequence"] == expected
        assert event["class_id"] == str(class_id)  # routing key, stripped at the wire
        assert event["sender"] == {"role": "student", "display_name": "Test Student"}
        assert event["body"] == body
        assert event["client_message_id"] == f"cid-{expected}"
        assert event["sent_at"].endswith("+00:00") or "Z" in event["sent_at"]
        session.commit()

    rows = _rows(session, class_id)
    assert [r.sequence for r in rows] == [1, 2, 3]  # contiguous, per-class, from 1
    assert [r.body for r in rows] == ["first", "second", "third"]
    assert all(r.sender_user_id == enrolled_student.id for r in rows)
    assert all(r.client_message_id == f"cid-{i + 1}" for i, r in enumerate(rows))


def test_a_retry_returns_the_canonical_message_without_a_second_row(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    """Same key + same body ? the SAME message, no new sequence, no fan-out."""
    class_id = _start(session, approved_teacher, offering_id)

    first_outcome, first = classroom_service.send_class_message(
        session, enrolled_student, class_id, "retry-me", "hello"
    )
    session.commit()
    retry_outcome, retry = classroom_service.send_class_message(
        session, enrolled_student, class_id, "retry-me", "hello"
    )
    session.commit()

    assert (first_outcome, retry_outcome) == ("created", "duplicate")
    assert retry["message_id"] == first["message_id"]
    assert retry["sequence"] == first["sequence"] == 1
    assert _rows(session, class_id) and len(_rows(session, class_id)) == 1


def test_the_same_key_with_a_different_body_is_refused_not_replaced(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    classroom_service.send_class_message(
        session, enrolled_student, class_id, "cid-1", "the original"
    )
    session.commit()

    with pytest.raises(classroom_service.MessageRejected) as excinfo:
        classroom_service.send_class_message(
            session, enrolled_student, class_id, "cid-1", "a rewritten body"
        )
    session.rollback()
    assert excinfo.value.code == cc.ERROR_CLIENT_MESSAGE_ID_CONFLICT
    rows = _rows(session, class_id)
    assert [r.body for r in rows] == ["the original"]  # never silently replaced


def test_a_send_is_refused_once_the_class_is_not_live(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    scheduled = _create(session, approved_teacher, offering_id)
    with pytest.raises(classroom_service.MessageRejected) as excinfo:
        classroom_service.send_class_message(
            session, enrolled_student, scheduled.class_id, "cid-1", "too early"
        )
    session.rollback()
    assert excinfo.value.code == cc.ERROR_CLASS_NOT_LIVE
    assert _rows(session, scheduled.class_id) == []

    live = _start(session, approved_teacher, offering_id, offset=4)
    _end(session, approved_teacher, offering_id, live)
    with pytest.raises(classroom_service.MessageRejected) as excinfo:
        classroom_service.send_class_message(
            session, enrolled_student, live, "cid-1", "too late"
        )
    session.rollback()
    assert excinfo.value.code == cc.ERROR_CLASS_NOT_LIVE
    assert _rows(session, live) == []


def test_sending_rechecks_authorization_on_every_message(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_student: User,
) -> None:
    """A ticket is not permanent authorization (�9): each send is rechecked."""
    class_id = _start(session, approved_teacher, offering_id)

    # Not enrolled in this offering: same refusal as an unknown class.
    with pytest.raises(classroom_service.MessageRejected) as excinfo:
        classroom_service.send_class_message(
            session, other_student, class_id, "cid-1", "outsider"
        )
    session.rollback()
    assert excinfo.value.code == cc.ERROR_NOT_AUTHORIZED

    # Another teacher does not own this class.
    with pytest.raises(classroom_service.MessageRejected) as excinfo:
        classroom_service.send_class_message(
            session, other_teacher, class_id, "cid-1", "not mine"
        )
    session.rollback()
    assert excinfo.value.code == cc.ERROR_NOT_AUTHORIZED

    # ...while the participants really inside the class do send.
    assert classroom_service.send_class_message(
        session, enrolled_student, class_id, "cid-1", "hi"
    )[0] == "created"
    assert classroom_service.send_class_message(
        session, approved_teacher, class_id, "cid-t", "welcome"
    )[0] == "created"
    session.commit()
    assert [r.sequence for r in _rows(session, class_id)] == [1, 2]


def test_a_send_never_creates_or_extends_attendance(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    """Authorization for a message must not have join side effects (�5)."""
    class_id = _start(session, approved_teacher, offering_id)

    classroom_service.send_class_message(
        session, enrolled_student, class_id, "cid-1", "talking without joining"
    )
    session.commit()
    assert _segments(session, class_id) == []  # no segment invented by a send

    segment = _join(session, enrolled_student, class_id, "conn-1")
    classroom_service.send_class_message(
        session, enrolled_student, class_id, "cid-2", "now joined"
    )
    session.commit()
    session.refresh(segment)
    assert segment.left_at is None  # and an open segment is untouched
    assert len(_segments(session, class_id)) == 1


def test_an_overdue_live_class_is_ended_and_refuses_the_send(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _force_overdue(session, class_id)

    with pytest.raises(classroom_service.MessageRejected) as excinfo:
        classroom_service.send_class_message(
            session, enrolled_student, class_id, "cid-1", "still here?"
        )
    # The sweep found along the way is committed by the caller (�53)...
    session.commit()
    assert excinfo.value.code == cc.ERROR_CLASS_NOT_LIVE
    row = session.get(OnlineClassSession, class_id)
    assert row.status == "ended"  # ...so the class really did end
    assert _rows(session, class_id) == []


def test_the_quota_counts_new_messages_but_never_a_retry(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    limit = get_settings().WS_MESSAGE_RATE_LIMIT

    for index in range(limit):
        assert classroom_service.send_class_message(
            session, enrolled_student, class_id, f"cid-{index}", f"msg {index}"
        )[0] == "created"
    session.commit()

    with pytest.raises(classroom_service.MessageRejected) as excinfo:
        classroom_service.send_class_message(
            session, enrolled_student, class_id, "cid-overflow", "one too many"
        )
    session.rollback()
    assert excinfo.value.code == cc.ERROR_RATE_LIMITED

    # A retry still gets its canonical acknowledgement past the quota,
    # because idempotency is answered BEFORE the quota (�10).
    outcome, _ = classroom_service.send_class_message(
        session, enrolled_student, class_id, "cid-0", "msg 0"
    )
    assert outcome == "duplicate"
    assert len(_rows(session, class_id)) == limit  # exactly 10 rows survive


def test_a_payload_beyond_the_notify_cap_is_refused_before_any_write(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    # 2000 astral-plane characters are 8000 UTF-8 bytes on their own �
    # the body limit is characters, the bus limit is bytes (�41).
    huge = "\U0001F600" * 2000
    with pytest.raises(classroom_service.MessageRejected) as excinfo:
        classroom_service.send_class_message(
            session, enrolled_student, class_id, "cid-big", huge
        )
    session.rollback()
    assert excinfo.value.code == cc.ERROR_INVALID_MESSAGE
    assert _rows(session, class_id) == []  # refused before insert AND publish

    # The maximum ordinary body still fits comfortably.
    ascii_body = "x" * 2000
    assert classroom_service.send_class_message(
        session, enrolled_student, class_id, "cid-ok", ascii_body
    )[0] == "created"
    session.commit()
    assert _rows(session, class_id)[0].body == ascii_body


# --- live recovery (slice 3D: the cursor read for reconnects) ------------------------


def test_live_recovery_reads_after_the_cursor_for_an_enrolled_student(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    for index in (1, 2, 3):
        classroom_service.send_class_message(
            session, enrolled_student, class_id, f"cid-{index}", f"m{index}"
        )
    session.commit()

    profile = _student_profile(session, enrolled_student)
    messages, auto_ended = classroom_service.read_messages_for_student(
        session, profile, class_id
    )
    assert auto_ended is False
    assert [m.sequence for m in messages] == [1, 2, 3]

    # EXCLUSIVE cursor + bounded page: replay the last seen sequence.
    page, _ = classroom_service.read_messages_for_student(
        session, profile, class_id, after_sequence=1, limit=1
    )
    assert [m.sequence for m in page] == [2]
    tail, _ = classroom_service.read_messages_for_student(
        session, profile, class_id, after_sequence=3
    )
    assert tail == []

    dump = json.dumps([m.model_dump(mode="json") for m in messages], default=str)
    assert "@example.com" not in dump
    assert "client_message_id" not in dump  # the retry key is not part of a record
    assert set(messages[0].model_dump()) == {
        "message_id",
        "sequence",
        "sender",
        "body",
        "sent_at",
    }


def test_live_recovery_never_answers_a_class_id_alone(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_student: User,
) -> None:
    """No ACTIVE enrollment ? the SAME 404 as an unknown class (L6)."""
    class_id = _start(session, approved_teacher, offering_id)
    outsider = _student_profile(session, other_student)

    for target in (class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            classroom_service.read_messages_for_student(session, outsider, target)
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {target}"


def test_ended_recovery_keeps_the_participation_policy_verbatim(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    quiet_enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _join(session, enrolled_student, class_id, "conn-1")
    classroom_service.send_class_message(
        session, enrolled_student, class_id, "cid-1", "said during class"
    )
    session.commit()
    _end(session, approved_teacher, offering_id, class_id)
    session.expire_all()

    profile = _student_profile(session, enrolled_student)
    enrollment = learning_enrollment_service.list_my_enrollments(session, profile)[0]
    learning_enrollment_service.leave(session, profile, enrollment.enrollment_id)
    session.commit()

    # Participation, not enrollment, is the entitlement after the end.
    messages, auto_ended = classroom_service.read_messages_for_student(
        session, profile, class_id
    )
    assert auto_ended is False
    assert [m.sequence for m in messages] == [1]

    # Enrolled but never joined: the SAME 404 as an unknown class.
    quiet = _student_profile(session, quiet_enrolled_student)
    for target in (class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            classroom_service.read_messages_for_student(session, quiet, target)
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {target}"


def test_scheduled_and_cancelled_recovery_is_a_conflict(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    profile = _student_profile(session, enrolled_student)

    scheduled = _create(session, approved_teacher, offering_id)
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.read_messages_for_student(session, profile, scheduled.class_id)
    session.rollback()
    assert "status is 'scheduled'" in str(excinfo.value)

    live = _start(session, approved_teacher, offering_id, offset=4)
    cancelled = _create(session, approved_teacher, offering_id, *_window(8))
    online_class_service.cancel_my_class(
        session, approved_teacher, offering_id, cancelled.class_id
    )
    session.commit()
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.read_messages_for_student(session, profile, cancelled.class_id)
    session.rollback()
    assert "status is 'cancelled'" in str(excinfo.value)

    # A live class of the same offering answers � this read IS for
    # reconnects, unlike the historical transcript.
    messages, _ = classroom_service.read_messages_for_student(session, profile, live)
    assert messages == []


def test_recovery_reads_never_create_messages(
    session: Session, approved_teacher: User, offering_id: uuid.UUID, enrolled_student: User
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    profile = _student_profile(session, enrolled_student)

    for _ in range(3):
        classroom_service.read_messages_for_student(session, profile, class_id)
    session.commit()
    assert _rows(session, class_id) == []
    assert session.scalar(
        select(ClassMessage).where(ClassMessage.class_session_id == class_id)
    ) is None


def test_teacher_live_recovery_is_owner_scoped(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    classroom_service.send_class_message(
        session, approved_teacher, class_id, "cid-1", "teaching"
    )
    session.commit()

    # The owning teacher reads a LIVE class � the whole point of 3D.
    messages, auto_ended = classroom_service.read_messages_for_teacher(
        session, approved_teacher, offering_id, class_id
    )
    assert auto_ended is False
    assert [m.sequence for m in messages] == [1]
    assert messages[0].sender.role == "teacher"

    # Another teacher gets the SAME 404 as an unknown class (L6).
    for target in (class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError):
            classroom_service.read_messages_for_teacher(
                session, other_teacher, other_offering_id, target
            )
        session.rollback()

    # A scheduled class has no classroom history (409), a cancelled one
    # neither � and the cursor stays exclusive and ordered.
    scheduled = _create(session, approved_teacher, offering_id, *_window(6))
    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.read_messages_for_teacher(
            session, approved_teacher, offering_id, scheduled.class_id
        )
    session.rollback()
    assert "status is 'scheduled'" in str(excinfo.value)

    _end(session, approved_teacher, offering_id, class_id)
    ended, _ = classroom_service.read_messages_for_teacher(
        session, approved_teacher, offering_id, class_id, after_sequence=1
    )
    assert ended == []
