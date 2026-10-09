"""Data access for ``class_attendance_segments`` (Phase 3, slice 3B).

How participation intervals are written and found — never *when* (that is
the classroom service's business):

- :func:`create` opens a segment; the partial unique index
  ``uq_class_attendance_segments_open_key`` (at most one OPEN segment per
  student per class) is the race backstop behind the service's class row
  lock, so a duplicate join cannot commit even under interleaving;
- :func:`find_by_connection` looks a segment up by its unguessable
  ``connection_id`` first: leave/heartbeat therefore learn nothing about
  whether some *other* class id exists (no existence leak from a UUID);
- :func:`close` / :func:`touch` finalize ONE segment (flush, never
  commit); :func:`close_open_segments_for_class` finalizes them all in a
  single atomic UPDATE when the class ends — whoever ends the class, the
  open segments never outlive it;
- the roster helpers feed the two read surfaces: participants (students
  with at least one segment) and attendance (active-enrolled students
  UNION segment-having students, so a student who joined and left — or
  joined after dropping enrollment — still shows up).

Lock order (inherited from ``online_class_repository``):

    teaching_offerings  ->  online_class_sessions  ->  segments

Every writer here runs *after* the service took the class row lock.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.models.class_attendance_segment import ClassAttendanceSegment
from app.models.enums import LearningEnrollmentStatus
from app.models.learning_enrollment import LearningEnrollment
from app.models.student import Student


def create(
    session: Session,
    *,
    class_session_id: uuid.UUID,
    student_id: uuid.UUID,
    connection_id: str,
    joined_at: datetime,
) -> ClassAttendanceSegment:
    """Open one participation segment (flushed, not committed).

    ``joined_at``/``last_seen_at`` come from the caller so the service's
    single ``now`` stamps the whole join atomically. A duplicate open
    segment for the same (student, class) raises ``IntegrityError`` from
    the partial unique index — the service maps that to a 409.
    """
    segment = ClassAttendanceSegment(
        class_session_id=class_session_id,
        student_id=student_id,
        connection_id=connection_id,
        joined_at=joined_at,
        last_seen_at=joined_at,
    )
    session.add(segment)
    session.flush()
    return segment


def find_open(
    session: Session, class_session_id: uuid.UUID, student_id: uuid.UUID
) -> ClassAttendanceSegment | None:
    """The student's still-open segment in one class, if any (no lock).

    Duplicate-join check: the service reads this *while holding the class
    row lock*, so no concurrent join can slip between this read and
    :func:`create`.
    """
    stmt = (
        select(ClassAttendanceSegment)
        .where(
            ClassAttendanceSegment.class_session_id == class_session_id,
            ClassAttendanceSegment.student_id == student_id,
            ClassAttendanceSegment.left_at.is_(None),
        )
        .limit(1)
    )
    return session.scalar(stmt)


def find_by_connection(
    session: Session,
    *,
    class_session_id: uuid.UUID,
    connection_id: str,
    student_id: uuid.UUID,
) -> ClassAttendanceSegment | None:
    """The segment one WebSocket connection produced, open or closed.

    Leave/heartbeat key on ``connection_id`` (unguessable, 64 chars) *and*
    the student id *and* the class id, so an outsider replaying a guessed
    class id learns nothing: the lookup itself never confirms that some
    other class exists. The caller then locks the class row and re-reads
    this segment with :func:`get_by_id_for_update`.
    """
    stmt = (
        select(ClassAttendanceSegment)
        .where(
            ClassAttendanceSegment.class_session_id == class_session_id,
            ClassAttendanceSegment.connection_id == connection_id,
            ClassAttendanceSegment.student_id == student_id,
        )
        .limit(1)
    )
    return session.scalar(stmt)


def get_by_id_for_update(
    session: Session, segment_id: uuid.UUID
) -> ClassAttendanceSegment | None:
    """One segment with its row locked (``SELECT ... FOR UPDATE``).

    Taken after the class row lock (documented lock order), which turns
    "re-read the segment, then close or touch it" into one atomic decision
    — a leave racing the class-end bulk close has exactly one winner.
    ``populate_existing`` makes the lock read authoritative for an object
    the identity map may already hold.
    """
    stmt = (
        select(ClassAttendanceSegment)
        .where(ClassAttendanceSegment.id == segment_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalar(stmt)


def close(
    session: Session,
    segment: ClassAttendanceSegment,
    *,
    left_at: datetime,
    last_seen_at: datetime,
) -> ClassAttendanceSegment:
    """Finalize one open segment (flushed, not committed).

    ``left_at`` and ``last_seen_at`` arrive from the caller: leaving sets
    both to the same ``now`` so the final heartbeat equals the departure.
    """
    segment.left_at = left_at
    segment.last_seen_at = last_seen_at
    session.flush()
    return segment


def touch(
    session: Session,
    segment: ClassAttendanceSegment,
    *,
    last_seen_at: datetime,
) -> ClassAttendanceSegment:
    """Liveness only: move ``last_seen_at``, never ``joined_at``/``left_at``."""
    segment.last_seen_at = last_seen_at
    session.flush()
    return segment


def close_open_segments_for_class(
    session: Session, class_session_id: uuid.UUID, *, left_at: datetime
) -> int:
    """Close every still-open segment of one class; return rows closed.

    One atomic UPDATE, not read-then-write: the class row is already
    locked by the service, and this must not depend on a prior SELECT
    having seen every open row. Called from ``_end_internal`` for BOTH
    the manual End and the automatic end — no open segment can outlive
    its class. No audit row: the class end is audited once, not once per
    participant.
    """
    stmt = (
        update(ClassAttendanceSegment)
        .where(
            ClassAttendanceSegment.class_session_id == class_session_id,
            ClassAttendanceSegment.left_at.is_(None),
        )
        .values(left_at=left_at)
    )
    result = session.execute(stmt)
    return result.rowcount or 0


def list_for_class(
    session: Session, class_session_id: uuid.UUID
) -> list[ClassAttendanceSegment]:
    """Every segment of one class, chronological (attendance grouping)."""
    stmt = (
        select(ClassAttendanceSegment)
        .where(ClassAttendanceSegment.class_session_id == class_session_id)
        .order_by(ClassAttendanceSegment.joined_at, ClassAttendanceSegment.id)
    )
    return list(session.scalars(stmt))


def list_for_class_student(
    session: Session, class_session_id: uuid.UUID, student_id: uuid.UUID
) -> list[ClassAttendanceSegment]:
    """One student's segments in one class, chronological (own attendance)."""
    stmt = (
        select(ClassAttendanceSegment)
        .where(
            ClassAttendanceSegment.class_session_id == class_session_id,
            ClassAttendanceSegment.student_id == student_id,
        )
        .order_by(ClassAttendanceSegment.joined_at, ClassAttendanceSegment.id)
    )
    return list(session.scalars(stmt))


def list_open_students(
    session: Session, class_session_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """``(student_id, full_name)`` of everyone with an OPEN segment (§20).

    The database half of the WebSocket presence snapshot: students whose
    socket lives in ANOTHER worker still have an open segment here, so a
    newcomer sees them. Locally connected students are filtered out by
    the caller (their registry entry is fresher). Ordered ``joined_at``
    ascending — deterministic snapshot order.
    """
    stmt = (
        select(ClassAttendanceSegment.student_id, Student.full_name)
        .join(Student, Student.id == ClassAttendanceSegment.student_id)
        .where(
            ClassAttendanceSegment.class_session_id == class_session_id,
            ClassAttendanceSegment.left_at.is_(None),
        )
        .order_by(ClassAttendanceSegment.joined_at, ClassAttendanceSegment.id)
    )
    return list(session.execute(stmt))


def has_participation(
    session: Session, *, class_session_id: uuid.UUID, student_id: uuid.UUID
) -> bool:
    """Has this student ever had a segment in this class? (EXISTS, no rows.)"""
    stmt = (
        select(ClassAttendanceSegment.id)
        .where(
            ClassAttendanceSegment.class_session_id == class_session_id,
            ClassAttendanceSegment.student_id == student_id,
        )
        .limit(1)
    )
    return session.scalar(stmt) is not None


def list_participant_rows(
    session: Session, class_session_id: uuid.UUID
) -> list[tuple]:
    """One aggregate row per student who has at least one segment.

    Returns ``(student_id, full_name, first_joined_at, last_seen_at,
    segment_count, open_count)`` ordered ``full_name`` then ``student_id``
    (deterministic roster). ``open_count > 0`` is "online"; the aggregate
    keeps the roster one round-trip instead of N+1 per participant.
    """
    open_count = func.sum(
        case((ClassAttendanceSegment.left_at.is_(None), 1), else_=0)
    ).label("open_count")
    stmt = (
        select(
            ClassAttendanceSegment.student_id.label("student_id"),
            Student.full_name.label("full_name"),
            func.min(ClassAttendanceSegment.joined_at).label("first_joined_at"),
            func.max(ClassAttendanceSegment.last_seen_at).label("last_seen_at"),
            func.count(ClassAttendanceSegment.id).label("segment_count"),
            open_count,
        )
        .join(Student, Student.id == ClassAttendanceSegment.student_id)
        .where(ClassAttendanceSegment.class_session_id == class_session_id)
        .group_by(ClassAttendanceSegment.student_id, Student.full_name)
        .order_by(Student.full_name, ClassAttendanceSegment.student_id)
    )
    return list(session.execute(stmt))


def list_attendance_roster(
    session: Session,
    *,
    teaching_offering_id: uuid.UUID,
    class_session_id: uuid.UUID,
) -> list[tuple[uuid.UUID, str]]:
    """``(student_id, full_name)`` for everyone attendance must cover.

    UNION of the offering's ACTIVE enrollments and the class's
    segment-having students: enrolled students show up before they join
    (with zero seconds), and a student with segments — including one who
    left the enrollment after attending — is never dropped. DISTINCT is
    the union's default; ordering is ``full_name`` then ``student_id``.
    """
    enrolled = (
        select(Student.id, Student.full_name)
        .join(LearningEnrollment, LearningEnrollment.student_id == Student.id)
        .where(
            LearningEnrollment.teaching_offering_id == teaching_offering_id,
            LearningEnrollment.status == LearningEnrollmentStatus.ACTIVE.value,
        )
    )
    segmented = (
        select(Student.id, Student.full_name)
        .join(
            ClassAttendanceSegment,
            ClassAttendanceSegment.student_id == Student.id,
        )
        .where(ClassAttendanceSegment.class_session_id == class_session_id)
    )
    unioned = enrolled.union(segmented).subquery()
    stmt = (
        select(unioned.c.id, unioned.c.full_name)
        .order_by(unioned.c.full_name, unioned.c.id)
    )
    return list(session.execute(stmt))
