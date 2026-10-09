"""Student self-service: online classes (Phase 3, slices 3A + 3B).

Role-split (no endpoint serves two roles) — all under ``/me/classes`` and
guarded by ``get_current_student``:

- ``GET /me/classes`` — the classes of the offerings the student is
  ACTIVELY enrolled in (teaching_offering_id filter optional), newest
  first. There is no global class list: without an active
  LearningEnrollment the offering's classes are not reachable, so this
  surface can never widen into the class population of other offerings.
- ``GET /me/classes/{class_id}`` — one class reached through that same
  enrollment chain; a class that exists but belongs to an offering the
  student is not enrolled in returns the SAME 404 as an unknown id (L6
  existence leak — a class id can never confirm someone else's class).
- ``GET /me/classes/{class_id}/attendance`` — the caller's OWN derived
  verdict for one class (cumulative connected seconds, the class's real
  duration, ``attendance_status``), recomputed from the attendance
  segments on every read. Reachable through ACTIVE enrollment **or**
  prior participation — a student who joined and later left still reads
  their own history — and 409 before the class has started (attendance
  only exists once the class runs). Identity comes from the token: there
  is no student id parameter, so nobody can read anyone else's row.
- ``POST /me/classes/{class_id}/ws-ticket`` — mint the short-lived,
  SINGLE-USE ticket that authenticates the classroom WebSocket (3C):
  ACTIVE enrollment in the class's offering + LIVE class only (unknown
  or unrelated ids → the SAME 404, non-live → 409), TTL 120 s, only the
  SHA-256 digest is stored. The raw ticket is returned exactly once and
  the socket redeems it at ``wss://.../ws/classes/{class_id}?ticket=...``.
- ``GET /me/classes/{class_id}/transcript`` — the HISTORICAL transcript
  of an ENDED class as a cursor-paged list (``after_sequence``,
  exclusive; ``limit`` bounded), ordered ``sequence`` ASC. Eligibility is
  PAST PARTICIPATION: a durable attendance segment for you in that class
  — regardless of length (5 minutes qualifies), regardless of the 50%
  attendance verdict, and it survives leaving the enrollment or switching
  teachers. Never-joined students and unknown ids get the SAME 404 (L6).
   409 while the class is scheduled/live/cancelled: the live classroom
   reads through the WebSocket (3C/3D), never this endpoint. Senders
   expose role and display name only — never an email address.
- ``GET /me/classes/{class_id}/messages`` — the slice 3D RECOVERY read:
   the same exclusive ``after_sequence`` cursor, but reachable while the
   class is LIVE so a reconnecting client can close the gap between its
   last processed sequence and the live stream. Live reads demand an
   ACTIVE enrollment in this class's offering (a class id alone grants
   nothing), ENDED reads keep the participation policy above verbatim,
   and SCHEDULED/CANCELLED answer 409.

Every class read mechanically ends an overdue ``live`` class
(``reason: "auto"``) and commits that transition — the system must not
stay LIVE because the teacher forgot to press End.

Endpoint bodies stay thin: validate schema → delegate to the service →
map the Phase 1 error family onto HTTP. Read paths commit ONLY when that
mechanical auto-end fired.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_STUDENT
from app.core.auth_dependencies import get_current_student
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.student import Student
from app.schemas.online_class import (
    ClassAttendanceRead,
    ClassMessageRead,
    ClassWsTicketRead,
    OnlineClassSessionRead,
)
from app.services import classroom_service, online_class_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/me/classes", tags=[TAG_STUDENT])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get(
    "",
    response_model=list[OnlineClassSessionRead],
    summary="List the online classes you can attend",
    description=(
        "Every class of the offerings you are ACTIVELY enrolled in, newest "
        "first; optional teaching_offering_id narrows the list to one of "
        "them. Identity comes from the Bearer token — there is no student "
        "search, no directory and no id parameter that could reach another "
        "student's classes or an offering you are not enrolled in. An "
        "overdue live class in the list is mechanically ended (reason: auto) "
        "before the rows are returned, and this read commits that transition."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_my_classes(
    request: Request,
    teaching_offering_id: uuid.UUID | None = Query(default=None),
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[OnlineClassSessionRead]:
    try:
        classes, auto_ended = online_class_service.list_classes_for_student(
            session, student, teaching_offering_id=teaching_offering_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended overdue class(es): persist it.
        session.commit()
    return classes


@router.get(
    "/{class_id}",
    response_model=OnlineClassSessionRead,
    summary="Retrieve one online class you may attend",
    description=(
        "One class, reached ONLY through an ACTIVE enrollment in its "
        "offering: student → ACTIVE LearningEnrollment → offering → class. "
        "A class of an offering you are not enrolled in returns the SAME 404 "
        "as an unknown id (L6 existence leak). An overdue live class is ended "
        "mechanically (reason: auto) and this read commits that transition."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown class id, or not in an offering you are enrolled in"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_class(
    request: Request,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> OnlineClassSessionRead:
    try:
        read, auto_ended = online_class_service.get_class_for_student(
            session, student, class_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended an overdue class: persist it.
        session.commit()
    return read


@router.get(
    "/{class_id}/attendance",
    response_model=ClassAttendanceRead,
    summary="Read your attendance for one online class",
    description=(
        "YOUR derived attendance for ONE class: cumulative connected seconds "
        "(clamped to the class window), the class's real duration "
        "(actual_ended_at - actual_started_at; while live, the time so far) "
        "and attendance_status — \"attended\" when cumulative >= 10 minutes "
        "AND cumulative covers at least half of the real duration (exactly "
        "50% qualifies), else \"not_attended\". Recomputed from the segments "
        "on every read; never stored as an editable flag. Reachable through "
        "an ACTIVE enrollment OR prior participation (having joined), so your "
        "own history survives a dropped enrollment — everything else returns "
        "the SAME 404 as an unknown id. 409 before the class has started. "
        "Identity comes from the Bearer token: there is no student id "
        "parameter, so this surface cannot reach anyone else's row."
    ),
    responses={
        200: {"description": "Your attendance for this class"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown class id, or no enrollment/participation link"},
        409: {"description": "The class has not started (scheduled or cancelled)"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_attendance(
    request: Request,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> ClassAttendanceRead:
    try:
        row, auto_ended = classroom_service.get_attendance_for_student(
            session, student, class_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if auto_ended:
        # The read mechanically ended an overdue class: persist it.
        session.commit()
    return row


@router.get(
    "/{class_id}/transcript",
    response_model=list[ClassMessageRead],
    summary="Read the message transcript of one online class",
    description=(
        "The HISTORICAL transcript of ONE class you actually joined: every "
        "message — teacher's and other participants' alike — ordered "
        "``sequence`` ASC (the ONLY ordering), cursor-paged with "
        "``after_sequence`` (EXCLUSIVE: replay the last sequence you saw "
        "and nothing duplicates) and ``limit``. The decisive fact is a "
        "durable participation segment for YOU in this class: joined for "
        "5 minutes qualifies, the 50% attendance verdict does not matter, "
        "and the access survives leaving the enrollment or switching "
        "teachers — past participation preserves historical access and "
        "nothing revokes it. Enrolled-but-never-joined gets the SAME 404 "
        "as an unknown class id (L6 existence leak): knowing an id is "
        "never enough. Only an ENDED class answers (409 for scheduled/"
        "live/cancelled — the live classroom reads through the WebSocket, "
        "slices 3C/3D). Each message exposes only presentation-safe data "
        "— id, sequence, sender role + display name, body, timestamp — "
        "never an email address, user id or connection id. Read-only: "
        "messages are immutable once written. This is a transcript, NOT a "
        "participant directory: identities appear only where messages were "
        "actually sent."
    ),
    responses={
        200: {"description": "Transcript page (possibly empty)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown class id, or you were never inside this class"},
        409: {"description": "The class has not ended yet (scheduled/live/cancelled)"},
        422: {"description": "Invalid cursor or limit"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_transcript(
    request: Request,
    class_id: uuid.UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[ClassMessageRead]:
    try:
        messages, auto_ended = classroom_service.get_transcript_for_student(
            session,
            student,
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
    "/{class_id}/messages",
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
        "itself never creates a message. While the class is LIVE this "
        "requires CURRENT authorization: an ACTIVE enrollment in this "
        "class's offering (no enrollment ⇒ the SAME 404 as an unknown "
        "class id, L6 existence leak). For an ENDED class the exact slice "
        "3B historical policy applies: a durable participation segment "
        "for you in this class, regardless of current enrollment, teacher "
        "switch or the 50% attendance verdict. SCHEDULED and CANCELLED "
        "classes expose nothing (409). Each message exposes only "
        "presentation-safe data — id, sequence, sender role + display "
        "name, body, timestamp — never an email address, user id or "
        "connection id. Read-only: messages are immutable once written. "
        "An overdue live class is ended mechanically (reason: auto) "
        "first, and this read commits that transition."
    ),
    responses={
        200: {"description": "A page of messages (possibly empty)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown class id, or you are not (or never were) inside it"},
        409: {"description": "The class never happened (scheduled/cancelled)"},
        422: {"description": "Invalid cursor or limit"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_class_messages(
    request: Request,
    class_id: uuid.UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[ClassMessageRead]:
    try:
        messages, auto_ended = classroom_service.read_messages_for_student(
            session,
            student,
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


@router.post(
    "/{class_id}/ws-ticket",
    response_model=ClassWsTicketRead,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a single-use WebSocket ticket for one of your live classes",
    description=(
        "Issue ONE short-lived (120 s), single-use access ticket for the "
        "classroom WebSocket of this class: the browser then opens "
        "``wss://...{ws_path}?ticket=...`` — browsers cannot attach a "
        "Bearer header to a WebSocket handshake, and this is deliberately "
        "not a second authentication system: the ticket is an opaque "
        "capability, not an identity proof. The chain is student → ACTIVE "
        "LearningEnrollment → offering → class: unknown and unrelated ids "
        "answer the SAME 404 (L6 existence leak), and a participation "
        "segment alone (the enrollment has ended) does NOT grant a new "
        "ticket — leaving the offering is the revocation point (409 from "
        "the class not being live, 404 from the relationship being gone). "
        "Only a LIVE class is ticketed (an overdue class is swept and ends "
        "first, committed by this write). The response carries the raw "
        "ticket EXACTLY ONCE: the server keeps only its SHA-256 digest, "
        "never logs it, and redemption is atomic — a second attempt, an "
        "expired ticket or one minted for another class all fail with the "
        "same WebSocket close code 4001."
    ),
    responses={
        201: {"description": "Ticket minted (raw value returned once)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown class id, or no active enrollment link"},
        409: {"description": "The class is not live (scheduled/ended/cancelled)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def issue_class_ws_ticket(
    request: Request,
    class_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> ClassWsTicketRead:
    try:
        ticket = classroom_service.issue_ws_ticket_for_student(
            session, student, class_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return ticket
