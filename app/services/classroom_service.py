"""Participation, presence and attendance for the text-only classroom.

Slice 3B adds everything the classroom needs *before* transport: who is
inside a class, when they were inside it, and what that adds up to. The
WebSocket (3C) and the message send/broadcast path (3D) call the write
primitives here; the four read endpoints expose the historical surface.

Write primitives (service-only — no HTTP route in this slice):

- :func:`join_class` — open one participation segment. Order: student
  role (403, messages borrowed verbatim from
  ``auth_service.resolve_current_student``) → overdue sweep (an overdue
  live class ends instead of admitting anyone) → class row lock (404,
  house message) → ACTIVE enrollment in the class's offering else the
  SAME 404 (a class id can never confirm someone else's class, L6) →
  only ``live`` accepts joins (409) → no open segment yet (409) →
  insert. The partial unique index ``uq_class_attendance_segments_open_key``
  is the race backstop: a duplicate join cannot commit even if the check
  above were raced, and the resulting ``IntegrityError`` is mapped to the
  same 409.
- :func:`leave_class` — close your own segment at ``now``. The segment is
  found by ``connection_id`` FIRST (unguessable → the lookup leaks
  nothing about class ids), then the class row is locked, then the
  segment is re-read FOR UPDATE: "still live?" and "still open?" become
  one atomic decision (409 / 409). Leave deliberately does NOT sweep an
  overdue class — closing your own segment must never be refused by a
  sweep you did not ask for; the class-end bulk close is the backstop.
- :func:`heartbeat` — liveness only (``last_seen_at`` moves,
  ``joined_at``/``left_at`` never do). The heartbeat IS the auto-end
  sweep: if the class has passed its window it ends here and every open
  segment (yours included) is finalized at ``actual_ended_at`` — the
  call then reports ``auto_ended=True`` so the caller commits that
  transition instead of failing a pointless touch.

Slice 3C adds the transport-facing write primitives — still no sockets
here, only the rules the WebSocket handshake and heartbeat delegate to:

- :func:`issue_ws_ticket_for_teacher` / :func:`issue_ws_ticket_for_student`
  — mint one short-lived, single-use ticket for a LIVE class the caller
  may enter (owner + approved / active enrollment); a class that is not
  live answers 409, a foreign or unknown class the SAME 404 (L6). Only
  the SHA-256 digest is stored — the raw ticket is returned exactly once.
- :func:`consume_ws_ticket` — atomically redeem one ticket for its class
  (single UPDATE, §10); ``None`` = invalid, without saying why.
- :func:`authorize_ws_connection` — revalidate a redeemed identity
  against the class at handshake time (account still active, role and
  approval still valid, ownership/enrollment still held) and, for
  students, open the attendance segment through :func:`join_class` (the
  single owner of segment rules). Refusals raise
  :class:`ClassroomConnectionRefused` with a transport-neutral reason the
  endpoint maps to a close code.
- :func:`heartbeat_teacher` — the teacher-side probe: sweep + "still
  live?", no segment to touch.

Audits: exactly one ``auth_events`` row per join/leave (subject = the
acting student, ``actor_user_id`` None, metadata = class + student ids);
heartbeats, ticket issuance/consumption and the class-end bulk close are
deliberately silent — an audit trail records decisions, not traffic.

Read surfaces (endpoint commits only when the sweep fired):

- :func:`list_class_participants` / :func:`get_class_attendance` — the
  teacher's roster and verdicts for one class of an owned offering
  (authorization rides ``online_class_service.get_my_class``: 403 role,
  404 unknown-or-foreign, sweep, all in one place);
- :func:`get_attendance_for_student` — the student's own verdict,
  reachable through ACTIVE enrollment **or** prior participation (a
  student who joined and left still reads their own history); 409 before
  the class has started;
- :func:`get_transcript_for_teacher` / :func:`get_transcript_for_student`
  — the HISTORICAL transcript of an ENDED class only (the live classroom
  is the WebSocket's job, slices 3C/3D). The owning teacher reads the
  full transcript of their class; a student reads it if and only if a
  participation segment exists for them — past participation, never
  current enrollment and never the 50% attendance threshold, grants the
  read, and nothing can revoke it later. Unauthorized viewers and
  never-joined students get the SAME 404 as an unknown id.

Attendance is derived, never stored: segments are the facts, and every
read recomputes

    cumulative = sum of each segment's overlap with the class window
                 (clamped: start = max(joined_at, actual_started_at),
                  end = min(left_at or now-or-actual_ended_at, window end))
    actual     = actual_ended_at - actual_started_at
                 (while live: now - actual_started_at; 0 → nobody attended)
    attended   <=> actual > 0 AND cumulative >= 10 minutes
                    AND cumulative * 2 >= actual   (>= 50%, ties included)

Nothing commits here — the API layer (or the WebSocket handler in 3C)
owns the transaction, exactly like the Phase 1/2 services.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.class_attendance_segment import ClassAttendanceSegment
from app.models.class_ws_ticket import ClassWsTicket
from app.models.enums import (
    OnlineClassStatus,
    TeacherVerificationStatus,
    UserRole,
    UserStatus,
)
from app.models.online_class_session import OnlineClassSession
from app.models.student import Student
from app.models.teacher import Teacher
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import class_attendance_repository as segment_repo
from app.repositories import class_message_repository as message_repo
from app.repositories import learning_enrollment_repository as enrollment_repo
from app.repositories import online_class_repository as class_repo
from app.repositories import teaching_offering_repository as offering_repo
from app.repositories import ws_ticket_repository as ticket_repo
from app.realtime.connections import participant_ref
from app.realtime.close_codes import (
    REASON_CLASS_NOT_LIVE,
    REASON_DUPLICATE,
    REASON_NOT_AUTHORIZED,
)
from app.schemas.online_class import (
    AttendanceSegmentRead,
    ClassAttendanceRead,
    ClassMessageRead,
    ClassParticipantRead,
    ClassWsTicketRead,
    MessageSenderRead,
)
from app.services import auth_service, online_class_service
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)

logger = logging.getLogger(__name__)

#: MVP attendance gate: at least ten connected minutes before the >= 50%
#: rule can ever say "attended" — the floor that keeps a 2-minute glance
#: at a 4-minute class from counting. Exposed through
#: :func:`meets_minimum_duration` so the future teacher-rating
#: eligibility rule reuses one predicate instead of restating it.
MIN_ATTENDED_DURATION = timedelta(minutes=10)

#: Fallback display name when a sender's profile row cannot be resolved.
_UNKNOWN_PARTICIPANT = "Unknown participant"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize a stored datetime to aware UTC for comparisons.

    PostgreSQL ``timestamptz`` always returns aware datetimes; SQLite
    (unit-test scratch databases) returns naive ones stored as UTC — the
    same helper ``online_class_service`` uses for its window checks.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# --- shared guards --------------------------------------------------------------------


def _student_profile(session: Session, user: User) -> Student:
    """The caller's student profile with role enforced (403 otherwise).

    Borrowing ``auth_service.resolve_current_student`` verbatim keeps one
    wording for both role refusals — the HTTP layer and the WebSocket
    layer can never disagree about what a non-student is told.
    """
    try:
        return auth_service.resolve_current_student(session, user)
    except auth_service.AuthForbiddenError as exc:
        raise LearningForbiddenError(str(exc)) from exc


def _require_live(class_session: OnlineClassSession, action: str) -> None:
    """Only a ``live`` class accepts participant activity (409 otherwise)."""
    if class_session.status != OnlineClassStatus.LIVE.value:
        raise LearningConflictError(
            f"online class is {class_session.status!r}; participant "
            f"{action} requires a live class"
        )


def _require_attendance_readable(class_session: OnlineClassSession) -> None:
    """Attendance exists only once the class has started (409 otherwise)."""
    if class_session.status not in (
        OnlineClassStatus.LIVE.value,
        OnlineClassStatus.ENDED.value,
    ):
        raise LearningConflictError(
            "attendance is only available once the class has started; "
            f"status is {class_session.status!r}"
        )


def _readable_class_for_student(
    session: Session, student: Student, class_id: uuid.UUID
) -> OnlineClassSession:
    """The class through ACTIVE enrollment **or** prior participation.

    Extends the 3A chain (student → ACTIVE enrollment → offering → class)
    with the attendance history: a student whose enrollment ended after
    they attended still reads their own verdict and transcript. Both
    refusals — unknown id and no relationship at all — answer the SAME
    404 (L6 existence leak).
    """
    class_session = class_repo.get_by_id(session, class_id)
    if class_session is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")
    enrollment = enrollment_repo.find_active_for_student_offering(
        session, student.id, class_session.teaching_offering_id
    )
    if enrollment is None and not segment_repo.has_participation(
        session, class_session_id=class_id, student_id=student.id
    ):
        raise LearningNotFoundError(f"no online class with id {class_id}")
    return class_session


# --- attendance arithmetic -------------------------------------------------------------


def meets_minimum_duration(cumulative_seconds: int) -> bool:
    """Has the student reached the minimum connected time (>= 10 minutes)?"""
    return timedelta(seconds=max(cumulative_seconds, 0)) >= MIN_ATTENDED_DURATION


def attendance_status(cumulative_seconds: int, actual_seconds: int) -> str:
    """``"attended"`` or ``"not_attended"`` — derived, never stored.

    Rules, all required: the class must actually have run (``actual > 0``),
    the student must clear the minimum-duration floor, and their connected
    time must cover at least half of the class's real duration (exactly
    50% qualifies).
    """
    if actual_seconds <= 0 or not meets_minimum_duration(cumulative_seconds):
        return "not_attended"
    return "attended" if cumulative_seconds * 2 >= actual_seconds else "not_attended"


def _attendance_window(
    class_session: OnlineClassSession, now: datetime
) -> tuple[datetime | None, datetime, int]:
    """``(window_start, window_end, actual_seconds)`` for one class.

    ``window_end`` is ``actual_ended_at`` for an ended class and ``now``
    while it is live — a class still running keeps accumulating
    denominator, so early reads of a live class never over-credit
    anyone. ``window_start`` is ``actual_started_at`` (LIVE/ENDED always
    record it when the class starts); without it the class never ran and
    the duration is 0 → nobody attended.
    """
    if class_session.actual_ended_at is not None:
        window_end = _as_aware_utc(class_session.actual_ended_at)
    else:
        window_end = now
    if class_session.actual_started_at is None:
        return None, window_end, 0
    window_start = _as_aware_utc(class_session.actual_started_at)
    actual_seconds = max(0, int((window_end - window_start).total_seconds()))
    return window_start, window_end, actual_seconds


def _segment_seconds(
    segment: ClassAttendanceSegment,
    window_start: datetime | None,
    window_end: datetime,
) -> int:
    """Connected seconds of ONE segment inside the class window (>= 0).

    Each segment is clamped to ``[actual_started_at, window_end]`` so
    stray timestamps can never credit time outside the class, and an
    open segment counts up to the window end (live: now, ended: the
    moment the class closed it).
    """
    if window_start is None:
        return 0
    start = max(_as_aware_utc(segment.joined_at), window_start)
    end = (
        _as_aware_utc(segment.left_at)
        if segment.left_at is not None
        else window_end
    )
    end = min(end, window_end)
    if end <= start:
        return 0
    return int((end - start).total_seconds())


def _attendance_row(
    student_id: uuid.UUID,
    full_name: str,
    segments: list[ClassAttendanceSegment],
    *,
    window_start: datetime | None,
    window_end: datetime,
    actual_seconds: int,
) -> ClassAttendanceRead:
    """Build one student's derived attendance verdict from their segments."""
    cumulative = sum(
        _segment_seconds(segment, window_start, window_end)
        for segment in segments
    )
    return ClassAttendanceRead(
        student_id=student_id,
        full_name=full_name,
        online=any(segment.left_at is None for segment in segments),
        cumulative_seconds=cumulative,
        actual_seconds=actual_seconds,
        attendance_status=attendance_status(cumulative, actual_seconds),
        segments=[
            AttendanceSegmentRead(
                joined_at=segment.joined_at,
                last_seen_at=segment.last_seen_at,
                left_at=segment.left_at,
            )
            for segment in segments
        ],
    )


def _group_by_student(
    segments: list[ClassAttendanceSegment],
) -> dict[uuid.UUID, list[ClassAttendanceSegment]]:
    """One bucket per student, preserving chronological order."""
    grouped: dict[uuid.UUID, list[ClassAttendanceSegment]] = {}
    for segment in segments:  # input order is joined_at ascending
        grouped.setdefault(segment.student_id, []).append(segment)
    return grouped


# --- write primitives (service-only in 3B) ---------------------------------------------


def join_class(
    session: Session,
    user: User,
    class_id: uuid.UUID,
    connection_id: str,
) -> ClassAttendanceSegment:
    """Open one participation segment for this student in this class.

    Write primitive: the caller (slice 3C's WebSocket handler) commits.
    Order: student role (403) → overdue sweep → class row lock (404) →
    ACTIVE enrollment (404, same message as unknown) → live class (409)
    → no open segment yet (409) → insert + one audit row
    ``online_class_student_joined``.
    """
    student = _student_profile(session, user)
    now = _now()

    # Sweep first: an overdue live class must end instead of admitting
    # anyone (the 409 below then names the real problem — it is over).
    online_class_service.ensure_not_overdue(session, class_id, now=now)
    class_session = class_repo.get_by_id_for_update(session, class_id)
    if class_session is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")

    enrollment = enrollment_repo.find_active_for_student_offering(
        session, student.id, class_session.teaching_offering_id
    )
    if enrollment is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")

    _require_live(class_session, "join")
    if segment_repo.find_open(session, class_id, student.id) is not None:
        raise LearningConflictError(
            "this student already has an open attendance segment in this class"
        )

    try:
        segment = segment_repo.create(
            session,
            class_session_id=class_id,
            student_id=student.id,
            connection_id=connection_id,
            joined_at=now,
        )
    except IntegrityError as exc:
        # uq_class_attendance_segments_open_key fired: a concurrent join
        # slipped past the check above and PostgreSQL refused the second
        # open row. Same 409 as the check — one contract, two guards.
        raise LearningConflictError(
            "this student already has an open attendance segment in this class"
        ) from exc

    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="online_class_student_joined",
        metadata_json=json.dumps(
            {
                "class_id": str(class_id),
                "student_id": str(student.id),
            }
        ),
    )
    session.flush()
    logger.info(
        "student joined online class",
        extra={
            "class_id": str(class_id),
            "student_id": str(student.id),
            "connection_id": connection_id,
        },
    )
    return segment


def leave_class(
    session: Session,
    user: User,
    class_id: uuid.UUID,
    connection_id: str,
) -> ClassAttendanceSegment:
    """Close this student's own segment at ``now``.

    Write primitive: the caller commits. The segment is found by its
    ``connection_id`` first (the id is unguessable, so the lookup cannot
    confirm whether ``class_id`` exists to anyone else), then the class
    row is locked, then the segment is re-read FOR UPDATE — a leave
    racing the class-end bulk close leaves exactly one winner. Checks:
    class still live (409) → segment still open (409). One audit row:
    ``online_class_student_left``.

    Deliberately does not sweep an overdue class: closing your segment is
    your transition, and the class-end bulk close (``_end_internal``)
    covers the class's own expiry.
    """
    student = _student_profile(session, user)
    now = _now()

    segment = segment_repo.find_by_connection(
        session,
        class_session_id=class_id,
        connection_id=connection_id,
        student_id=student.id,
    )
    if segment is None:
        raise LearningNotFoundError(
            f"no attendance segment for connection {connection_id}"
        )

    # Class lock BEFORE the segment lock (documented lock order).
    class_session = class_repo.get_by_id_for_update(
        session, segment.class_session_id
    )
    locked = segment_repo.get_by_id_for_update(session, segment.id)
    if class_session is None or locked is None:
        raise LearningNotFoundError(
            f"no attendance segment for connection {connection_id}"
        )

    _require_live(class_session, "leave")
    if locked.left_at is not None:
        raise LearningConflictError("this attendance segment is already closed")

    segment_repo.close(session, locked, left_at=now, last_seen_at=now)
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="online_class_student_left",
        metadata_json=json.dumps(
            {
                "class_id": str(class_id),
                "student_id": str(student.id),
            }
        ),
    )
    session.flush()
    logger.info(
        "student left online class",
        extra={
            "class_id": str(class_id),
            "student_id": str(student.id),
            "connection_id": connection_id,
        },
    )
    return locked


def heartbeat(
    session: Session,
    user: User,
    class_id: uuid.UUID,
    connection_id: str,
) -> tuple[ClassAttendanceSegment, bool]:
    """Liveness probe: move ``last_seen_at``; also the auto-end sweep.

    Write primitive: the caller commits — including when the sweep fired.
    Returns ``(segment, auto_ended)`` like the Phase 3 read surface:

    - sweep fires (the class passed its window while we were probing):
      every open segment is finalized at ``actual_ended_at`` already, so
      there is nothing left to touch — return the refreshed segment with
      ``auto_ended=True`` so the caller PERSISTS the end;
    - class already ended before we got here: 409 (not live);
    - otherwise: touch ``last_seen_at`` only — ``joined_at`` and
      ``left_at`` are history and never move — with ``auto_ended=False``.

    No audit row: heartbeats are traffic, not decisions.
    """
    student = _student_profile(session, user)
    now = _now()

    segment = segment_repo.find_by_connection(
        session,
        class_session_id=class_id,
        connection_id=connection_id,
        student_id=student.id,
    )
    if segment is None:
        raise LearningNotFoundError(
            f"no attendance segment for connection {connection_id}"
        )

    # The heartbeat IS the "teacher forgot to press End" sweep.
    if online_class_service.ensure_not_overdue(session, class_id, now=now):
        refreshed = segment_repo.get_by_id_for_update(session, segment.id)
        if refreshed is None:
            raise LearningNotFoundError(
                f"no attendance segment for connection {connection_id}"
            )
        logger.info(
            "auto-ended online class during heartbeat",
            extra={"class_id": str(class_id), "student_id": str(student.id)},
        )
        return refreshed, True

    class_session = class_repo.get_by_id_for_update(session, class_id)
    locked = segment_repo.get_by_id_for_update(session, segment.id)
    if class_session is None or locked is None:
        raise LearningNotFoundError(
            f"no attendance segment for connection {connection_id}"
        )

    _require_live(class_session, "heartbeat")
    if locked.left_at is not None:
        raise LearningConflictError("this attendance segment is already closed")

    segment_repo.touch(session, locked, last_seen_at=now)
    return locked, False


# --- WebSocket ticket + handshake primitives (slice 3C) --------------------------------


class ClassroomConnectionRefused(Exception):
    """A handshake refusal, named transport-neutrally.

    Raised by :func:`authorize_ws_connection`. ``reason`` is one of the
    ``REASON_*`` constants from ``app.realtime.close_codes`` — the
    endpoint alone maps reason to close code, so this module never
    imports socket machinery. The message is for logs only; clients get
    a code, never this text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"websocket connection refused: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class WsHandshakeIdentity:
    """Plain data a registered connection needs (no ORM objects)."""

    user_id: uuid.UUID
    role: str
    display_name: str
    teaching_offering_id: uuid.UUID
    student_id: uuid.UUID | None


def _ws_path(class_id: uuid.UUID) -> str:
    return f"/ws/classes/{class_id}"


def _mint_ws_ticket(
    session: Session, *, class_id: uuid.UUID, user_id: uuid.UUID
) -> ClassWsTicketRead:
    """Create one ticket row and hand back the raw value exactly once.

    Opaque ``secrets.token_urlsafe(32)`` — deliberately NOT a JWT (§5):
    a ticket is a single-use bearer capability, not an identity proof, so
    signing it would only add a decode path an attacker could probe. The
    row keeps its SHA-256 digest alone; the raw value never appears in
    logs, audits or any later response.
    """
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expires_at = _now() + timedelta(seconds=get_settings().WS_TICKET_TTL_SECONDS)
    ticket_repo.create(
        session,
        class_session_id=class_id,
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    logger.info(
        "ws ticket issued",
        extra={"class_id": str(class_id), "user_id": str(user_id)},
    )
    return ClassWsTicketRead(
        ticket=raw, expires_at=expires_at, ws_path=_ws_path(class_id)
    )


def issue_ws_ticket_for_teacher(
    session: Session, user: User, offering_id: uuid.UUID, class_id: uuid.UUID
) -> ClassWsTicketRead:
    """Mint a handshake ticket for the owning, approved teacher (§6).

    Order mirrors the class-create chain: teacher role (403) → approved
    verification (403, vetting keeps meaning after invitation acceptance)
    → get_my_class (404 unknown-or-foreign offering/class, L6 — plus the
    overdue sweep, so an expired class ends instead of admitting a
    handshake) → class is LIVE (409) → mint. The caller commits.
    """
    if user.role != UserRole.TEACHER.value:
        raise LearningForbiddenError("teacher role required for this operation")
    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    if profile is None:
        raise LearningNotFoundError("no teacher profile for this account")
    if profile.verification_status != TeacherVerificationStatus.APPROVED.value:
        raise LearningForbiddenError(
            f"teacher verification is {profile.verification_status!r}; "
            "only an approved teacher may enter the classroom"
        )

    read, _auto_ended = online_class_service.get_my_class(
        session, user, offering_id, class_id
    )
    class_session = class_repo.get_by_id(session, class_id)
    if class_session is None:  # pragma: no cover — get_my_class validated it
        raise LearningNotFoundError(f"no online class with id {class_id}")
    _require_live(class_session, "ticket issuance")
    return _mint_ws_ticket(session, class_id=read.class_id, user_id=user.id)


def issue_ws_ticket_for_student(
    session: Session, student: Student, class_id: uuid.UUID
) -> ClassWsTicketRead:
    """Mint a handshake ticket for an actively enrolled student (§6).

    The 3A chain without any participation path: student → ACTIVE
    LearningEnrollment → offering → class, unknown and unrelated both
    answering the SAME 404 (L6). A participation segment alone (the
    enrollment has ended) does NOT grant re-entry — §29 makes leaving the
    revocation point. Only a LIVE class is ticketed (409 otherwise); the
    sweep runs first so an expired class ends instead of being ticketed.
    """
    class_session = class_repo.get_by_id(session, class_id)
    if class_session is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")
    enrollment = enrollment_repo.find_active_for_student_offering(
        session, student.id, class_session.teaching_offering_id
    )
    if enrollment is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")

    online_class_service.ensure_not_overdue(session, class_id)
    _require_live(class_session, "ticket issuance")
    return _mint_ws_ticket(session, class_id=class_id, user_id=student.user_id)


def consume_ws_ticket(
    session: Session, class_id: uuid.UUID, raw_ticket: str
) -> User | None:
    """Atomically redeem one ticket for this class → its user, or ``None``.

    The repository's single UPDATE does class-scope, expiry and
    single-use in one statement (§10): two racing handshakes leave one
    winner. ``None`` collapses unknown/foreign/expired/used/never-a-ticket
    into one answer, so a probe cannot map the ticket space (§40) — the
    endpoint closes with WS_TICKET_INVALID either way.
    """
    token_hash = hashlib.sha256(raw_ticket.encode("utf-8")).hexdigest()
    ticket = ticket_repo.consume(
        session,
        token_hash=token_hash,
        class_session_id=class_id,
        now=_now(),
    )
    if ticket is None:
        return None
    user = session.get(User, ticket.user_id)
    if user is None:  # account deleted between issuance and handshake
        return None
    return user


def _handshake_conflict_reason(message: str) -> str:
    """Map a join_conflict to its transport-neutral handshake reason."""
    if "requires a live class" in message:
        return REASON_CLASS_NOT_LIVE
    if "already has an open attendance segment" in message:
        return REASON_DUPLICATE
    return REASON_NOT_AUTHORIZED


def authorize_ws_connection(
    session: Session, user: User, class_id: uuid.UUID, connection_id: str
) -> WsHandshakeIdentity:
    """Revalidate a redeemed ticket against the LIVE class (§11 step 3+).

    The ticket proves WHO; this decides whether WHO may still enter RIGHT
    NOW — account status, role, approval, ownership and enrollment are
    all rechecked, because they may have changed since issuance
    (§11 step 5 revalidates the account itself before anything else: the
    HTTP gates that minted the ticket ran at issuance time only, so a
    deactivated or suspended account never enters with a still-valid
    ticket). Refusals carry a transport-neutral ``reason``:

    - ``not_authorized`` → 4003 (inactive account, unknown class, wrong
      role, unapproved, foreign offering, missing/expired enrollment —
      the SAME answer as an unknown class, so nothing leaks);
    - ``class_not_live`` → 4008 (the class ended, was cancelled or has
      not started between issuance and this handshake);
    - ``duplicate_connection`` → 1008 (this student already has an open
      segment — the cross-worker backstop behind the in-process registry).

    For students the side effect is THE join: the segment is opened via
    :func:`join_class`, the single owner of segment rules (sweep, lock,
    enrollment, live, one-open-segment, audit). Teachers create no
    segment — attendance only tracks students. The caller commits (also
    persisting an overdue sweep the handshake triggered).
    """
    # §11 step 5 first: the identity behind the ticket must still be an
    # account that is allowed to participate at all.
    if user.status != UserStatus.ACTIVE.value:
        raise ClassroomConnectionRefused(REASON_NOT_AUTHORIZED)

    # Sweep first: a class whose window closed while the ticket sat must
    # end now, and the identity checks below then see the real status.
    online_class_service.ensure_not_overdue(session, class_id)
    class_session = class_repo.get_by_id(session, class_id)
    if class_session is None:
        raise ClassroomConnectionRefused(REASON_NOT_AUTHORIZED)

    if user.role == UserRole.TEACHER.value:
        profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
        if (
            profile is None
            or profile.verification_status != TeacherVerificationStatus.APPROVED.value
        ):
            raise ClassroomConnectionRefused(REASON_NOT_AUTHORIZED)
        offering = offering_repo.get_by_id(
            session, class_session.teaching_offering_id
        )
        if offering is None or offering.teacher_id != profile.id:
            raise ClassroomConnectionRefused(REASON_NOT_AUTHORIZED)
        if class_session.status != OnlineClassStatus.LIVE.value:
            raise ClassroomConnectionRefused(REASON_CLASS_NOT_LIVE)
        return WsHandshakeIdentity(
            user_id=user.id,
            role="teacher",
            display_name=profile.full_name,
            teaching_offering_id=class_session.teaching_offering_id,
            student_id=None,
        )

    if user.role == UserRole.STUDENT.value:
        try:
            student = _student_profile(session, user)
            join_class(session, user, class_id, connection_id)
        except LearningNotFoundError as exc:
            raise ClassroomConnectionRefused(REASON_NOT_AUTHORIZED) from exc
        except LearningForbiddenError as exc:
            raise ClassroomConnectionRefused(REASON_NOT_AUTHORIZED) from exc
        except LearningConflictError as exc:
            raise ClassroomConnectionRefused(
                _handshake_conflict_reason(str(exc))
            ) from exc
        return WsHandshakeIdentity(
            user_id=user.id,
            role="student",
            display_name=student.full_name,
            teaching_offering_id=class_session.teaching_offering_id,
            student_id=student.id,
        )

    # Administrator (or any other role): not a classroom participant (§6).
    raise ClassroomConnectionRefused(REASON_NOT_AUTHORIZED)


def heartbeat_teacher(session: Session, class_id: uuid.UUID) -> bool:
    """Liveness probe of a connected teacher: sweep, then "still live?".

    Teachers own no attendance segment, so there is nothing to touch —
    the probe exists to catch the class ending under them and to move the
    connection's last-activity stamp (the endpoint updates that locally).
    Returns ``auto_ended=True`` when THIS call ended the overdue class:
    the caller must commit that transition, and the ``class.ended`` event
    published inside the end drives the socket close (§22/§36). The class
    having ended for any other reason is a conflict — mapped by the
    endpoint to CLASS_NOT_LIVE.
    """
    if online_class_service.ensure_not_overdue(session, class_id):
        return True
    class_session = class_repo.get_by_id(session, class_id)
    if class_session is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")
    _require_live(class_session, "heartbeat")
    return False


def remote_students_in_class(
    session: Session,
    class_id: uuid.UUID,
    *,
    local_student_ids: set[uuid.UUID],
    exclude_student_id: uuid.UUID | None,
) -> list[dict]:
    """Presence entries for students connected in OTHER workers (§20).

    The newcomer's snapshot must show everyone already inside: local
    sockets come from the registry, but a student whose socket lives on
    another worker is only visible through their open attendance segment
    (the database half). Locally registered students are skipped — the
    registry entry is fresher — as is the newcomer themself. Ordered by
    ``joined_at`` (deterministic), each entry the §19 shape.
    """
    presences: list[dict] = []
    for student_id, full_name in segment_repo.list_open_students(session, class_id):
        if student_id in local_student_ids or student_id == exclude_student_id:
            continue
        presences.append(
            {
                "role": "student",
                "display_name": full_name,
                "participant_ref": participant_ref(class_id, student_id),
            }
        )
    return presences


# --- teacher read surface ---------------------------------------------------------------


def list_class_participants(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
) -> tuple[list[ClassParticipantRead], bool]:
    """Who has ever been inside this class (roster of segment-having students).

    Authorization rides ``online_class_service.get_my_class`` (403 role,
    404 unknown-or-foreign, overdue sweep) — a scheduled class simply has
    no segments yet, so its roster is empty rather than an error. Returns
    ``(participants, auto_ended)``; the endpoint commits only when the
    sweep fired.
    """
    _, auto_ended = online_class_service.get_my_class(
        session, user, offering_id, class_id
    )
    rows = segment_repo.list_participant_rows(session, class_id)
    participants = [
        ClassParticipantRead(
            student_id=row.student_id,
            full_name=row.full_name,
            online=row.open_count > 0,
            first_joined_at=row.first_joined_at,
            last_seen_at=row.last_seen_at,
            segment_count=row.segment_count,
        )
        for row in rows
    ]
    return participants, auto_ended


def get_class_attendance(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
) -> tuple[list[ClassAttendanceRead], bool]:
    """Every student's derived verdict for one of your classes.

    Roster = ACTIVE enrollments of the offering UNION segment-having
    students (attendance only once started: 409 for scheduled/cancelled
    — an enrollment list is not attendance). Returns ``(rows,
    auto_ended)``; commit only when the sweep fired.
    """
    _, auto_ended = online_class_service.get_my_class(
        session, user, offering_id, class_id
    )
    class_session = class_repo.get_by_id(session, class_id)
    if class_session is None:  # pragma: no cover — get_my_class validated it
        raise LearningNotFoundError(f"no online class with id {class_id}")
    _require_attendance_readable(class_session)

    window_start, window_end, actual_seconds = _attendance_window(
        class_session, _now()
    )
    grouped = _group_by_student(segment_repo.list_for_class(session, class_id))
    roster = segment_repo.list_attendance_roster(
        session,
        teaching_offering_id=class_session.teaching_offering_id,
        class_session_id=class_id,
    )
    rows = [
        _attendance_row(
            student_id,
            full_name,
            grouped.get(student_id, []),
            window_start=window_start,
            window_end=window_end,
            actual_seconds=actual_seconds,
        )
        for student_id, full_name in roster
    ]
    return rows, auto_ended


# --- student read surface ---------------------------------------------------------------


def get_attendance_for_student(
    session: Session,
    student: Student,
    class_id: uuid.UUID,
) -> tuple[ClassAttendanceRead, bool]:
    """The caller's own derived attendance for one class.

    Authorizes through ACTIVE enrollment **or** prior participation, then
    sweeps, then demands a started class (409 scheduled/cancelled).
    Returns ``(row, auto_ended)``.
    """
    class_session = _readable_class_for_student(session, student, class_id)
    auto_ended = online_class_service.ensure_not_overdue(session, class_id)
    _require_attendance_readable(class_session)

    window_start, window_end, actual_seconds = _attendance_window(
        class_session, _now()
    )
    segments = segment_repo.list_for_class_student(session, class_id, student.id)
    row = _attendance_row(
        student.id,
        student.full_name,
        segments,
        window_start=window_start,
        window_end=window_end,
        actual_seconds=actual_seconds,
    )
    return row, auto_ended


# --- transcript (historical, ENDED only) ------------------------------------------------


def _require_ended_for_transcript(class_status: str) -> None:
    """The transcript endpoint is historical: only an ENDED class answers.

    A SCHEDULED class never occurred, a CANCELLED one never admitted
    anyone, and a LIVE class belongs to the WebSocket recovery flow
    (slices 3C/3D) — the HTTP surface never doubles as a live chat read.
    """
    if class_status != OnlineClassStatus.ENDED.value:
        raise LearningConflictError(
            "the class transcript is only available once the class has "
            f"ended; status is {class_status!r}"
        )


def get_transcript_for_teacher(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    *,
    after_sequence: int = 0,
    limit: int = 100,
) -> tuple[list[ClassMessageRead], bool]:
    """The full transcript of one of YOUR classes, for the owning teacher.

    Authorization rides ``get_my_class`` (403 role, 404 unknown-or-foreign
    offering/class, overdue sweep) — the teacher needs no participation
    segment: owning the class IS the entitlement. The class must be
    ENDED (409 otherwise): during a live class the classroom reads
    through the WebSocket, not this historical endpoint. Returns
    ``(messages, auto_ended)``; the endpoint commits only when the sweep
    fired.
    """
    read, auto_ended = online_class_service.get_my_class(
        session, user, offering_id, class_id
    )
    _require_ended_for_transcript(read.status)

    messages = message_repo.list_messages_after(
        session, class_id, after_sequence=after_sequence, limit=limit
    )
    return _message_reads(session, messages), auto_ended


def get_transcript_for_student(
    session: Session,
    student: Student,
    class_id: uuid.UUID,
    *,
    after_sequence: int = 0,
    limit: int = 100,
) -> tuple[list[ClassMessageRead], bool]:
    """The class transcript after one sequence cursor, oldest first.

    The decisive fact is PAST PARTICIPATION: a segment for this student
    in this class, regardless of its length (a 5-minute visit qualifies)
    and regardless of what happened to the enrollment afterwards —
    historical access survives leaving the offering and switching
    teachers, and is never revoked by the 50% attendance rule. Someone
    who was never inside gets the SAME 404 as an unknown class id: the
    transcript is the classroom's history, not a read surface for
    outsiders, and enrollment alone (without a segment) never grants it.

    The class must be ENDED (409 for scheduled/cancelled/live — the live
    classroom is the WebSocket's job). Returns ``(messages, auto_ended)``.
    """
    class_session = class_repo.get_by_id(session, class_id)
    if class_session is None:
        raise LearningNotFoundError(f"no online class with id {class_id}")
    if not segment_repo.has_participation(
        session, class_session_id=class_id, student_id=student.id
    ):
        raise LearningNotFoundError(f"no online class with id {class_id}")

    auto_ended = online_class_service.ensure_not_overdue(session, class_id)
    _require_ended_for_transcript(class_session.status)

    messages = message_repo.list_messages_after(
        session, class_id, after_sequence=after_sequence, limit=limit
    )
    return _message_reads(session, messages), auto_ended


def _message_reads(
    session: Session, messages: list
) -> list[ClassMessageRead]:
    """Attach role + display name to each message (never an email).

    Sender identity is resolved with three batched IN queries — one for
    the accounts (role), one for teacher profiles, one for student
    profiles — so a 200-message page is 4 round-trips, not 600. A missing
    profile degrades to "Unknown participant"; it never falls back to the
    email address.
    """
    if not messages:
        return []

    sender_ids = {message.sender_user_id for message in messages}
    roles = dict(
        session.execute(
            select(User.id, User.role).where(User.id.in_(sender_ids))
        ).all()
    )
    teacher_names = dict(
        session.execute(
            select(Teacher.user_id, Teacher.full_name).where(
                Teacher.user_id.in_(sender_ids)
            )
        ).all()
    )
    student_names = dict(
        session.execute(
            select(Student.user_id, Student.full_name).where(
                Student.user_id.in_(sender_ids)
            )
        ).all()
    )

    reads: list[ClassMessageRead] = []
    for message in messages:
        role = roles.get(message.sender_user_id, "unknown")
        if role == "teacher":
            display_name = teacher_names.get(message.sender_user_id)
        elif role == "student":
            display_name = student_names.get(message.sender_user_id)
        else:
            display_name = None
        reads.append(
            ClassMessageRead(
                message_id=message.id,
                sequence=message.sequence,
                sender=MessageSenderRead(
                    role=role,
                    display_name=display_name or _UNKNOWN_PARTICIPANT,
                ),
                body=message.body,
                sent_at=message.created_at,
            )
        )
    return reads
