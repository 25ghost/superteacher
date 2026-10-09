"""The single source of truth for WebSocket close codes (Phase 3, slice 3C).

Browsers cannot read HTTP status codes during a WebSocket handshake, so
every refusal travels as an APPLICATION close code — the one place the
protocol documents them is here (slice §16). The endpoint accepts the
socket FIRST and then closes with one of these codes, because a close
sent after ``accept`` reliably reaches the client while a close before it
does not.

Codes (stable contract, never renumbered):

- ``NORMAL_CLOSURE`` (1000) — the class ended and everyone left cleanly;
- ``POLICY_VIOLATION`` (1008) — duplicate connection, stale heartbeat or
  a malformed control frame — the participant broke the protocol;
- ``INTERNAL_ERROR`` (1011) — an unexpected server-side failure; nothing
  about it is ever sent to the client;
- ``WS_TICKET_INVALID`` (4001) — the ticket was missing, unknown,
  expired, already used, minted for another class or not a ticket at all;
- ``WS_NOT_AUTHORIZED`` (4003) — the identity behind a valid ticket may
  not enter THIS class (role, ownership, enrollment or account state
  changed after issuance);
- ``CLASS_NOT_LIVE`` (4008) — the class is no longer (or never was)
  ``live`` between ticket issuance and the handshake.

Handshake refusals raised by the service layer name a transport-neutral
reason (``REASON_*``); the endpoint maps reason -> code through
``CLOSE_BY_REASON`` so the service never imports socket machinery.

Client-facing error events (``{"type": "error", "code": ...}``) are a
separate, deliberately small vocabulary: they carry a machine-readable
hint while a refusal or a close is in flight, and never any internal
detail (no SQL, no stack traces, no database ids).
"""
from __future__ import annotations

# --- close codes (RFC 6455 range + 4xxx application codes) ---------------------------
NORMAL_CLOSURE = 1000
POLICY_VIOLATION = 1008
INTERNAL_ERROR = 1011
WS_TICKET_INVALID = 4001
WS_NOT_AUTHORIZED = 4003
CLASS_NOT_LIVE = 4008

# --- handshake refusal reasons (raised by classroom_service) -------------------------
REASON_NOT_AUTHORIZED = "not_authorized"
REASON_CLASS_NOT_LIVE = "class_not_live"
REASON_DUPLICATE = "duplicate_connection"

CLOSE_BY_REASON: dict[str, int] = {
    REASON_NOT_AUTHORIZED: WS_NOT_AUTHORIZED,
    REASON_CLASS_NOT_LIVE: CLASS_NOT_LIVE,
    REASON_DUPLICATE: POLICY_VIOLATION,
}

# --- event type vocabulary -----------------------------------------------------------
EVENT_PRESENCE_JOINED = "presence.joined"
EVENT_PRESENCE_LEFT = "presence.left"
EVENT_CLASS_STARTED = "class.started"
EVENT_CLASS_ENDED = "class.ended"
#: Internal only — revokes a student's live connection when their
#: enrollment ends. Never forwarded to a client as-is (§28).
EVENT_CONNECTION_REVOKE = "connection.revoke"
EVENT_HEARTBEAT = "presence.heartbeat"
EVENT_ERROR = "error"

# --- client-facing error codes -------------------------------------------------------
ERROR_INVALID_CONTROL_MESSAGE = "INVALID_CONTROL_MESSAGE"
ERROR_CLASS_NOT_LIVE = "CLASS_NOT_LIVE"
ERROR_NOT_AUTHORIZED = "NOT_AUTHORIZED"
ERROR_HEARTBEAT_REJECTED = "HEARTBEAT_REJECTED"
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"

__all__ = [
    "NORMAL_CLOSURE",
    "POLICY_VIOLATION",
    "INTERNAL_ERROR",
    "WS_TICKET_INVALID",
    "WS_NOT_AUTHORIZED",
    "CLASS_NOT_LIVE",
    "REASON_NOT_AUTHORIZED",
    "REASON_CLASS_NOT_LIVE",
    "REASON_DUPLICATE",
    "CLOSE_BY_REASON",
    "EVENT_PRESENCE_JOINED",
    "EVENT_PRESENCE_LEFT",
    "EVENT_CLASS_STARTED",
    "EVENT_CLASS_ENDED",
    "EVENT_CONNECTION_REVOKE",
    "EVENT_HEARTBEAT",
    "EVENT_ERROR",
    "ERROR_INVALID_CONTROL_MESSAGE",
    "ERROR_CLASS_NOT_LIVE",
    "ERROR_NOT_AUTHORIZED",
    "ERROR_HEARTBEAT_REJECTED",
    "ERROR_INTERNAL_ERROR",
]
