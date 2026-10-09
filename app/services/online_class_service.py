"""Online classes: scheduling and lifecycle of the text-only classroom.

Business rules live here (same split as the Phase 1/2 services); the
endpoint stays thin and the repository only writes rows:

- a class belongs to exactly ONE existing teaching offering; the teacher
  derives from that offering and is never an independently mutable link.
  Creating one requires an **approved** teacher who owns an *usable*
  (active) offering — vetting keeps meaning after invitation acceptance;
- ``scheduled -> live -> ended`` and ``scheduled -> cancelled`` are the
  only transitions (both terminal, never reopened); anything else is a
  409, never a silent 200;
- overlapping ``scheduled``/``live`` windows of the SAME offering are
  refused while the offering row is held under ``SELECT ... FOR UPDATE``,
  so two concurrent creates cannot double-book the timetable. There is no
  recurrence and no calendar: the window is what was typed;
- an overdue ``live`` class is ended mechanically (``reason: "auto"``) by
  :func:`ensure_not_overdue`, which every class touchpoint runs — the
  system must not stay LIVE because a teacher forgot to press End. A read
  that performs this transition reports it so the API layer can commit;
- every mutation writes exactly one ``auth_events`` row (subject = the
  teacher for lifecycle events; student join/leave events arrive with the
  classroom in a later slice), with ``actor_user_id`` None: each caller
  acts on their own record;
- slice 3C: the LIVE and ENDED transitions also publish ``class.started``
  / ``class.ended`` on the realtime bus, INSIDE this transaction — the
  event fires exactly when the state does (and never when a rollback
  undoes it), and no endpoint ever learns what a socket looks like;
- a teacher's class visibility ends at their own offerings, and a
  student's at the offerings they are actively enrolled in — there is no
  global class list, student search or directory on either surface.

Nothing commits here — the API layer owns the transaction.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    OnlineClassStatus,
    TeacherVerificationStatus,
    UserRole,
)
from app.models.lesson import Lesson
from app.models.online_class_session import OnlineClassSession
from app.models.teacher import Teacher
from app.models.topic import Topic
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import class_attendance_repository as segment_repo
from app.repositories import learning_enrollment_repository as enrollment_repo
from app.repositories import online_class_repository as class_repo
from app.repositories import teaching_offering_repository as offering_repo
from app.realtime.event_bus import get_event_bus
from app.schemas.online_class import (
    OnlineClassSessionCreate,
    OnlineClassSessionRead,
    OnlineClassSessionUpdate,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)

logger = logging.getLogger(__name__)

#: Allowed status moves, as ``current -> frozenset(next ...)``. Anything
#: outside this map (including every move out of ``ended``/``cancelled``)
#: is refused — there is no reopening.
_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    OnlineClassStatus.SCHEDULED.value: frozenset(
        {OnlineClassStatus.LIVE.value, OnlineClassStatus.CANCELLED.value}
    ),
    OnlineClassStatus.LIVE.value: frozenset({OnlineClassStatus.ENDED.value}),
    OnlineClassStatus.ENDED.value: frozenset(),
    OnlineClassStatus.CANCELLED.value: frozenset(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize a stored datetime to aware UTC for comparisons.

    PostgreSQL ``timestamptz`` always returns aware datetimes; SQLite
    (unit-test scratch databases) returns naive ones stored as UTC — the
    same helper ``auth_service`` uses for its expiry checks.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _load_profile(session: Session, user: User) -> Teacher:
    """The caller's teacher profile (404 when the account has none)."""
    if user.role != UserRole.TEACHER.value:
        raise LearningForbiddenError("teacher role required for this operation")
    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    if profile is None:
        raise LearningNotFoundError("no teacher profile for this account")
    return profile


def _require_approved(profile: Teacher) -> None:
    """Veto every class-creating attempt by a teacher who is not approved."""
    if profile.verification_status != TeacherVerificationStatus.APPROVED.value:
        raise LearningForbiddenError(
            f"teacher verification is {profile.verification_status!r}; "
            "only an approved teacher may schedule online classes"
        )


def _read(class_session: OnlineClassSession) -> OnlineClassSessionRead:
    """Build the class summary from the entity."""
    return OnlineClassSessionRead(
        class_id=class_session.id,
        teaching_offering_id=class_session.teaching_offering_id,
        lesson_id=class_session.lesson_id,
        scheduled_start_at=class_session.scheduled_start_at,
        scheduled_end_at=class_session.scheduled_end_at,
        actual_started_at=class_session.actual_started_at,
        actual_ended_at=class_session.actual_ended_at,
        status=class_session.status,
        created_at=class_session.created_at,
        updated_at=class_session.updated_at,
    )


def _owned_offering(session: Session, profile: Teacher, offering_id: uuid.UUID):
    """One of the caller's offerings, ROW-LOCKED — write paths only.

    Locking here is the serialization point for every operation that can
    affect the timetable (create/update/cancel), so the overlap check
    below always reads committed state. A foreign id 404s like an unknown
    one: UUIDs are identifiers, not authorization.
    """
    offering = offering_repo.get_by_id_for_update(session, offering_id)
    if offering is None or offering.teacher_id != profile.id:
        raise LearningNotFoundError(f"no teaching offering with id {offering_id}")
    return offering


def _visible_offering(session: Session, profile: Teacher, offering_id: uuid.UUID):
    """One of the caller's offerings for READ paths (no lock taken)."""
    offering = offering_repo.get_by_id(session, offering_id)
    if offering is None or offering.teacher_id != profile.id:
        raise LearningNotFoundError(f"no teaching offering with id {offering_id}")
    return offering


def _require_usable_offering(offering) -> None:
    """Only an active offering accepts new classes (same rule as enroll)."""
    if offering.status != "active":
        raise LearningConflictError(
            f"teaching offering is {offering.status!r} and is not "
            "accepting new classes"
        )


def _lesson_of_offering(
    session: Session, teaching_offering_id: uuid.UUID, lesson_id: uuid.UUID
) -> Lesson:
    """The lesson, but only if it hangs under this offering (else 404).

    lesson -> topic -> teaching offering: a class can never reference a
    lesson of another teacher's curriculum.
    """
    stmt = (
        select(Lesson)
        .join(Topic, Lesson.topic_id == Topic.id)
        .where(
            Lesson.id == lesson_id,
            Topic.teaching_offering_id == teaching_offering_id,
        )
    )
    lesson = session.scalar(stmt)
    if lesson is None:
        raise LearningNotFoundError(
            f"no lesson with id {lesson_id} in this teaching offering"
        )
    return lesson


def _owned_class(
    session: Session, teaching_offering_id: uuid.UUID, class_id: uuid.UUID
) -> OnlineClassSession:
    """The class, ROW-LOCKED, but only inside the supplied offering.

    A class of a foreign offering (or an unknown id) answers the same
    404 — the path's offering id is authorization, and the class must
    belong to it.
    """
    class_session = class_repo.get_by_id_for_update(session, class_id)
    if (
        class_session is None
        or class_session.teaching_offering_id != teaching_offering_id
    ):
        raise LearningNotFoundError(f"no online class with id {class_id}")
    return class_session


def _require_transition(class_session: OnlineClassSession, new_status: str) -> None:
    """Refuse any move the state machine does not allow (409, never 200)."""
    allowed = _STATUS_TRANSITIONS.get(class_session.status, frozenset())
    if new_status not in allowed:
        raise LearningConflictError(
            f"online class cannot move from {class_session.status!r} "
            f"to {new_status!r}"
        )


def _check_no_overlap(
    session: Session,
    teaching_offering_id: uuid.UUID,
    scheduled_start_at: datetime,
    scheduled_end_at: datetime,
    *,
    exclude_class_id: uuid.UUID | None = None,
) -> None:
    """Refuse a window that intersects another scheduled/live class (409).

    Callers hold the offering row lock, which is what makes this check
    race-free: every create/update that could introduce an overlap is
    serialized on that lock.
    """
    existing = class_repo.find_overlapping(
        session,
        teaching_offering_id,
        scheduled_start_at,
        scheduled_end_at,
        exclude_class_id=exclude_class_id,
    )
    if existing is not None:
        raise LearningConflictError(
            "this class window overlaps another scheduled or live class "
            "of the offering"
        )


def _end_internal(
    session: Session,
    class_session: OnlineClassSession,
    *,
    now: datetime,
    reason: str,
) -> None:
    """LIVE -> ENDED + ``actual_ended_at`` + exactly one audit row.

    The caller must already hold the class row lock (or have just taken
    it), so a manual End and the automatic end of the same class cannot
    both succeed. Attendance segments are finalized here as well, so no
    open segment can outlive the class.
    """
    _require_transition(class_session, OnlineClassStatus.ENDED.value)
    class_session.status = OnlineClassStatus.ENDED.value
    class_session.actual_ended_at = now
    session.flush()

    # Finalize attendance in the SAME transaction, for BOTH reasons this
    # function runs (manual End and automatic end): no open segment may
    # outlive its class, and the segments close at actual_ended_at — not
    # at some later disconnect — so nobody accrues time after the class
    # is over. Atomic UPDATE (one statement), no audit per participant.
    segments_closed = segment_repo.close_open_segments_for_class(
        session, class_session.id, left_at=now
    )

    # Publish in the SAME transaction, before anything else looks at the
    # new state (§22/§36): the event fires on commit for EVERY reason the
    # class ends — manual End here, the overdue auto-end inside this very
    # function — so connected sockets always hear "class.ended" exactly
    # once and no open connection outlives its class.
    get_event_bus().publish(
        session,
        {
            "type": "class.ended",
            "class_id": str(class_session.id),
        },
    )

    offering = offering_repo.get_by_id(session, class_session.teaching_offering_id)
    auth_event_repo.log_event(
        session,
        user_id=offering.teacher.user_id,
        event_type="online_class_ended",
        metadata_json=json.dumps(
            {
                "class_id": str(class_session.id),
                "teaching_offering_id": str(class_session.teaching_offering_id),
                "reason": reason,
            }
        ),
    )
    session.flush()
    logger.info(
        "online class ended",
        extra={
            "class_id": str(class_session.id),
            "teaching_offering_id": str(class_session.teaching_offering_id),
            "reason": reason,
            "segments_closed": segments_closed,
        },
    )


def ensure_not_overdue(
    session: Session, class_id: uuid.UUID, *, now: datetime | None = None
) -> bool:
    """End a ``live`` class whose scheduled window has closed.

    The mechanical backstop behind "the teacher forgot to press End":
    every class touchpoint calls this (reads, ticket issuance, classroom
    messages, heartbeat). The row is locked first, so a manual End racing
    this path has exactly one winner. Returns True when *this* call
    performed the transition (the caller — a committing path — persists
    it).
    """
    now = now or _now()
    class_session = class_repo.get_by_id_for_update(session, class_id)
    if class_session is None:
        return False
    if (
        class_session.status == OnlineClassStatus.LIVE.value
        and _as_aware_utc(class_session.scheduled_end_at) <= now
    ):
        _end_internal(session, class_session, now=now, reason="auto")
        return True
    return False


def _end_due_for_offering(
    session: Session, teaching_offering_id: uuid.UUID, now: datetime
) -> None:
    """Finish overdue live classes of ONE offering before its timetable is
    read for an overlap decision (create/update hold the offering lock)."""
    for due in class_repo.list_due_live(
        session, now, teaching_offering_id=teaching_offering_id
    ):
        locked = class_repo.get_by_id_for_update(session, due.id)
        if (
            locked is not None
            and locked.status == OnlineClassStatus.LIVE.value
            and _as_aware_utc(locked.scheduled_end_at) <= now
        ):
            _end_internal(session, locked, now=now, reason="auto")


# --- teacher surface ----------------------------------------------------------------


def create_my_class(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    payload: OnlineClassSessionCreate,
) -> OnlineClassSessionRead:
    """Schedule one class inside an owned, usable offering (then audit it).

    Order: approved teacher (403) -> owned offering, ROW-LOCKED (404) ->
    usable offering (409) -> optional lesson of this offering (404) ->
    sane window (422) -> no overlap with scheduled/live classes (409) ->
    insert -> audit. The lock on the offering is what makes the overlap
    check hold under concurrent creates.
    """
    profile = _load_profile(session, user)
    _require_approved(profile)
    offering = _owned_offering(session, profile, offering_id)
    _require_usable_offering(offering)

    if payload.lesson_id is not None:
        _lesson_of_offering(session, offering_id, payload.lesson_id)

    now = _now()
    if payload.scheduled_end_at <= payload.scheduled_start_at:
        raise LearningValidationError("scheduled_end_at must be after scheduled_start_at")

    _end_due_for_offering(session, offering_id, now)
    _check_no_overlap(
        session,
        offering_id,
        payload.scheduled_start_at,
        payload.scheduled_end_at,
    )

    class_session = class_repo.create(
        session,
        teaching_offering_id=offering_id,
        lesson_id=payload.lesson_id,
        scheduled_start_at=payload.scheduled_start_at,
        scheduled_end_at=payload.scheduled_end_at,
    )
    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="online_class_created",
        metadata_json=json.dumps(
            {
                "class_id": str(class_session.id),
                "teaching_offering_id": str(offering_id),
                "scheduled_start_at": payload.scheduled_start_at.isoformat(),
                "scheduled_end_at": payload.scheduled_end_at.isoformat(),
            }
        ),
    )
    session.flush()
    logger.info(
        "online class created",
        extra={
            "user_id": str(user.id),
            "class_id": str(class_session.id),
            "teaching_offering_id": str(offering_id),
        },
    )
    return _read(class_session)


def list_my_classes(
    session: Session, user: User, offering_id: uuid.UUID
) -> tuple[list[OnlineClassSessionRead], bool]:
    """Every class of one owned offering, earliest first.

    Returns ``(classes, auto_ended)``: the second element tells the
    endpoint that this read mechanically ended overdue live classes and
    must therefore commit.
    """
    profile = _load_profile(session, user)
    _visible_offering(session, profile, offering_id)

    classes = class_repo.list_for_offering(session, offering_id)
    auto_ended = False
    for class_session in classes:
        if ensure_not_overdue(session, class_session.id):
            auto_ended = True
    return [_read(class_session) for class_session in classes], auto_ended


def get_my_class(
    session: Session, user: User, offering_id: uuid.UUID, class_id: uuid.UUID
) -> tuple[OnlineClassSessionRead, bool]:
    """Read one class of an owned offering (404 for unknown *or* foreign).

    Returns ``(class, auto_ended)`` — see :func:`list_my_classes`.
    """
    profile = _load_profile(session, user)
    _visible_offering(session, profile, offering_id)

    class_session = class_repo.get_by_id(session, class_id)
    if (
        class_session is None
        or class_session.teaching_offering_id != offering_id
    ):
        raise LearningNotFoundError(f"no online class with id {class_id}")

    auto_ended = ensure_not_overdue(session, class_id)
    return _read(class_session), auto_ended


def update_my_class(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    payload: OnlineClassSessionUpdate,
) -> OnlineClassSessionRead:
    """Amend the schedule (and/or lesson link) of a SCHEDULED class.

    Once ``SCHEDULED -> LIVE`` happens every scheduling field is
    immutable. The effective window is re-validated end-to-end (order +
    overlap excluding this row) while the offering lock is held. Exactly
    one audit row: ``online_class_updated``.
    """
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    class_session = _owned_class(session, offering_id, class_id)

    if class_session.status != OnlineClassStatus.SCHEDULED.value:
        raise LearningConflictError(
            "only a scheduled class can be edited; scheduling information "
            "is immutable once the class has started"
        )

    supplied = payload.model_fields_set
    new_start = _as_aware_utc(
        payload.scheduled_start_at
        if "scheduled_start_at" in supplied
        else class_session.scheduled_start_at
    )
    new_end = _as_aware_utc(
        payload.scheduled_end_at
        if "scheduled_end_at" in supplied
        else class_session.scheduled_end_at
    )
    if new_end <= new_start:
        raise LearningValidationError("scheduled_end_at must be after scheduled_start_at")

    if "lesson_id" in supplied and payload.lesson_id is not None:
        _lesson_of_offering(session, offering_id, payload.lesson_id)

    now = _now()
    _end_due_for_offering(session, offering_id, now)
    if "scheduled_start_at" in supplied or "scheduled_end_at" in supplied:
        _check_no_overlap(
            session,
            offering_id,
            new_start,
            new_end,
            exclude_class_id=class_session.id,
        )

    class_session.scheduled_start_at = new_start
    class_session.scheduled_end_at = new_end
    if "lesson_id" in supplied:
        class_session.lesson_id = payload.lesson_id

    session.flush()
    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="online_class_updated",
        metadata_json=json.dumps(
            {
                "class_id": str(class_session.id),
                "teaching_offering_id": str(offering_id),
                "scheduled_start_at": new_start.isoformat(),
                "scheduled_end_at": new_end.isoformat(),
            }
        ),
    )
    session.flush()
    logger.info(
        "online class updated",
        extra={"user_id": str(user.id), "class_id": str(class_session.id)},
    )
    return _read(class_session)


def start_my_class(
    session: Session, user: User, offering_id: uuid.UUID, class_id: uuid.UUID
) -> OnlineClassSessionRead:
    """SCHEDULED -> LIVE, recording ``actual_started_at`` (then audit it).

    Atomic under the class row lock: two concurrent starts leave exactly
    one winner (the loser is a 409). Only the owning teacher can start,
    and a window that has already closed can never go live.
    """
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    class_session = _owned_class(session, offering_id, class_id)

    _require_transition(class_session, OnlineClassStatus.LIVE.value)
    now = _now()
    if now >= _as_aware_utc(class_session.scheduled_end_at):
        raise LearningConflictError(
            "the scheduled window of this class has already ended; "
            "it cannot be started"
        )

    class_session.status = OnlineClassStatus.LIVE.value
    class_session.actual_started_at = now
    session.flush()
    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="online_class_started",
        metadata_json=json.dumps(
            {
                "class_id": str(class_session.id),
                "teaching_offering_id": str(offering_id),
            }
        ),
    )
    session.flush()
    # Same transaction as the transition (§25): connected sockets hear
    # "class.started" exactly when — and only if — the status persisted.
    get_event_bus().publish(
        session,
        {
            "type": "class.started",
            "class_id": str(class_session.id),
        },
    )
    logger.info(
        "online class started",
        extra={"user_id": str(user.id), "class_id": str(class_session.id)},
    )
    return _read(class_session)


def end_my_class(
    session: Session, user: User, offering_id: uuid.UUID, class_id: uuid.UUID
) -> OnlineClassSessionRead:
    """LIVE -> ENDED, recording ``actual_ended_at`` and finalizing
    attendance (then audit it).

    Atomic under the class row lock, so the teacher's End and the
    automatic end of the same class leave exactly one winner; the loser
    (this call losing) is a 409, never a second transition.
    """
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    class_session = _owned_class(session, offering_id, class_id)

    _end_internal(session, class_session, now=_now(), reason="manual")
    logger.info(
        "online class ended",
        extra={"user_id": str(user.id), "class_id": str(class_session.id)},
    )
    return _read(class_session)


def cancel_my_class(
    session: Session, user: User, offering_id: uuid.UUID, class_id: uuid.UUID
) -> OnlineClassSessionRead:
    """SCHEDULED -> CANCELLED (then audit it).

    Only a scheduled class may be cancelled; a cancelled class can never
    go live, so it can never gather messages or attendance.
    """
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    class_session = _owned_class(session, offering_id, class_id)

    _require_transition(class_session, OnlineClassStatus.CANCELLED.value)
    class_session.status = OnlineClassStatus.CANCELLED.value
    session.flush()
    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="online_class_cancelled",
        metadata_json=json.dumps(
            {
                "class_id": str(class_session.id),
                "teaching_offering_id": str(offering_id),
            }
        ),
    )
    session.flush()
    logger.info(
        "online class cancelled",
        extra={"user_id": str(user.id), "class_id": str(class_session.id)},
    )
    return _read(class_session)


# --- student surface ----------------------------------------------------------------


def _authorized_student_class(
    session: Session, student, class_id: uuid.UUID
) -> OnlineClassSession:
    """The class, but only through an ACTIVE enrollment in its offering.

    The chain is fixed: student -> ACTIVE LearningEnrollment ->
    TeachingOffering -> class. Unknown and unauthorized answer the SAME
    404, so a class id can never confirm whether someone else's class
    exists.
    """
    class_session = class_repo.get_by_id(session, class_id)
    if class_session is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")
    enrollment = enrollment_repo.find_active_for_student_offering(
        session, student.id, class_session.teaching_offering_id
    )
    if enrollment is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")
    return class_session


def list_classes_for_student(
    session: Session,
    student,
    *,
    teaching_offering_id: uuid.UUID | None = None,
) -> tuple[list[OnlineClassSessionRead], bool]:
    """Classes of the offerings the student is actively enrolled in.

    Returns ``(classes, auto_ended)`` like the teacher read surface: an
    overdue live class in the result is mechanically ended, and the
    endpoint commits that transition.
    """
    classes = class_repo.list_for_student(
        session, student.id, teaching_offering_id=teaching_offering_id
    )
    auto_ended = False
    for class_session in classes:
        if ensure_not_overdue(session, class_session.id):
            auto_ended = True
    return [_read(class_session) for class_session in classes], auto_ended


def get_class_for_student(
    session: Session, student, class_id: uuid.UUID
) -> tuple[OnlineClassSessionRead, bool]:
    """Read one class of an actively-enrolled offering (404 otherwise)."""
    class_session = _authorized_student_class(session, student, class_id)
    auto_ended = ensure_not_overdue(session, class_id)
    return _read(class_session), auto_ended
