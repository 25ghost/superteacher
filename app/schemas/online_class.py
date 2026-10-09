"""Pydantic schemas for Phase 3 online classes and the text classroom.

Boundary: scheduling, launching and closing one live class inside an
existing teaching offering (teacher self-service), listing and joining
those classes (student self-service), and the durable classroom payloads
(transcript, attendance, WebSocket ticket).

Security rules baked into these schemas:

- no request field can select a *teacher* or *student*: both identities
  come from the Bearer token (or, for the WebSocket, from a
  participation-authorized ticket), so a body can only ever describe the
  class window or the caller's own message;
- ``extra="forbid"`` rejects unknown keys with a 422 — including
  ``teaching_offering_id``, ``teacher_id`` and anything role-shaped; the
  offering comes from the path and is ownership-checked by the service;
- timestamps must be timezone-aware: the platform stores ``timestamptz``
  and does no per-user timezone handling, so a naive datetime is a 422
  rather than a silently-interpreted value;
- no response schema includes credential material — the WebSocket ticket
  is returned exactly once, and messages expose only the sender's role
  and display name, never email, phone or account state.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Derived, never stored: the attendance verdict recomputed on every read.
AttendanceStatusLiteral = Literal["attended", "not_attended"]


def _require_aware(value: datetime | None, field_name: str) -> None:
    """Reject naive datetimes — the database column is ``timestamptz``."""
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone offset")


class OnlineClassSessionCreate(BaseModel):
    """Schedule a class (``POST /me/teacher/offerings/{id}/classes``).

    Only the window is supplied: the offering comes from the path and is
    ownership-checked, the teacher derives from the offering, and the
    optional ``lesson_id`` may only name a lesson of *this* offering.
    """

    model_config = ConfigDict(extra="forbid")

    scheduled_start_at: datetime
    scheduled_end_at: datetime
    lesson_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> "OnlineClassSessionCreate":
        _require_aware(self.scheduled_start_at, "scheduled_start_at")
        _require_aware(self.scheduled_end_at, "scheduled_end_at")
        if self.scheduled_end_at <= self.scheduled_start_at:
            raise ValueError("scheduled_end_at must be after scheduled_start_at")
        return self


class OnlineClassSessionUpdate(BaseModel):
    """Amend a still-scheduled class (``PATCH .../classes/{id}``).

    Only ``scheduled`` classes are editable; after ``SCHEDULED → LIVE``
    every scheduling field is immutable. ``lesson_id: null`` clears the
    optional lesson link.
    """

    model_config = ConfigDict(extra="forbid")

    scheduled_start_at: datetime | None = None
    scheduled_end_at: datetime | None = None
    lesson_id: UUID | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "OnlineClassSessionUpdate":
        supplied = self.model_fields_set
        if not supplied:
            raise ValueError(
                "supply at least one of: scheduled_start_at, "
                "scheduled_end_at, lesson_id"
            )
        for name in ("scheduled_start_at", "scheduled_end_at"):
            if name in supplied and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        if "scheduled_start_at" in supplied:
            _require_aware(self.scheduled_start_at, "scheduled_start_at")
        if "scheduled_end_at" in supplied:
            _require_aware(self.scheduled_end_at, "scheduled_end_at")
        if (
            self.scheduled_start_at is not None
            and self.scheduled_end_at is not None
            and self.scheduled_end_at <= self.scheduled_start_at
        ):
            raise ValueError("scheduled_end_at must be after scheduled_start_at")
        return self


class OnlineClassSessionRead(BaseModel):
    """One class session — the same shape for the teacher's own surface and
    a student's class list, so both roles read one object."""

    class_id: UUID
    teaching_offering_id: UUID
    lesson_id: UUID | None = None
    scheduled_start_at: datetime
    scheduled_end_at: datetime
    actual_started_at: datetime | None = None
    actual_ended_at: datetime | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class ClassWsTicketRead(BaseModel):
    """One freshly minted, single-use WebSocket access ticket.

    Returned exactly once by the HTTP endpoint; the server keeps only the
    SHA-256 digest. ``ws_path`` is the path the socket must connect to —
    the ticket is bound to that one class session.
    """

    ticket: str
    expires_at: datetime
    ws_path: str


class MessageSenderRead(BaseModel):
    """Who wrote a message, as far as the classroom needs to know."""

    role: str
    display_name: str


class ClassMessageRead(BaseModel):
    """One durable class message as the transcript presents it.

    Exactly the presentation-safe shape (never an address, user id or
    connection id): ``message_id``, ``sequence``, the sender's role and
    display name, the body and the server timestamp. The client retry key
    stays a database concern — it is not part of the historical record a
    viewer sees.
    """

    message_id: UUID
    sequence: int
    sender: MessageSenderRead
    body: str
    sent_at: datetime


class MessageSendFrame(BaseModel):
    """One ``message.send`` frame of the live classroom (slice 3D).

    The ONLY client-originated application message of the WebSocket: the
    connection itself decides sender, role and class, so this schema
    carries no identity field at all — a client-supplied ``sender``,
    ``sequence``, ``message_id`` or timestamp is simply ignored
    (``extra="ignore"``; unlike HTTP bodies this frame must stay
    forward-compatible, and an unknown key must never influence the
    record). The two client-chosen values are both bounded:

    - ``client_message_id`` — the retry/idempotency key, trimmed and
      1..64 characters; the same value from the same sender in the same
      class can never create a second message;
    - ``body`` — 1..2000 characters, whitespace-only rejected.

    A payload that fails validation is refused with the stable client
    code ``INVALID_MESSAGE`` before any database work happens.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["message.send"]
    client_message_id: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _reject_blank_values(self) -> "MessageSendFrame":
        self.client_message_id = self.client_message_id.strip()
        if not self.client_message_id:
            raise ValueError("client_message_id must not be blank")
        if not self.body.strip():
            raise ValueError("body must not be blank")
        return self


class AttendanceSegmentRead(BaseModel):
    """One participation interval of one student in one class."""

    joined_at: datetime
    last_seen_at: datetime
    left_at: datetime | None = None


class ClassAttendanceRead(BaseModel):
    """One student's participation in one class, derived at read time.

    Nothing here is stored as an editable verdict: the segments are the
    facts, the numbers below are recomputed on every read.

    - ``cumulative_seconds`` — connected time actually accumulated by the
      student (clamped to the class window, never overlapping);
    - ``actual_seconds`` — the class's real duration (``actual_ended_at -
      actual_started_at``; while the class is live, the time so far);
    - ``attendance_status`` — ``attended`` when
      ``cumulative_seconds * 2 >= actual_seconds`` (>= 50% qualifies, ties
      included) and the class actually ran; ``not_attended`` otherwise.

    A cancelled class never started, so its denominator is zero and every
    row reads ``not_attended``.
    """

    student_id: UUID
    full_name: str
    online: bool = False
    cumulative_seconds: int
    actual_seconds: int
    attendance_status: AttendanceStatusLiteral
    segments: list[AttendanceSegmentRead] = Field(default_factory=list)


class ClassParticipantRead(BaseModel):
    """Who has ever been inside this class (teacher's participant roster).

    Present = at least one attendance segment exists — a student who
    joined and already left still appears, with ``online`` false. Ordering
    is the service's: ``full_name`` then ``student_id``.
    """

    student_id: UUID
    full_name: str
    online: bool
    first_joined_at: datetime
    last_seen_at: datetime
    segment_count: int
