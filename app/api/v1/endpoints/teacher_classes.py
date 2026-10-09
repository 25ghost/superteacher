"""Teacher self-service: online classes (Phase 3, slices 3A + 3B).

Role-split (no endpoint serves two roles) — all under ``/me/teacher``
and guarded by ``require_teacher``, scoped by the offering in the path:

- ``POST   /offerings/{offering_id}/classes`` — schedule a class: the
  window is validated end to end (sane order, no overlap with another
  scheduled/live class of the SAME offering) while the offering row is
  held under ``SELECT ... FOR UPDATE``, so concurrent creates cannot
  double-book the timetable.
- ``GET    /offerings/{offering_id}/classes`` — the timetable of one of
  your offerings, earliest first.
- ``GET    /offerings/{offering_id}/classes/{class_id}`` — one class of
  one of your offerings; a class of another offering answers the SAME
  404 as an unknown id (L6 existence leak).
- ``PATCH  /offerings/{offering_id}/classes/{class_id}`` — amend the
  schedule of a still-``scheduled`` class; after ``SCHEDULED -> LIVE``
  every scheduling field is immutable (409, never a silent 200).
- ``POST   .../classes/{class_id}/start`` — ``scheduled -> live`` with
  ``actual_started_at``; atomic under the class row lock (a second start
  is a 409) and a window that has already closed can never go live.
- ``POST   .../classes/{class_id}/end`` — ``live -> ended`` with
  ``actual_ended_at``; atomic under the same lock, so this End and the
  automatic end of the same class leave exactly one winner. Open
  attendance segments are finalized in the same transaction.
- ``POST   .../classes/{class_id}/cancel`` — ``scheduled -> cancelled``
  (terminal; a live class must be ended, never cancelled).
- ``POST   .../classes/{class_id}/ws-ticket`` — mint the short-lived,
  SINGLE-USE ticket that authenticates the classroom WebSocket (3C):
  approved owner + LIVE class only (403/404/409), TTL 120 s, only the
  SHA-256 digest is stored. The raw ticket is returned exactly once —
  ``{"ticket", "expires_at", "ws_path"}`` — and the socket redeems it at
  ``wss://.../ws/classes/{class_id}?ticket=...``.
- ``GET    .../classes/{class_id}/participants`` — who has ever been
  inside the class (students with at least one attendance segment),
  online flag included; an empty roster for a scheduled class, not an
  error.
- ``GET    .../classes/{class_id}/attendance`` — every student's
  DERIVED verdict (cumulative connected seconds vs the class's real
  duration: >= 50% AND >= 10 minutes ⇒ ``attended``), recomputed on
  every read; 409 before the class has started (attendance only exists
  once the class runs).
- ``GET    .../classes/{class_id}/transcript`` — the HISTORICAL
  transcript of an ENDED class: every message in ``sequence`` ASC,
  cursor-paged (``after_sequence``, exclusive). Owning the offering is
  the entitlement (no participation segment needed); 409 while the class
  is scheduled/live/cancelled — the live classroom reads through the
  WebSocket (3C/3D), never this endpoint.
- ``GET    .../classes/{class_id}/messages`` — the slice 3D RECOVERY
  read: the same exclusive ``after_sequence`` cursor, but reachable while
  the class is LIVE so a reconnecting client can close the gap between
  its last processed sequence and the live stream. Owning the class is
  the entitlement at every status; SCHEDULED/CANCELLED answer 409.

Creating one requires an **approved** teacher verification (403
otherwise) and an *usable* (active) offering (409 otherwise): vetting
keeps meaning after the invitation is accepted. Every class read also
mechanically ends an overdue ``live`` class (``reason: "auto"``) and
commits that transition — the system must not stay LIVE because the
teacher forgot to press End.

Endpoint bodies stay thin: validate schema → delegate to the service →
map the Phase 1 error family onto HTTP → commit on success. Read paths
commit ONLY when that mechanical auto-end fired.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_TEACHER
from app.core.auth_dependencies import require_teacher
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.online_class import (
    ClassAttendanceRead,
    ClassMessageRead,
    ClassParticipantRead,
    ClassWsTicketRead,
    OnlineClassSessionCreate,
    OnlineClassSessionRead,
    OnlineClassSessionUpdate,
)
from app.services import classroom_service, online_class_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/me/teacher", tags=[TAG_TEACHER])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post(
    "/offerings/{offering_id}/classes",
    response_model=OnlineClassSessionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule an online class inside one of your offerings",
    description=(
        "Schedule ONE class window (timezone-aware start/end) inside a "
        "teaching offering you own. The offering row is locked for the "
        "whole check, so overlapping scheduled/live windows of the SAME "
        "offering are refused under concurrency (409) — there is no "
        "recurrence and no calendar. The optional lesson_id may only name "
        "a lesson of this offering (404 otherwise). Requires an APPROVED "
        "teacher verification (403) and an active offering (409 "
        "otherwise). Exactly one audit event: online_class_created."
    ),
    responses={
        201: {"description": "Class created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account, or verification is not approved"},
        404: {"description": "Unknown offering id, not one of yours, or unknown lesson id"},
        409: {"description": "Offering not active, or the window overlaps another class"},
        422: {"description": "Invalid request (naive datetime, unknown keys, inverted window)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def create_my_class(
    request: Request,
    offering_id: uuid.UUID,
    payload: OnlineClassSessionCreate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> OnlineClassSessionRead:
    try:
        created = online_class_service.create_my_class(
            session, user, offering_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return created


@router.get(
    "/offerings/{offering_id}/classes",
    response_model=list[OnlineClassSessionRead],
    summary="List the classes of one of your offerings",
    description=(
        "Every class of ONE offering you own, earliest first (deterministic "
        "scheduled_start_at ascending). Unknown or foreign offering id → 404 "
        "(same answer as an unknown id). An overdue live class in the list is "
        "mechanically ended (reason: auto) before the rows are returned, and "
        "this read commits that transition. Teachers only — there is no "
        "cross-offering class list."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering id, or not one of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_my_classes(
    request: Request,
    offering_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[OnlineClassSessionRead]:
    try:
        classes, auto_ended = online_class_service.list_my_classes(
            session, user, offering_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended overdue class(es): persist it.
        session.commit()
    return classes


@router.get(
    "/offerings/{offering_id}/classes/{class_id}",
    response_model=OnlineClassSessionRead,
    summary="Retrieve one online class of one of your offerings",
    description=(
        "Full summary of ONE class inside ONE of YOUR offering ids "
        "(identity from the Bearer token). A class that exists but belongs "
        "to another teacher or another offering returns the SAME 404 as an "
        "unknown id (L6 existence leak). An overdue live class is ended "
        "mechanically (reason: auto) and this read commits that transition."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_class(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> OnlineClassSessionRead:
    try:
        read, auto_ended = online_class_service.get_my_class(
            session, user, offering_id, class_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended an overdue class: persist it.
        session.commit()
    return read


@router.patch(
    "/offerings/{offering_id}/classes/{class_id}",
    response_model=OnlineClassSessionRead,
    summary="Amend a still-scheduled online class",
    description=(
        "Change the window and/or the lesson link of a class that is still "
        "scheduled. Once SCHEDULED → LIVE happens every scheduling field is "
        "immutable (409, never a silent 200). The effective window is "
        "re-validated end to end (order + no overlap with another "
        "scheduled/live class of the offering, excluding this one). "
        "lesson_id: null clears the link; a supplied id may only name a "
        "lesson of this offering (404). Exactly one audit event: "
        "online_class_updated. Unknown or foreign id → 404."
    ),
    responses={
        200: {"description": "Class updated"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class/lesson id, or not one of yours"},
        409: {"description": "Class is not scheduled, or the new window overlaps"},
        422: {"description": "Invalid request (no fields, null window, unknown keys, naive datetime)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def update_my_class(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    payload: OnlineClassSessionUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> OnlineClassSessionRead:
    try:
        updated = online_class_service.update_my_class(
            session, user, offering_id, class_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated


@router.post(
    "/offerings/{offering_id}/classes/{class_id}/start",
    response_model=OnlineClassSessionRead,
    summary="Start a scheduled online class (SCHEDULED → LIVE)",
    description=(
        "Move a scheduled class to live and record actual_started_at. "
        "Atomic under the class row lock: two concurrent starts leave "
        "exactly one winner, the loser is a 409 (never a second "
        "transition). A window whose scheduled end has already passed can "
        "never go live (409). Only SCHEDULED → LIVE is allowed — a live, "
        "ended or cancelled class is a 409. Exactly one audit event: "
        "online_class_started. Unknown or foreign id → 404."
    ),
    responses={
        200: {"description": "Class is now live"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
        409: {"description": "Wrong current status, or the window has already ended"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def start_my_class(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> OnlineClassSessionRead:
    try:
        read = online_class_service.start_my_class(
            session, user, offering_id, class_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return read


@router.post(
    "/offerings/{offering_id}/classes/{class_id}/end",
    response_model=OnlineClassSessionRead,
    summary="End a live online class (LIVE → ENDED)",
    description=(
        "Close the class and record actual_ended_at; open attendance "
        "segments are finalized in the same transaction. Atomic under the "
        "class row lock, so this manual End and the automatic end of the "
        "same class leave exactly one winner — the loser is a 409. Only "
        "LIVE → ENDED is allowed (scheduled → 409: start it, or cancel it "
        "while it is still scheduled). Exactly one audit event: "
        "online_class_ended (reason: manual). Unknown or foreign id → 404."
    ),
    responses={
        200: {"description": "Class ended"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
        409: {"description": "The class is not live (wrong current status)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def end_my_class(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> OnlineClassSessionRead:
    try:
        read = online_class_service.end_my_class(
            session, user, offering_id, class_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return read


@router.post(
    "/offerings/{offering_id}/classes/{class_id}/cancel",
    response_model=OnlineClassSessionRead,
    summary="Cancel a scheduled online class (SCHEDULED → CANCELLED)",
    description=(
        "Drop a class that never started: only SCHEDULED → CANCELLED is "
        "allowed (a live class must be ended, never cancelled — 409; an "
        "ended/cancelled class is terminal — 409). A cancelled class can "
        "never go live, so it can never gather messages or attendance, and "
        "the window becomes free for rescheduling. Exactly one audit event: "
        "online_class_cancelled. Unknown or foreign id → 404."
    ),
    responses={
        200: {"description": "Class cancelled"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
        409: {"description": "The class is not in a cancellable (scheduled) status"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def cancel_my_class(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> OnlineClassSessionRead:
    try:
        read = online_class_service.cancel_my_class(
            session, user, offering_id, class_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return read


@router.post(
    "/offerings/{offering_id}/classes/{class_id}/ws-ticket",
    response_model=ClassWsTicketRead,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a single-use WebSocket ticket for one of your live classes",
    description=(
        "Issue ONE short-lived (120 s), single-use access ticket for the "
        "classroom WebSocket of this class: the browser then opens "
        "``wss://...{ws_path}?ticket=...`` — browsers cannot attach a "
        "Bearer header to a WebSocket handshake, and this is deliberately "
        "not a second authentication system: the ticket is an opaque "
        "capability, not an identity proof. Requirements: teacher role "
        "(403), APPROVED verification (403), the class inside one of YOUR "
        "offerings — unknown or foreign ids answer the SAME 404 (L6) — and "
        "a LIVE class (409; an overdue class is swept and ends first, and "
        "its end is committed by this write). The response carries the raw "
        "ticket EXACTLY ONCE: the server keeps only its SHA-256 digest, "
        "never logs it, and redemption is atomic — a second attempt, an "
        "expired ticket or one minted for another class all fail with the "
        "same WebSocket close code 4001."
    ),
    responses={
        201: {"description": "Ticket minted (raw value returned once)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account, or verification is not approved"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
        409: {"description": "The class is not live (scheduled/ended/cancelled)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def issue_class_ws_ticket(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> ClassWsTicketRead:
    try:
        ticket = classroom_service.issue_ws_ticket_for_teacher(
            session, user, offering_id, class_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return ticket


@router.get(
    "/offerings/{offering_id}/classes/{class_id}/participants",
    response_model=list[ClassParticipantRead],
    summary="List who has been inside one of your online classes",
    description=(
        "The participant roster of ONE class of ONE offering you own: every "
        "student with at least one attendance segment, ordered full name then "
        "student id, with ``online`` (still connected), first join, last seen "
        "and segment count. A scheduled class has no segments yet and answers "
        "an EMPTY list — that is a fact, not an error. A class of another "
        "offering returns the SAME 404 as an unknown id (L6 existence leak). "
        "An overdue live class is ended mechanically (reason: auto) and this "
        "read commits that transition. Teachers only — there is no "
        "cross-offering participant list and no student directory."
    ),
    responses={
        200: {"description": "Participant roster (possibly empty)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_class_participants(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[ClassParticipantRead]:
    try:
        participants, auto_ended = classroom_service.list_class_participants(
            session, user, offering_id, class_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended an overdue class: persist it.
        session.commit()
    return participants


@router.get(
    "/offerings/{offering_id}/classes/{class_id}/attendance",
    response_model=list[ClassAttendanceRead],
    summary="Read the derived attendance of one of your online classes",
    description=(
        "Every student's attendance for ONE class, RECOMPUTED on every read "
        "from the participation segments — never an editable flag. The roster "
        "is the offering's ACTIVE enrollments UNION the students who actually "
        "have segments (nobody who attended is ever dropped). Per row: "
        "cumulative connected seconds (clamped to the class window), the "
        "class's real duration (actual_ended_at - actual_started_at; while "
        "live, the time so far), and attendance_status — \"attended\" when "
        "cumulative >= 10 minutes AND cumulative covers at least half of the "
        "real duration (exactly 50% qualifies), else \"not_attended\". 409 "
        "before the class has started: attendance only exists once the class "
        "runs. Unknown or foreign class id → 404."
    ),
    responses={
        200: {"description": "One derived attendance row per student"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
        409: {"description": "The class has not started (scheduled or cancelled)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_class_attendance(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[ClassAttendanceRead]:
    try:
        rows, auto_ended = classroom_service.get_class_attendance(
            session, user, offering_id, class_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended an overdue class: persist it.
        session.commit()
    return rows


@router.get(
    "/offerings/{offering_id}/classes/{class_id}/transcript",
    response_model=list[ClassMessageRead],
    summary="Read the message transcript of one of your online classes",
    description=(
        "The full historical transcript of ONE class of ONE offering you "
        "own: every message, ordered ``sequence`` ASC (the ONLY ordering — "
        "unique per class, so it is total and stable), cursor-paged with "
        "``after_sequence`` (EXCLUSIVE: replay the last sequence you saw "
        "and nothing duplicates) and ``limit``. Owning the class IS the "
        "entitlement — no participation segment needed — and another "
        "teacher gets the SAME 404 as an unknown id (L6 existence leak). "
        "Only an ENDED class answers (409 for scheduled/live/cancelled): "
        "the live classroom reads through the WebSocket (slices 3C/3D), "
        "never this endpoint. Each message exposes only presentation-safe "
        "data — id, sequence, sender role + display name, body, timestamp "
        "— never an email address, user id or connection id. Read-only: "
        "messages are immutable once written. An overdue live class is "
        "ended mechanically (reason: auto) first, and this read commits "
        "that transition."
    ),
    responses={
        200: {"description": "Transcript page (possibly empty)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
        409: {"description": "The class has not ended yet (scheduled/live/cancelled)"},
        422: {"description": "Invalid cursor or limit"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_class_transcript(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[ClassMessageRead]:
    try:
        messages, auto_ended = classroom_service.get_transcript_for_teacher(
            session,
            user,
            offering_id,
            class_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended an overdue class: persist it.
        session.commit()
    return messages


@router.get(
    "/offerings/{offering_id}/classes/{class_id}/messages",
    response_model=list[ClassMessageRead],
    summary="Recover classroom messages after your last processed sequence",
    description=(
        "The reconnection cursor read of the LIVE classroom (slice 3D): "
        "every message with ``sequence`` strictly greater than "
        "``after_sequence`` (EXCLUSIVE — replay the last sequence you saw "
        "and nothing duplicates), bounded by ``limit`` and ordered "
        "``sequence`` ASC, the ONLY authoritative order (unique per class, "
        "so it is total and stable). Use it after a fresh WebSocket "
        "connection is already open — subscribe first, then fetch this "
        "page, then deduplicate against live ``message.created`` events by "
        "sequence: a message committed while your socket was down stays "
        "discoverable here even if its notification was lost, and recovery "
        "itself never creates a message. Owning the class IS the "
        "entitlement at every status (403 role, another teacher gets the "
        "SAME 404 as an unknown id — L6 existence leak): a LIVE class "
        "answers for its owner, an ENDED class keeps the unchanged slice "
        "3B historical policy, and SCHEDULED/CANCELLED classes expose "
        "nothing (409). Each message exposes only presentation-safe data "
        "— id, sequence, sender role + display name, body, timestamp — "
        "never an email address, user id or connection id. Read-only: "
        "messages are immutable once written. An overdue live class is "
        "ended mechanically (reason: auto) first, and this read commits "
        "that transition."
    ),
    responses={
        200: {"description": "A page of messages (possibly empty)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown class id, or not inside one of your offerings"},
        409: {"description": "The class never happened (scheduled/cancelled)"},
        422: {"description": "Invalid cursor or limit"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_class_messages(
    request: Request,
    offering_id: uuid.UUID,
    class_id: uuid.UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[ClassMessageRead]:
    try:
        messages, auto_ended = classroom_service.read_messages_for_teacher(
            session,
            user,
            offering_id,
            class_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended an overdue class: persist it.
        session.commit()
    return messages
