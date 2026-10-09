"""Data access for ``online_class_sessions`` (Phase 3).

Ownership and lifecycle transitions are *service* decisions; this module
only decides how class rows are written and found. Reads on mutation paths
lock the class row (``SELECT ... FOR UPDATE``) so "check the state, then
change it" is one atomic decision — start/end races, join-vs-end and
message sequence allocation all serialize on that lock. Writes ``flush``,
never commit.

Lock order (documented here because every writer must respect it):

    teaching_offerings  ->  online_class_sessions  ->  (messages, segments)

The offering row is always locked *first* by the service, which makes the
overlap check below race-free: every create/update that can introduce an
overlap holds the same offering lock before reading.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import LearningEnrollmentStatus, OnlineClassStatus
from app.models.learning_enrollment import LearningEnrollment
from app.models.online_class_session import OnlineClassSession

#: Statuses that occupy a teacher's timetable: they make another session
#: overlapping the same window a 409. ``ended``/``cancelled`` do not block.
OCCUPYING_STATUSES = (
    OnlineClassStatus.SCHEDULED.value,
    OnlineClassStatus.LIVE.value,
)


def get_by_id(session: Session, class_id: uuid.UUID) -> OnlineClassSession | None:
    """One class session (or None) — read-only paths, no lock."""
    stmt = select(OnlineClassSession).where(OnlineClassSession.id == class_id)
    return session.scalar(stmt)


def get_by_id_for_update(
    session: Session, class_id: uuid.UUID
) -> OnlineClassSession | None:
    """One class session with its row locked (``SELECT ... FOR UPDATE``).

    Plain single-table statement: a locking read must not take locks on
    other tables. ``populate_existing`` makes the lock read authoritative
    for an object the identity map may already hold.
    """
    stmt = (
        select(OnlineClassSession)
        .where(OnlineClassSession.id == class_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalar(stmt)


def list_for_offering(
    session: Session, teaching_offering_id: uuid.UUID
) -> list[OnlineClassSession]:
    """Every class of one offering — a timetable, so earliest first.

    Deterministic (``scheduled_start_at`` ascending, unique id as
    tiebreaker) so a whole batch sharing one timestamp still pages the
    same way.
    """
    stmt = (
        select(OnlineClassSession)
        .where(OnlineClassSession.teaching_offering_id == teaching_offering_id)
        .order_by(OnlineClassSession.scheduled_start_at, OnlineClassSession.id)
    )
    return list(session.scalars(stmt))


def list_for_student(
    session: Session,
    student_id: uuid.UUID,
    *,
    teaching_offering_id: uuid.UUID | None = None,
) -> list[OnlineClassSession]:
    """Classes of the offerings the student is actively enrolled in.

    The authorization chain is this join: student -> ACTIVE
    LearningEnrollment -> TeachingOffering -> class. A class of an
    offering the student never joined (or left) simply cannot appear, and
    an optional offering filter narrows within the same chain.
    """
    stmt = (
        select(OnlineClassSession)
        .join(
            LearningEnrollment,
            LearningEnrollment.teaching_offering_id
            == OnlineClassSession.teaching_offering_id,
        )
        .where(
            LearningEnrollment.student_id == student_id,
            LearningEnrollment.status == LearningEnrollmentStatus.ACTIVE.value,
        )
        .order_by(OnlineClassSession.scheduled_start_at, OnlineClassSession.id)
    )
    if teaching_offering_id is not None:
        stmt = stmt.where(
            OnlineClassSession.teaching_offering_id == teaching_offering_id
        )
    return list(session.scalars(stmt))


def find_overlapping(
    session: Session,
    teaching_offering_id: uuid.UUID,
    scheduled_start_at: datetime,
    scheduled_end_at: datetime,
    *,
    exclude_class_id: uuid.UUID | None = None,
) -> OnlineClassSession | None:
    """A scheduled/live session of this offering whose window intersects
    the proposed one, if any.

    Two half-open windows overlap when ``existing.start < new.end`` and
    ``existing.end > new.start``. ``exclude_class_id`` lets a PATCH ignore
    itself. Read while the service holds the offering lock, so a racing
    create/update for the same offering cannot slip past the check.
    """
    conditions = [
        OnlineClassSession.teaching_offering_id == teaching_offering_id,
        OnlineClassSession.status.in_(OCCUPYING_STATUSES),
        OnlineClassSession.scheduled_start_at < scheduled_end_at,
        OnlineClassSession.scheduled_end_at > scheduled_start_at,
    ]
    if exclude_class_id is not None:
        conditions.append(OnlineClassSession.id != exclude_class_id)
    stmt = select(OnlineClassSession).where(*conditions).limit(1)
    return session.scalar(stmt)


def list_due_live(
    session: Session,
    now: datetime,
    *,
    teaching_offering_id: uuid.UUID | None = None,
) -> list[OnlineClassSession]:
    """Live sessions whose scheduled window has already closed.

    The auto-end sweep reads these to finish forgotten classes; callers
    still lock each row through :func:`get_by_id_for_update` before
    transitioning, so a manual End racing the sweep has exactly one
    winner. An optional offering filter scopes the sweep to one
    timetable (used by create/update, which must see a clear schedule).
    """
    conditions = [
        OnlineClassSession.status == OnlineClassStatus.LIVE.value,
        OnlineClassSession.scheduled_end_at <= now,
    ]
    if teaching_offering_id is not None:
        conditions.append(
            OnlineClassSession.teaching_offering_id == teaching_offering_id
        )
    stmt = (
        select(OnlineClassSession)
        .where(*conditions)
        .order_by(OnlineClassSession.scheduled_end_at, OnlineClassSession.id)
    )
    return list(session.scalars(stmt))


def create(
    session: Session,
    *,
    teaching_offering_id: uuid.UUID,
    lesson_id: uuid.UUID | None,
    scheduled_start_at: datetime,
    scheduled_end_at: datetime,
) -> OnlineClassSession:
    """Insert one scheduled class (flushed, not committed), status ``scheduled``."""
    class_session = OnlineClassSession(
        teaching_offering_id=teaching_offering_id,
        lesson_id=lesson_id,
        scheduled_start_at=scheduled_start_at,
        scheduled_end_at=scheduled_end_at,
        status=OnlineClassStatus.SCHEDULED.value,
    )
    session.add(class_session)
    session.flush()  # assign the PK so the audit event can name it
    return class_session
