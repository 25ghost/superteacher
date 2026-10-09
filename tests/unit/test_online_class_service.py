"""Unit tests: online classes and their lifecycle (Phase 3, slice 3A).

In-memory SQLite, no HTTP — the service layer's own contract:

- only the teacher who owns an offering may schedule/operate its classes;
  a foreign offering/class id answers the same 404 as an unknown one (L6
  existence leak), never a 403 that would confirm the row exists;
- scheduling requires an APPROVED teacher and an active offering;
- overlapping scheduled/live windows of the SAME offering are refused,
  while adjacent windows (end == next start) and cancelled/ended classes
  free their window again;
- the state machine ``scheduled -> live -> ended`` and
  ``scheduled -> cancelled`` is the ONLY legal path: every other move is a
  409, and a window that already closed can never go live;
- an overdue ``live`` class is ended mechanically (reason: "auto") by the
  read paths and by ``ensure_not_overdue``, so nobody has to press End;
- a student's class visibility is exactly their ACTIVE enrollments:
  unknown and unauthorized answer the same 404;
- every lifecycle mutation writes exactly one ``auth_events`` row with the
  documented event type.
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
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
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
from app.schemas.curriculum import LessonCreate, TopicCreate
from app.schemas.learning import TeachingOfferingCreate, TeachingOfferingUpdate
from app.schemas.online_class import OnlineClassSessionCreate, OnlineClassSessionUpdate
from app.services import (
    curriculum_service,
    learning_enrollment_service,
    online_class_service,
    teaching_offering_service,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)

PASSWORD = "correct horse battery staple"

#: Windows are far in the future so "start" is legal against the real clock.
START = datetime(2027, 3, 1, 10, 0, tzinfo=timezone.utc)
END = datetime(2027, 3, 1, 11, 0, tzinfo=timezone.utc)


def _window(offset_hours: float, duration_hours: float = 1.0) -> tuple[datetime, datetime]:
    start = START + timedelta(hours=offset_hours)
    return start, start + timedelta(hours=duration_hours)


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


@pytest.fixture()
def enrolled_student(session: Session, student: User, offering_id: uuid.UUID) -> User:
    """A student with an ACTIVE enrollment in the tested offering."""
    profile = _student_profile(session, student)
    learning_enrollment_service.enroll(session, profile, offering_id)
    session.commit()
    return student


def _create(
    session: Session,
    teacher: User,
    offering: uuid.UUID,
    start: datetime = START,
    end: datetime = END,
    lesson_id: uuid.UUID | None = None,
):
    return online_class_service.create_my_class(
        session,
        teacher,
        offering,
        OnlineClassSessionCreate(
            scheduled_start_at=start,
            scheduled_end_at=end,
            lesson_id=lesson_id,
        ),
    )


def _events(session: Session, user: User) -> list[AuthEvent]:
    rows = session.scalars(select(AuthEvent).where(AuthEvent.user_id == user.id)).all()
    return [e for e in rows if e.event_type.startswith("online_class")]


# --- scheduling ------------------------------------------------------------------------


def test_teacher_schedules_a_class_and_it_is_audited(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    read = _create(session, approved_teacher, offering_id)
    session.commit()

    assert read.teaching_offering_id == offering_id
    assert read.status == "scheduled"
    assert read.scheduled_start_at == START
    assert read.scheduled_end_at == END
    assert read.actual_started_at is None
    assert read.actual_ended_at is None

    events = _events(session, approved_teacher)
    assert [e.event_type for e in events] == ["online_class_created"]
    metadata = json.loads(events[0].metadata_json)
    assert metadata["class_id"] == str(read.class_id)
    assert metadata["teaching_offering_id"] == str(offering_id)


def test_foreign_or_unknown_offering_is_the_same_conflict_free_404(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
) -> None:
    for target in (other_offering_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            _create(session, approved_teacher, target)
        message = str(excinfo.value)
        assert message.startswith("no teaching offering with id ")
        assert not any(word in message for word in ("other", "not yours", "teacher"))
        session.rollback()

    # The caller's own offering never received a class from these attempts.
    classes, _ = online_class_service.list_my_classes(
        session, approved_teacher, offering_id
    )
    assert classes == []


def test_unapproved_teacher_never_schedules_a_class(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    # The offering was created while approved; vetting later revokes writes.
    profile = session.scalar(
        select(Teacher).where(Teacher.user_id == approved_teacher.id)
    )
    profile.verification_status = TeacherVerificationStatus.PENDING.value
    session.commit()

    with pytest.raises(LearningForbiddenError) as excinfo:
        _create(session, approved_teacher, offering_id)
    message = str(excinfo.value)
    assert "approved teacher" in message
    assert str(offering_id) not in message
    session.rollback()

    # Reads of the existing timetable stay open.
    classes, _ = online_class_service.list_my_classes(
        session, approved_teacher, offering_id
    )
    assert classes == []


def test_non_teacher_caller_is_refused_by_the_role_guard(
    session: Session, student: User, offering_id: uuid.UUID
) -> None:
    with pytest.raises(LearningForbiddenError) as excinfo:
        _create(session, student, offering_id)
    assert excinfo.value.status_code == 403
    assert "teacher role required" in str(excinfo.value)
    session.rollback()


def test_overlapping_window_is_refused_but_adjacent_windows_are_allowed(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    first = _create(session, approved_teacher, offering_id)
    session.commit()

    # 10:00-11:00 exists -> 10:30-11:30 collides.
    with pytest.raises(LearningConflictError) as excinfo:
        _create(
            session,
            approved_teacher,
            offering_id,
            START + timedelta(minutes=30),
            END + timedelta(minutes=30),
        )
    session.rollback()
    assert "overlaps" in str(excinfo.value)

    # Back-to-back (end == next start) is a legal, non-overlapping window.
    adjacent = _create(
        session, approved_teacher, offering_id, END, END + timedelta(hours=1)
    )
    session.commit()
    assert adjacent.class_id != first.class_id

    listed, _ = online_class_service.list_my_classes(
        session, approved_teacher, offering_id
    )
    assert len(listed) == 2


def test_lesson_of_another_offering_is_a_conflict_free_404(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
) -> None:
    topic = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Algebra")
    )
    lesson = curriculum_service.create_lesson(
        session,
        approved_teacher,
        offering_id,
        topic.topic_id,
        LessonCreate(title="Linear equations"),
    )
    foreign_topic = curriculum_service.create_topic(
        session, other_teacher, other_offering_id, TopicCreate(title="Foreign")
    )
    foreign_lesson = curriculum_service.create_lesson(
        session,
        other_teacher,
        other_offering_id,
        foreign_topic.topic_id,
        LessonCreate(title="Foreign lesson"),
    )
    session.commit()

    read = _create(session, approved_teacher, offering_id, lesson_id=lesson.lesson_id)
    session.commit()
    assert read.lesson_id == lesson.lesson_id

    # A lesson that exists - but under ANOTHER offering - 404s like an unknown id.
    with pytest.raises(LearningNotFoundError) as excinfo:
        _create(
            session,
            approved_teacher,
            offering_id,
            lesson_id=foreign_lesson.lesson_id,
        )
    session.rollback()
    assert str(excinfo.value).startswith("no lesson with id ")


def test_paused_offering_refuses_new_classes(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    teaching_offering_service.update_my_offering(
        session, approved_teacher, offering_id, TeachingOfferingUpdate(status="paused")
    )
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        _create(session, approved_teacher, offering_id)
    session.rollback()
    assert "not accepting new classes" in str(excinfo.value)


def test_inverted_window_is_refused_before_the_service_is_reached() -> None:
    # The request schema rejects an inverted window outright (422 at the
    # endpoint); the service keeps its own copy of the rule for defense.
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        OnlineClassSessionCreate(scheduled_start_at=END, scheduled_end_at=START)
    assert "scheduled_end_at must be after scheduled_start_at" in str(excinfo.value)


# --- the state machine -----------------------------------------------------------------


def test_only_the_documented_transitions_are_ever_taken(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()

    # SCHEDULED -> CANCELLED (allowed), then every move out is a 409.
    cancelled = online_class_service.cancel_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    session.commit()
    assert cancelled.status == "cancelled"
    for action in ("start", "end"):
        with pytest.raises(LearningConflictError) as excinfo:
            getattr(online_class_service, f"{action}_my_class")(
                session, approved_teacher, offering_id, created.class_id
            )
        session.rollback()
        assert "cannot move from 'cancelled'" in str(excinfo.value)

    # A fresh scheduled class: SCHEDULED -> LIVE -> ENDED (allowed)...
    scheduled = _create(session, approved_teacher, offering_id, *_window(2))
    session.commit()
    live = online_class_service.start_my_class(
        session, approved_teacher, offering_id, scheduled.class_id
    )
    session.commit()
    assert live.status == "live"
    assert live.actual_started_at is not None

    # ...but a second Start and a Cancel of a LIVE class are both 409.
    with pytest.raises(LearningConflictError) as excinfo:
        online_class_service.start_my_class(
            session, approved_teacher, offering_id, scheduled.class_id
        )
    session.rollback()
    assert "cannot move from 'live' to 'live'" in str(excinfo.value)

    with pytest.raises(LearningConflictError) as excinfo:
        online_class_service.cancel_my_class(
            session, approved_teacher, offering_id, scheduled.class_id
        )
    session.rollback()
    assert "cannot move from 'live' to 'cancelled'" in str(excinfo.value)

    ended = online_class_service.end_my_class(
        session, approved_teacher, offering_id, scheduled.class_id
    )
    session.commit()
    assert ended.status == "ended"
    assert ended.actual_ended_at is not None

    # ENDED is terminal - no reopening, ever.
    for action in ("start", "cancel", "end"):
        with pytest.raises(LearningConflictError) as excinfo:
            getattr(online_class_service, f"{action}_my_class")(
                session, approved_teacher, offering_id, scheduled.class_id
            )
        session.rollback()
        assert "cannot move from 'ended'" in str(excinfo.value)

    # Exactly one audit row per successful transition, in order.
    assert [e.event_type for e in _events(session, approved_teacher)] == [
        "online_class_created",
        "online_class_cancelled",
        "online_class_created",
        "online_class_started",
        "online_class_ended",
    ]


def test_ending_a_scheduled_class_is_refused_until_it_is_started(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()
    with pytest.raises(LearningConflictError) as excinfo:
        online_class_service.end_my_class(
            session, approved_teacher, offering_id, created.class_id
        )
    session.rollback()
    assert "cannot move from 'scheduled' to 'ended'" in str(excinfo.value)


def test_a_window_that_already_closed_can_never_go_live(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    past_start = datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc)
    past_end = datetime(2020, 1, 1, 11, 0, tzinfo=timezone.utc)
    created = _create(session, approved_teacher, offering_id, past_start, past_end)
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        online_class_service.start_my_class(
            session, approved_teacher, offering_id, created.class_id
        )
    session.rollback()
    assert "already ended" in str(excinfo.value)


# --- amendment -------------------------------------------------------------------------


def test_only_a_scheduled_class_is_editable_and_self_overlap_is_excluded(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    first = _create(session, approved_teacher, offering_id)
    second = _create(session, approved_teacher, offering_id, *_window(4))
    session.commit()

    # Shifting the second window onto the first is a 409...
    with pytest.raises(LearningConflictError):
        online_class_service.update_my_class(
            session,
            approved_teacher,
            offering_id,
            second.class_id,
            OnlineClassSessionUpdate(scheduled_start_at=START, scheduled_end_at=END),
        )
    session.rollback()

    # ...while amending its OWN window is fine.
    updated = online_class_service.update_my_class(
        session,
        approved_teacher,
        offering_id,
        second.class_id,
        OnlineClassSessionUpdate(scheduled_end_at=END + timedelta(hours=6)),
    )
    session.commit()
    assert updated.scheduled_end_at == END + timedelta(hours=6)

    # Once live, every scheduling field is immutable (409, never a silent 200).
    live = online_class_service.start_my_class(
        session, approved_teacher, offering_id, first.class_id
    )
    session.commit()
    assert live.status == "live"
    with pytest.raises(LearningConflictError) as excinfo:
        online_class_service.update_my_class(
            session,
            approved_teacher,
            offering_id,
            first.class_id,
            OnlineClassSessionUpdate(scheduled_end_at=END + timedelta(hours=1)),
        )
    session.rollback()
    assert "immutable" in str(excinfo.value)


def test_inverted_effective_window_is_refused(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()
    with pytest.raises(LearningValidationError):
        online_class_service.update_my_class(
            session,
            approved_teacher,
            offering_id,
            created.class_id,
            OnlineClassSessionUpdate(scheduled_end_at=START - timedelta(hours=1)),
        )
    session.rollback()


def test_cancelling_a_class_frees_its_window_again(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()

    with pytest.raises(LearningConflictError):
        _create(session, approved_teacher, offering_id, START, END)
    session.rollback()

    online_class_service.cancel_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    session.commit()

    replacement = _create(session, approved_teacher, offering_id, START, END)
    session.commit()
    assert replacement.class_id != created.class_id


def test_ended_class_frees_its_window_again(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    created = _create(session, approved_teacher, offering_id)
    online_class_service.start_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    online_class_service.end_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    session.commit()

    replacement = _create(session, approved_teacher, offering_id, START, END)
    session.commit()
    assert replacement.status == "scheduled"


# --- automatic end ---------------------------------------------------------------------


def _force_live_and_overdue(session: Session, class_id: uuid.UUID) -> None:
    row = session.get(OnlineClassSession, class_id)
    row.status = "live"
    # Whole window into the past - keeps scheduled_end_at > scheduled_start_at.
    row.scheduled_start_at = datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc)
    row.scheduled_end_at = datetime(2020, 1, 1, 11, 0, tzinfo=timezone.utc)
    session.commit()


def test_overdue_live_class_is_ended_mechanically_on_the_next_read(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()
    _force_live_and_overdue(session, created.class_id)

    read, auto_ended = online_class_service.get_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    session.commit()  # the committing caller persists the mechanical transition

    assert auto_ended is True
    assert read.status == "ended"
    assert read.actual_ended_at is not None

    # A second read finds nothing left to do.
    again, auto_again = online_class_service.get_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    assert auto_again is False
    assert again.status == "ended"

    ends = [e for e in _events(session, approved_teacher) if e.event_type == "online_class_ended"]
    assert len(ends) == 1
    assert json.loads(ends[0].metadata_json)["reason"] == "auto"


def test_list_reads_also_finish_overdue_classes(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()
    _force_live_and_overdue(session, created.class_id)

    classes, auto_ended = online_class_service.list_my_classes(
        session, approved_teacher, offering_id
    )
    session.commit()
    assert auto_ended is True
    assert [c.status for c in classes] == ["ended"]


def test_a_scheduled_class_is_never_auto_ended(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    past_start = datetime(2020, 6, 1, 10, 0, tzinfo=timezone.utc)
    past_end = datetime(2020, 6, 1, 11, 0, tzinfo=timezone.utc)
    created = _create(session, approved_teacher, offering_id, past_start, past_end)
    session.commit()

    assert online_class_service.ensure_not_overdue(session, created.class_id) is False
    read, auto_ended = online_class_service.get_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    assert auto_ended is False
    assert read.status == "scheduled"


def test_a_due_scheduled_class_is_never_auto_started(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    """§44: only a teacher Start makes a class LIVE.

    A class whose start time arrived (even one that is already overdue)
    stays SCHEDULED on every read — no socket can ever observe a class
    that nobody started, and ``class.started`` is published by
    :func:`start_my_class` alone.
    """
    now = datetime.now(timezone.utc)
    created = _create(session, approved_teacher, offering_id, now - timedelta(hours=1), now + timedelta(hours=1))
    session.commit()

    assert online_class_service.ensure_not_overdue(session, created.class_id) is False
    read, auto_ended = online_class_service.get_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    assert auto_ended is False
    assert read.status == "scheduled"

    classes, _ = online_class_service.list_my_classes(session, approved_teacher, offering_id)
    assert [c.status for c in classes] == ["scheduled"]

    row = session.get(OnlineClassSession, created.class_id)
    assert row.actual_started_at is None
    started = [
        e for e in _events(session, approved_teacher)
        if e.event_type == "online_class_started"
    ]
    assert started == []


def test_ensure_not_overdue_is_a_noop_for_unknown_class(session: Session) -> None:
    assert online_class_service.ensure_not_overdue(session, uuid.uuid4()) is False


# --- student surface -------------------------------------------------------------------


def test_student_sees_exactly_their_enrolled_offerings_classes(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
    other_student: User,
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()

    enrolled_profile = _student_profile(session, enrolled_student)
    classes, auto_ended = online_class_service.list_classes_for_student(
        session, enrolled_profile
    )
    assert auto_ended is False
    assert [c.class_id for c in classes] == [created.class_id]

    read, _ = online_class_service.get_class_for_student(
        session, enrolled_profile, created.class_id
    )
    assert read.class_id == created.class_id

    # A student who never enrolled: empty list, and the class id - known or
    # not - is always the SAME 404 (no existence leak).
    outsider_profile = _student_profile(session, other_student)
    outsider_list, _ = online_class_service.list_classes_for_student(
        session, outsider_profile
    )
    assert outsider_list == []
    for target in (created.class_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError) as excinfo:
            online_class_service.get_class_for_student(session, outsider_profile, target)
        assert str(excinfo.value) == f"no online class with id {target}"


def test_student_visibility_shrinks_when_the_enrollment_ends(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    created = _create(session, approved_teacher, offering_id)
    session.commit()

    profile = _student_profile(session, enrolled_student)
    enrollment = learning_enrollment_service.list_my_enrollments(session, profile)[0]
    learning_enrollment_service.leave(session, profile, enrollment.enrollment_id)
    session.commit()

    classes, _ = online_class_service.list_classes_for_student(session, profile)
    assert classes == []
    with pytest.raises(LearningNotFoundError):
        online_class_service.get_class_for_student(session, profile, created.class_id)


def test_student_list_can_be_narrowed_to_one_offering(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    mine = _create(session, approved_teacher, offering_id)
    _create(session, other_teacher, other_offering_id)
    session.commit()

    profile = _student_profile(session, enrolled_student)
    classes, _ = online_class_service.list_classes_for_student(
        session, profile, teaching_offering_id=offering_id
    )
    assert [c.class_id for c in classes] == [mine.class_id]


# --- teacher read surface --------------------------------------------------------------


def test_teacher_reads_are_scoped_to_offerings_and_classes(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
) -> None:
    mine = _create(session, approved_teacher, offering_id)
    foreign = _create(session, other_teacher, other_offering_id)
    session.commit()

    classes, auto_ended = online_class_service.list_my_classes(
        session, approved_teacher, offering_id
    )
    assert auto_ended is False
    assert [c.class_id for c in classes] == [mine.class_id]

    # Foreign offering: 404, same as unknown.
    for target in (other_offering_id, uuid.uuid4()):
        with pytest.raises(LearningNotFoundError):
            online_class_service.list_my_classes(session, approved_teacher, target)
        session.rollback()

    # Another teacher's class id under MY offering path: 404.
    with pytest.raises(LearningNotFoundError) as excinfo:
        online_class_service.get_my_class(
            session, approved_teacher, offering_id, foreign.class_id
        )
    assert str(excinfo.value) == f"no online class with id {foreign.class_id}"
    session.rollback()


def test_writes_on_a_foreign_class_id_are_the_same_404(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
) -> None:
    foreign = _create(session, other_teacher, other_offering_id)
    session.commit()

    for attempt in (
        lambda: online_class_service.start_my_class(
            session, approved_teacher, offering_id, foreign.class_id
        ),
        lambda: online_class_service.end_my_class(
            session, approved_teacher, offering_id, foreign.class_id
        ),
        lambda: online_class_service.cancel_my_class(
            session, approved_teacher, offering_id, foreign.class_id
        ),
        lambda: online_class_service.update_my_class(
            session,
            approved_teacher,
            offering_id,
            foreign.class_id,
            OnlineClassSessionUpdate(scheduled_end_at=END),
        ),
    ):
        with pytest.raises(LearningNotFoundError) as excinfo:
            attempt()
        session.rollback()
        assert str(excinfo.value) == f"no online class with id {foreign.class_id}"
