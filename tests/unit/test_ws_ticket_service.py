"""Unit tests: WebSocket tickets, handshake authorization, transport bits.

In-memory SQLite, no HTTP, no sockets — the slice 3C service contract
that everything else stands on (§6, §10, §15, §16, §40):

- ticket issuance is a capability, not a role change: only an approved
  teacher owning the class, or a student with an ACTIVE enrollment, may
  mint one, only for a LIVE class (409 otherwise — scheduled, ended and
  cancelled alike), with unknown and foreign ids answering the SAME 404;
- the raw ticket is opaque ``token_urlsafe`` output — never a JWT, never
  stored (only its SHA-256 digest reaches the row), returned exactly once;
- consumption is ATOMIC single-use + class-scoped + expiry-checked in one
  UPDATE: the second attempt, the wrong class, the expired row and a
  stolen access token all collapse to ``None`` (one answer, §40);
- revalidation at handshake time rechecks everything that may have
  changed since issuance: teacher approval/ownership, student profile and
  enrollment, class still live — refusals carry transport-neutral reasons
  (not_authorized / class_not_live / duplicate_connection) the endpoint
  maps to close codes 4003 / 4008 / 1008;
- students enter THROUGH ``join_class`` (one open segment + one audit
  row), teachers create no segment at all, administrators are never
  classroom participants;
- the teacher heartbeat is the auto-end sweep without a segment to touch;
- the transport vocabulary itself — pinned close codes, deterministic
  opaque participant refs, the one-connection registry, and a publish
  that is a harmless local no-op on SQLite.
"""
from __future__ import annotations

import hashlib
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
from app.models.class_attendance_segment import ClassAttendanceSegment
from app.models.class_ws_ticket import ClassWsTicket
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    OnlineClassStatus,
    TeacherVerificationStatus,
    UserRole,
    UserStatus,
)
from app.models.learning_enrollment import LearningEnrollment
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
from app.realtime.connections import (
    ConnectionRegistry,
    LiveConnection,
    participant_ref,
)
from app.realtime.event_bus import RealtimeEventBus
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

PASSWORD = "correct horse battery staple"

#: Far-future windows so "start" stays legal against the real clock.
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


def _teacher_user(session: Session, verification: str, label: str) -> User:
    user = User(
        email=f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(user_id=user.id, full_name=label, verification_status=verification)
    )
    session.commit()
    return user


@pytest.fixture()
def approved_teacher(session: Session) -> User:
    return _teacher_user(
        session, TeacherVerificationStatus.APPROVED.value, "Ticket Teacher"
    )


@pytest.fixture()
def other_teacher(session: Session) -> User:
    return _teacher_user(
        session, TeacherVerificationStatus.APPROVED.value, "Other Teacher"
    )


@pytest.fixture()
def pending_teacher(session: Session) -> User:
    return _teacher_user(
        session, TeacherVerificationStatus.PENDING.value, "Pending Teacher"
    )


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
    return _student_user(session, "Ticket Student")


@pytest.fixture()
def other_student(session: Session) -> User:
    return _student_user(session, "Remote Student")


@pytest.fixture()
def quiet_student(session: Session) -> User:
    return _student_user(session, "Quiet Student")


@pytest.fixture()
def admin(session: Session) -> User:
    user = User(
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.ADMIN.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.commit()
    return user


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
    _enroll(session, student, offering_id)
    return student


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
    session: Session, teacher: User, offering: uuid.UUID, offset: float = 0.0
) -> uuid.UUID:
    created = _create(session, teacher, offering, *_window(offset))
    online_class_service.start_my_class(session, teacher, offering, created.class_id)
    session.commit()
    return created.class_id


def _segments(session: Session, class_id: uuid.UUID) -> list[ClassAttendanceSegment]:
    stmt = (
        select(ClassAttendanceSegment)
        .where(ClassAttendanceSegment.class_session_id == class_id)
        .order_by(ClassAttendanceSegment.connection_id)
    )
    return list(session.scalars(stmt))


def _force_overdue(session: Session, class_id: uuid.UUID) -> None:
    """Move the whole scheduled window into the past — overdue (§36)."""
    row = session.get(OnlineClassSession, class_id)
    row.scheduled_start_at = datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc)
    row.scheduled_end_at = datetime(2020, 1, 1, 11, 0, tzinfo=timezone.utc)
    session.commit()


def _tickets(session: Session) -> list[ClassWsTicket]:
    return list(session.scalars(select(ClassWsTicket)))


# --- issuance (§6) ---------------------------------------------------------------------


def test_teacher_mints_an_opaque_ticket_for_a_live_class_of_his_own(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    read = classroom_service.issue_ws_ticket_for_teacher(
        session, approved_teacher, offering_id, class_id
    )
    session.commit()

    assert read.ws_path == f"/ws/classes/{class_id}"
    # Opaque capability, not a signed token: no JWT structure to probe.
    assert "." not in read.ticket
    assert not read.ticket.startswith("eyJ")
    assert len(read.ticket) >= 40
    expires = read.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    ttl = expires - datetime.now(timezone.utc)
    assert timedelta(seconds=110) <= ttl <= timedelta(seconds=120)


def test_the_raw_ticket_is_never_stored_only_its_sha256_digest(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    read = classroom_service.issue_ws_ticket_for_teacher(
        session, approved_teacher, offering_id, class_id
    )
    session.commit()

    (row,) = _tickets(session)
    assert row.token_hash == hashlib.sha256(read.ticket.encode("utf-8")).hexdigest()
    assert len(row.token_hash) == 64
    assert set(row.token_hash) <= set("0123456789abcdef")
    assert read.ticket not in row.token_hash
    assert row.class_session_id == class_id
    assert row.user_id == approved_teacher.id
    assert row.used_at is None
    # Issuance alone never touches attendance (§11 step 2 is write-free).
    assert _segments(session, class_id) == []


def test_unapproved_teacher_cannot_mint(
    session: Session, pending_teacher: User, approved_teacher: User,
    offering_id: uuid.UUID,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    with pytest.raises(LearningForbiddenError) as excinfo:
        classroom_service.issue_ws_ticket_for_teacher(
            session, pending_teacher, offering_id, class_id
        )
    assert "only an approved teacher may enter the classroom" in str(excinfo.value)
    assert _tickets(session) == []


def test_a_foreign_class_is_the_same_404_as_an_unknown_one(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    other_teacher: User, other_offering_id: uuid.UUID,
) -> None:
    foreign_class = _start(session, other_teacher, other_offering_id)

    with pytest.raises(LearningNotFoundError) as excinfo:
        classroom_service.issue_ws_ticket_for_teacher(
            session, approved_teacher, offering_id, foreign_class
        )
    assert str(excinfo.value) == f"no online class with id {foreign_class}"


def test_a_non_live_class_is_never_ticketed(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    scheduled = _create(session, approved_teacher, offering_id)
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.issue_ws_ticket_for_teacher(
            session, approved_teacher, offering_id, scheduled.class_id
        )
    assert "participant ticket issuance requires a live class" in str(excinfo.value)

    online_class_service.start_my_class(
        session, approved_teacher, offering_id, scheduled.class_id
    )
    online_class_service.end_my_class(
        session, approved_teacher, offering_id, scheduled.class_id
    )
    session.commit()
    with pytest.raises(LearningConflictError):
        classroom_service.issue_ws_ticket_for_teacher(
            session, approved_teacher, offering_id, scheduled.class_id
        )
    assert _tickets(session) == []


def test_student_ticket_requires_active_enrollment_and_a_live_class(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User, other_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    read = classroom_service.issue_ws_ticket_for_student(
        session, _student_profile(session, enrolled_student), class_id
    )
    session.commit()
    assert read.ws_path == f"/ws/classes/{class_id}"

    with pytest.raises(LearningNotFoundError) as excinfo:
        classroom_service.issue_ws_ticket_for_student(
            session, _student_profile(session, other_student), class_id
        )
    assert str(excinfo.value) == f"no online class with id {class_id}"

    scheduled = _create(session, approved_teacher, offering_id, *_window(2))
    session.commit()
    with pytest.raises(LearningConflictError):
        classroom_service.issue_ws_ticket_for_student(
            session, _student_profile(session, enrolled_student), scheduled.class_id
        )


def test_a_ended_enrollment_loses_ticket_minting_but_keeps_history(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    profile = _student_profile(session, enrolled_student)
    enrollment = session.scalar(
        select(LearningEnrollment).where(LearningEnrollment.student_id == profile.id)
    )
    learning_enrollment_service.leave(session, profile, enrollment.id)
    session.commit()

    # §29: leaving is the revocation point — no new capability, even
    # though the participation history from before is untouched.
    with pytest.raises(LearningNotFoundError) as excinfo:
        classroom_service.issue_ws_ticket_for_student(session, profile, class_id)
    assert str(excinfo.value) == f"no online class with id {class_id}"


def test_a_student_of_another_offering_is_the_same_404_and_never_enters(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    other_teacher: User, other_offering_id: uuid.UUID, student: User,
) -> None:
    """§39: same subject, another teacher → no capability, no connection.

    The student is genuinely enrolled — just in the OTHER offering (the
    fixtures share one subject, so this is exactly "same subject, other
    teacher"). Both surfaces answer as if the class did not exist, and
    the refused handshake leaves no attendance behind.
    """
    _enroll(session, student, other_offering_id)
    class_id = _start(session, approved_teacher, offering_id)

    with pytest.raises(LearningNotFoundError) as excinfo:
        classroom_service.issue_ws_ticket_for_student(
            session, _student_profile(session, student), class_id
        )
    assert str(excinfo.value) == f"no online class with id {class_id}"
    assert _tickets(session) == []

    with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
        classroom_service.authorize_ws_connection(
            session, student, class_id, "conn-foreign-offering"
        )
    session.rollback()
    assert excinfo.value.reason == cc.REASON_NOT_AUTHORIZED
    assert _segments(session, class_id) == []


# --- consumption (§10, §40) ------------------------------------------------------------


def test_consumption_is_single_use_and_returns_the_ticket_holder(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    read = classroom_service.issue_ws_ticket_for_student(
        session, _student_profile(session, enrolled_student), class_id
    )
    session.commit()

    holder = classroom_service.consume_ws_ticket(session, class_id, read.ticket)
    session.commit()
    assert holder is not None and holder.id == enrolled_student.id

    again = classroom_service.consume_ws_ticket(session, class_id, read.ticket)
    session.commit()
    assert again is None

    (row,) = _tickets(session)
    assert row.used_at is not None


def test_consumption_binds_class_expiry_and_identity_together(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    other_class = _start(session, approved_teacher, offering_id, offset=4)
    read = classroom_service.issue_ws_ticket_for_student(
        session, _student_profile(session, enrolled_student), class_id
    )
    session.commit()

    # Wrong class: the UPDATE matches nothing and burns nothing (§40).
    assert (
        classroom_service.consume_ws_ticket(session, other_class, read.ticket) is None
    )
    assert (
        classroom_service.consume_ws_ticket(session, class_id, "not-a-ticket") is None
    )
    assert (
        classroom_service.consume_ws_ticket(session, class_id, read.ticket) is not None
    )

    # Expiry: flip the row into the past; the next redemption fails.
    expired = classroom_service.issue_ws_ticket_for_student(
        session, _student_profile(session, enrolled_student), other_class
    )
    session.commit()
    row = session.scalar(
        select(ClassWsTicket).where(ClassWsTicket.token_hash == hashlib.sha256(
            expired.ticket.encode("utf-8")
        ).hexdigest())
    )
    row.expires_at = datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc)
    session.commit()
    assert (
        classroom_service.consume_ws_ticket(session, other_class, expired.ticket)
        is None
    )


def test_an_access_token_is_not_a_ticket(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    stolen = security.create_access_token(approved_teacher.id, "teacher")

    assert classroom_service.consume_ws_ticket(session, class_id, stolen) is None
    assert _tickets(session) == []


def test_a_refresh_token_is_not_a_ticket(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    """§45: the refresh JWT is an HTTP credential, never a socket one."""
    class_id = _start(session, approved_teacher, offering_id)
    stolen = security.create_refresh_token(approved_teacher.id, uuid.uuid4())

    assert classroom_service.consume_ws_ticket(session, class_id, stolen) is None
    assert _tickets(session) == []
    assert _segments(session, class_id) == []


# --- handshake authorization (§11, §15, §39) -------------------------------------------


def test_teacher_handshake_returns_identity_and_creates_no_segment(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    audit_before = len(
        session.scalars(
            select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
        ).all()
    )

    identity = classroom_service.authorize_ws_connection(
        session, approved_teacher, class_id, "conn-teacher"
    )
    session.commit()

    assert identity.role == "teacher"
    assert identity.user_id == approved_teacher.id
    assert identity.student_id is None
    assert identity.display_name == "Ticket Teacher"
    assert identity.teaching_offering_id == offering_id
    # Teachers are participants, not attendees: no segment, no audit.
    assert _segments(session, class_id) == []
    audit_after = len(
        session.scalars(
            select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
        ).all()
    )
    assert audit_after == audit_before


def test_foreign_teacher_and_admin_handshakes_are_refused_not_authorized(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    other_teacher: User, other_offering_id: uuid.UUID, admin: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    for outsider in (other_teacher, admin):
        with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
            classroom_service.authorize_ws_connection(
                session, outsider, class_id, f"conn-{outsider.role}"
            )
        session.rollback()
        assert excinfo.value.reason == cc.REASON_NOT_AUTHORIZED
    assert _segments(session, class_id) == []


def test_student_handshake_opens_exactly_one_segment_with_one_audit(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)

    identity = classroom_service.authorize_ws_connection(
        session, enrolled_student, class_id, "conn-1"
    )
    session.commit()

    assert identity.role == "student"
    assert identity.student_id == _student_profile(session, enrolled_student).id
    assert identity.display_name == "Ticket Student"
    segments = _segments(session, class_id)
    assert len(segments) == 1
    assert segments[0].connection_id == "conn-1"
    assert segments[0].left_at is None
    audit = session.scalars(
        select(AuthEvent).where(
            AuthEvent.user_id == enrolled_student.id,
            AuthEvent.event_type == "online_class_student_joined",
        )
    ).all()
    assert len(audit) == 1


def test_a_second_handshake_for_the_same_student_is_a_duplicate(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    classroom_service.authorize_ws_connection(
        session, enrolled_student, class_id, "conn-1"
    )
    session.commit()

    with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
        classroom_service.authorize_ws_connection(
            session, enrolled_student, class_id, "conn-2"
        )
    session.rollback()
    assert excinfo.value.reason == cc.REASON_DUPLICATE
    # The refused handshake created no second segment (§15).
    assert [s.connection_id for s in _segments(session, class_id)] == ["conn-1"]


def test_handshakes_against_a_non_live_class_refuse_with_class_not_live(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    scheduled = _create(session, approved_teacher, offering_id)
    session.commit()

    with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
        classroom_service.authorize_ws_connection(
            session, approved_teacher, scheduled.class_id, "conn-t"
        )
    session.rollback()
    assert excinfo.value.reason == cc.REASON_CLASS_NOT_LIVE

    online_class_service.start_my_class(
        session, approved_teacher, offering_id, scheduled.class_id
    )
    online_class_service.end_my_class(
        session, approved_teacher, offering_id, scheduled.class_id
    )
    session.commit()

    for user, cid in (
        (approved_teacher, "conn-t2"),
        (enrolled_student, "conn-s2"),
    ):
        with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
            classroom_service.authorize_ws_connection(
                session, user, scheduled.class_id, cid
            )
        session.rollback()
        assert excinfo.value.reason == cc.REASON_CLASS_NOT_LIVE
    assert _segments(session, scheduled.class_id) == []


def test_student_handshake_survives_the_overdue_sweep_as_class_not_live(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    """A class whose window passed ends inside the handshake (§11 step 3)."""
    class_id = _start(session, approved_teacher, offering_id)
    _force_overdue(session, class_id)
    row = session.get(OnlineClassSession, class_id)

    with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
        classroom_service.authorize_ws_connection(
            session, enrolled_student, class_id, "conn-late"
        )
    assert excinfo.value.reason == cc.REASON_CLASS_NOT_LIVE
    # join_class swept: persisting (as the endpoint does) leaves it ENDED.
    session.commit()
    session.refresh(row)
    assert row.status == OnlineClassStatus.ENDED.value
    assert _segments(session, class_id) == []


def test_role_without_a_profile_never_enters(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    profile = _student_profile(session, student)
    session.delete(profile)
    session.commit()

    with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
        classroom_service.authorize_ws_connection(
            session, student, class_id, "conn-ghost"
        )
    session.rollback()
    assert excinfo.value.reason == cc.REASON_NOT_AUTHORIZED


def test_an_inactive_account_never_enters(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    """§11 step 5: the ticket is consumed, but a disabled account still loses.

    The identity behind the ticket is revalidated at the handshake, so a
    suspension between mint and connect never opens a segment.
    """
    class_id = _start(session, approved_teacher, offering_id)
    read = classroom_service.issue_ws_ticket_for_student(
        session, _student_profile(session, enrolled_student), class_id
    )
    session.commit()
    assert (
        classroom_service.consume_ws_ticket(session, class_id, read.ticket) is not None
    )
    session.commit()

    enrolled_student.status = UserStatus.DISABLED.value
    session.commit()

    with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
        classroom_service.authorize_ws_connection(
            session, enrolled_student, class_id, "conn-disabled"
        )
    session.rollback()
    assert excinfo.value.reason == cc.REASON_NOT_AUTHORIZED
    assert _segments(session, class_id) == []


def test_a_suspended_teacher_never_enters(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
) -> None:
    """The account gate applies to the teacher path too (§11 step 5)."""
    class_id = _start(session, approved_teacher, offering_id)
    approved_teacher.status = UserStatus.SUSPENDED.value
    session.commit()

    with pytest.raises(classroom_service.ClassroomConnectionRefused) as excinfo:
        classroom_service.authorize_ws_connection(
            session, approved_teacher, class_id, "conn-suspended"
        )
    session.rollback()
    assert excinfo.value.reason == cc.REASON_NOT_AUTHORIZED
    assert _segments(session, class_id) == []



# --- heartbeat (§32) -------------------------------------------------------------------


def test_teacher_heartbeat_sweeps_an_overdue_class_and_refuses_afterwards(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    _force_overdue(session, class_id)
    row = session.get(OnlineClassSession, class_id)

    assert classroom_service.heartbeat_teacher(session, class_id) is True
    session.commit()
    session.refresh(row)
    assert row.status == OnlineClassStatus.ENDED.value

    with pytest.raises(LearningConflictError) as excinfo:
        classroom_service.heartbeat_teacher(session, class_id)
    session.rollback()
    assert "requires a live class" in str(excinfo.value)


def test_teacher_heartbeat_on_a_live_class_touches_nothing(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    audit_before = len(
        session.scalars(
            select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
        ).all()
    )

    assert classroom_service.heartbeat_teacher(session, class_id) is False
    session.rollback()

    # Silence is the point: no audit row for a probe.
    audit_after = len(
        session.scalars(
            select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
        ).all()
    )
    assert audit_after == audit_before


# --- presence snapshot feed (§20) -------------------------------------------------------


def test_remote_student_feed_skips_local_and_self_students(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User, other_student: User, quiet_student: User,
) -> None:
    class_id = _start(session, approved_teacher, offering_id)
    for user in (other_student, quiet_student):
        _enroll(session, user, offering_id)
    for user, connection in (
        (enrolled_student, "conn-self"),
        (other_student, "conn-local"),
        (quiet_student, "conn-remote"),
    ):
        classroom_service.join_class(session, user, class_id, connection)
        session.commit()
    self_id = _student_profile(session, enrolled_student).id
    local_id = _student_profile(session, other_student).id
    remote_id = _student_profile(session, quiet_student).id

    feed = classroom_service.remote_students_in_class(
        session,
        class_id,
        local_student_ids={local_id},
        exclude_student_id=self_id,
    )

    assert len(feed) == 1
    (entry,) = feed
    assert entry["role"] == "student"
    assert entry["display_name"] == "Quiet Student"
    assert entry["participant_ref"] == participant_ref(class_id, remote_id)


# --- transport vocabulary (§16, §15, §13) ----------------------------------------------


def test_close_codes_are_stable_and_mapped_from_service_reasons() -> None:
    assert cc.NORMAL_CLOSURE == 1000
    assert cc.POLICY_VIOLATION == 1008
    assert cc.INTERNAL_ERROR == 1011
    assert cc.WS_TICKET_INVALID == 4001
    assert cc.WS_NOT_AUTHORIZED == 4003
    assert cc.CLASS_NOT_LIVE == 4008
    assert cc.CLOSE_BY_REASON == {
        cc.REASON_NOT_AUTHORIZED: 4003,
        cc.REASON_CLASS_NOT_LIVE: 4008,
        cc.REASON_DUPLICATE: 1008,
    }


def test_the_three_transport_events_are_published_from_the_services(
    session: Session, approved_teacher: User, offering_id: uuid.UUID,
    enrolled_student: User, monkeypatch,
) -> None:
    """§22/§25/§29: the transition itself publishes — the socket layer
    never decides, it only relays what the transaction already said."""
    published: list[dict] = []

    class _Recorder:
        def publish(self, _session: Session, event: dict) -> None:
            published.append(event)

    recorder = _Recorder()
    monkeypatch.setattr(online_class_service, "get_event_bus", lambda: recorder)
    monkeypatch.setattr(
        learning_enrollment_service, "get_event_bus", lambda: recorder
    )

    created = _create(session, approved_teacher, offering_id)
    session.commit()
    online_class_service.start_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    session.commit()
    assert published == [{"type": "class.started", "class_id": str(created.class_id)}]

    online_class_service.end_my_class(
        session, approved_teacher, offering_id, created.class_id
    )
    session.commit()
    assert published[-1] == {"type": "class.ended", "class_id": str(created.class_id)}

    profile = _student_profile(session, enrolled_student)
    enrollment = session.scalar(
        select(LearningEnrollment).where(LearningEnrollment.student_id == profile.id)
    )
    learning_enrollment_service.leave(session, profile, enrollment.id)
    session.commit()
    assert published[-1] == {
        "type": "connection.revoke",
        "class_id": None,
        "student_id": str(profile.id),
        "teaching_offering_id": str(offering_id),
    }
    # Exactly three transitions, exactly three events — nothing per socket.
    assert [event["type"] for event in published] == [
        "class.started",
        "class.ended",
        "connection.revoke",
    ]


def test_participant_ref_is_deterministic_opaque_and_class_bound() -> None:
    class_a, class_b = uuid.uuid4(), uuid.uuid4()
    user = uuid.uuid4()

    first = participant_ref(class_a, user)
    assert first == participant_ref(class_a, user)
    assert len(first) == 32
    assert set(first) <= set("0123456789abcdef")
    assert str(user) not in first and str(class_a) not in first
    assert participant_ref(class_b, user) != first
    assert participant_ref(class_a, uuid.uuid4()) != first


def _fake_connection(class_id: uuid.UUID, user_id: uuid.UUID, **overrides) -> LiveConnection:
    fields = {
        "websocket": None,
        "class_id": class_id,
        "user_id": user_id,
        "role": "student",
        "display_name": "Fake",
        "participant_ref": participant_ref(class_id, user_id),
        "connection_id": overrides.pop("connection_id", "conn-fake"),
        "teaching_offering_id": uuid.uuid4(),
        "student_id": user_id,
    }
    fields.update(overrides)
    return LiveConnection(**fields)


def test_registry_enforces_one_live_connection_per_participant() -> None:
    registry = ConnectionRegistry()
    class_id, user_id = uuid.uuid4(), uuid.uuid4()
    first = _fake_connection(class_id, user_id, connection_id="conn-1")
    second = _fake_connection(class_id, user_id, connection_id="conn-2")
    elsewhere = _fake_connection(uuid.uuid4(), user_id, connection_id="conn-3")

    assert registry.try_register(first) is True
    assert registry.try_register(second) is False  # §15: one per participant
    assert registry.try_register(elsewhere) is True  # other class, other key
    assert len(registry) == 2
    assert registry.connections_for_class(class_id) == [first]

    registry.unregister(first)
    assert registry.try_register(second) is True  # reconnect after cleanup
    registry.unregister(second)  # idempotent double-fire
    registry.unregister(second)
    assert registry.try_register(second) is True


def test_registry_finds_connections_for_an_offering_revocation() -> None:
    registry = ConnectionRegistry()
    class_a, class_b = uuid.uuid4(), uuid.uuid4()
    offering = uuid.uuid4()
    student_id, other_id = uuid.uuid4(), uuid.uuid4()
    one = _fake_connection(
        class_a, student_id, teaching_offering_id=offering, connection_id="c1"
    )
    two = _fake_connection(
        class_b, student_id, teaching_offering_id=offering, connection_id="c2"
    )
    stranger = _fake_connection(
        class_a, other_id, teaching_offering_id=offering, connection_id="c3"
    )
    for connection in (one, two, stranger):
        assert registry.try_register(connection) is True

    found = registry.find_for_revoke(offering, student_id)
    assert {connection.connection_id for connection in found} == {"c1", "c2"}


def test_bus_publish_validates_and_stays_a_local_noop_on_sqlite() -> None:
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        bus = RealtimeEventBus()
        bus.publish(db, {"type": "class.started", "class_id": str(uuid.uuid4())})
        db.rollback()

        with pytest.raises(ValueError):
            bus.publish(db, {"class_id": None})  # no type
        with pytest.raises(ValueError):
            bus.publish(db, {"type": "x" * 9000, "class_id": None})  # too big
    finally:
        db.close()
        engine.dispose()
