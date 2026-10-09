"""The classroom WebSocket — transport, heartbeat and presence (slice 3C).

This module owns the SOCKET, never the business rules: every decision it
makes is delegated to ``classroom_service`` (tickets, authorization,
join/leave/heartbeat) or to the realtime package (registry, bus, close
codes). It is the protocol contract of the classroom (§47) —
``ClassWsTicketRead.ws_path`` points here:

Connection
    ``wss://<host>/ws/classes/{class_id}?ticket=<opaque>`` — mounted on
    the app root, deliberately NOT under ``/api/v1`` (it is not an HTTP
    resource). There is no ``Authorization`` header on a browser socket;
    the ticket IS the credential: a short-lived, single-use, class-scoped
    capability minted by the HTTP endpoints
    (``POST /me/teacher/offerings/{oid}/classes/{cid}/ws-ticket`` and
    ``POST /me/classes/{cid}/ws-ticket``), consumed atomically by this
    handshake. Missing/invalid/expired/used/wrong-class tickets all get
    the same answer: **accept the socket first, then close 4001**, so the
    application close code reaches the client (§16).

Handshake order (§11 — a refusal at ANY step means no presence event and
no broadcast; a student's attendance only exists for the moment they
joined a class that was still live)
    1. ``accept()``;
    2. redeem the ticket (single atomic UPDATE, commit the use);
    3. revalidate identity + class: ACCOUNT still active (§11 step 5),
       sweep the overdue window, load the class, recheck
       role/approval/ownership/ENROLLMENT, demand ``live`` — and for
       students open the attendance segment through the existing
       ``join_class`` (the single owner of segment rules);
    4. register in the in-process registry — one live connection per
       participant per class, a duplicate refused with **1008** (the
       segment's unique open index backstops this across workers);
    5. re-check ``live`` — the class may have ended between
       authorization and registration (§38): refused with **4008**,
       before any snapshot or presence broadcast;
    6. snapshot (``presence.joined`` for every other participant, others
       first then self — §20), broadcast your join to everyone else
       (never to yourself);
    7. live loop: control frames + heartbeat watchdog.

Close codes (stable, documented in ``app.realtime.close_codes``)
    1000 class ended cleanly · 1008 duplicate/stale/malformed ·
    1011 internal error · 4001 ticket invalid · 4003 not authorized ·
    4008 class not live. Refusals are sent as a close code only — no
    error text, no ids, no stack traces (§45).

Events (client-visible vocabulary, nothing else ever crosses the wire)
    * ``presence.joined`` / ``presence.left`` — ``{"type",
      "participant": {"role", "display_name", "participant_ref"}}``;
      ``participant_ref`` is opaque and deterministic (SHA-256 of
      class+user, 32 hex), never a raw id. A newcomer receives the
      snapshot BEFORE their own join is broadcast to the others.
      ``presence.left`` is published only while the class is still live
      (§36): a class end bulk-closes everyone and sends ``class.ended``
      instead.
    * ``class.started`` / ``class.ended`` — ``{"type", "class_id"}``,
      relayed from the bus exactly when the database transition commits
      (manual End, overdue auto-end, and the teacher's Start alike —
      §22/§25/§36). On ``class.ended`` every socket is closed 1000 in
      the order: event → close → unregister.
    * ``message.created`` — one committed classroom message (slice 3D):
      ``{"type", "message_id", "sequence", "sender": {"role",
      "display_name"}, "body", "sent_at", "client_message_id"}``. It
      reaches every participant of the class only AFTER the row is
      durably committed (the publish rides the insert's transaction) and
      exactly once per participant (class-scoped bus, no local dispatch
      alongside the notification). The sender sees the same frame — its
      ``message_id``/``sequence`` are the canonical identity it
      reconciles against.
    * ``error`` — ``{"type": "error", "code": ...}`` from a small fixed
      set (INVALID_CONTROL_MESSAGE, INVALID_MESSAGE,
      CLIENT_MESSAGE_ID_CONFLICT, RATE_LIMITED, CLASS_NOT_LIVE,
      NOT_AUTHORIZED, HEARTBEAT_REJECTED, INTERNAL_ERROR). Most are
      advisory — the socket stays open (a bad payload or a full quota is
      not a protocol violation) — but a refusal that invalidates the
      socket's own precondition terminates it: NOT_AUTHORIZED closes 4003
      and CLASS_NOT_LIVE closes 4008, both through the one idempotent
      cleanup path.

Heartbeat (§32/§33)
    The client sends ``{"type": "presence.heartbeat"}`` every
    ``WS_HEARTBEAT_INTERVAL_SECONDS`` (20 s). It is a control frame —
    never a ``ClassMessage``, never in the transcript. For students it
    rides the existing ``heartbeat`` service (last-seen touch AND the
    auto-end sweep: a class whose window passed ends here, persists, and
    its ``class.ended`` event closes every socket). For teachers it is a
    class-still-live probe. Any frame counts as liveness; a socket silent
    for ``WS_STALE_TIMEOUT_SECONDS`` (60 s) is terminated with 1008 and
    its segment closed (§33).

Teardown (one idempotent path — §21/§22/§29)
    Close student segment (tolerating "already closed" — the class-end
    bulk close or a revoke may have won) → unregister → publish
    ``presence.left`` if the class is still live. Class-end shutdowns
    skip the segment close and the left event (already handled by the
    transition); enrollment revocation closes the socket with 4003
    after an ``error`` frame. Every branch runs through the same
    ``cleanup`` hook, so double-fires (watchdog vs disconnect, event vs
    crash) are harmless.

Messages (slice 3D — transport only, the rules live in the service)
    One new client frame, ``{"type": "message.send",
    "client_message_id", "body"}``. This module parses it with the
    ``MessageSendFrame`` schema (a payload failure is the advisory
    ``INVALID_MESSAGE`` — framing failures stay
    ``INVALID_CONTROL_MESSAGE``), then delegates everything else to
    ``classroom_service.send_class_message`` through a thread worker with
    its OWN short-lived session: authorization, class state, the retry
    key, the quota and the sequence are service decisions, never this
    endpoint's. A first submission answers with the broadcast
    ``message.created`` (the sender included); an idempotent retry is
    acknowledged directly to the requesting socket with the canonical
    message and is NEVER broadcast twice.

Forbidden here (§16/§28): domain rules for messages (the service owns
    them), Redis, WebRTC, audio/video, generic chat, a second auth
    system — and no internal event name or detail ever reaches a client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.enums import OnlineClassStatus
from app.models.online_class_session import OnlineClassSession
from app.models.user import User
from app.realtime import close_codes as cc
from app.realtime.connections import LiveConnection, participant_ref, registry
from app.realtime.event_bus import get_event_bus
from app.schemas.online_class import MessageSendFrame
from app.services import classroom_service
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: connection_id entropy for attendance segments (unguessable, 32 chars).
_SEGMENT_ID_BYTES = 24


# --- thread workers (own Session per phase; the endpoint commits/rolls back) ----------


def _consume_ticket(class_id: uuid.UUID, raw_ticket: str) -> User | None:
    """Redeem one ticket, committing its single use (§10)."""
    session = SessionLocal()
    try:
        user = classroom_service.consume_ws_ticket(session, class_id, raw_ticket)
        if user is None:
            session.rollback()
            return None
        session.commit()
        return user
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _authorize(
    user_id: uuid.UUID, class_id: uuid.UUID, connection_id: str
) -> classroom_service.WsHandshakeIdentity:
    """Revalidate + join, committing the segment (or the swept end)."""
    session = SessionLocal()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise classroom_service.ClassroomConnectionRefused(
                cc.REASON_NOT_AUTHORIZED
            )
        try:
            identity = classroom_service.authorize_ws_connection(
                session, user, class_id, connection_id
            )
        except classroom_service.ClassroomConnectionRefused as exc:
            if exc.reason == cc.REASON_CLASS_NOT_LIVE:
                # A sweep may have fired inside: persist that end (and its
                # class.ended event) even though THIS handshake failed.
                session.commit()
            else:
                session.rollback()
            raise
        session.commit()
        return identity
    except classroom_service.ClassroomConnectionRefused:
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _remote_students(
    class_id: uuid.UUID,
    identity: classroom_service.WsHandshakeIdentity,
    local_student_ids: set[uuid.UUID],
) -> list[dict]:
    """The database half of the snapshot (§20) — students of other workers."""
    session = SessionLocal()
    try:
        return classroom_service.remote_students_in_class(
            session,
            class_id,
            local_student_ids=local_student_ids,
            exclude_student_id=identity.student_id,
        )
    finally:
        session.close()


def _publish(event: dict) -> None:
    """Publish on a private session and commit — fires pg_notify (§13)."""
    session = SessionLocal()
    try:
        get_event_bus().publish(session, event)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _class_status(class_id: uuid.UUID) -> str | None:
    session = SessionLocal()
    try:
        class_session = session.get(OnlineClassSession, class_id)
        return class_session.status if class_session is not None else None
    finally:
        session.close()


def _discard_segment(connection: LiveConnection) -> None:
    """Close this connection's student segment — tolerate every loss.

    The class-end bulk close or a concurrent revoke may already have
    finalized (or the connection may own none at all): those are not
    errors, they are the same decision made elsewhere first (§21).
    """
    if connection.student_id is None:
        return
    session = SessionLocal()
    try:
        user = session.get(User, connection.user_id)
        if user is None:
            session.rollback()
            return
        try:
            classroom_service.leave_class(
                session, user, connection.class_id, connection.connection_id
            )
        except (LearningNotFoundError, LearningConflictError):
            session.rollback()
            return
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "failed to close attendance segment",
            extra={"class_id": str(connection.class_id)},
        )
    finally:
        session.close()


def _heartbeat_worker(connection: LiveConnection) -> str:
    """Run one heartbeat probe; return a transport outcome (never raises)."""
    session = SessionLocal()
    try:
        user = session.get(User, connection.user_id)
        if user is None:
            return "not_authorized"
        if connection.role == "teacher":
            auto_ended = classroom_service.heartbeat_teacher(
                session, connection.class_id
            )
        else:
            _, auto_ended = classroom_service.heartbeat(
                session, user, connection.class_id, connection.connection_id
            )
        session.commit()
        # "ended": this probe ended the overdue class — committed, its
        # class.ended event drives the close; say nothing to the client.
        return "ended" if auto_ended else "ok"
    except LearningConflictError as exc:
        session.rollback()
        message = str(exc)
        if "requires a live class" in message:
            return "class_not_live"
        return "rejected"
    except (LearningNotFoundError, LearningForbiddenError):
        session.rollback()
        return "not_authorized"
    except Exception:
        session.rollback()
        logger.exception("websocket heartbeat failed")
        return "error"
    finally:
        session.close()


def _send_message_worker(connection: LiveConnection, frame: MessageSendFrame) -> dict:
    """Commit one ``message.send`` in its own short-lived session (§3D).

    The socket never holds a session: this worker opens one, delegates
    every rule to ``classroom_service.send_class_message`` and commits
    (or rolls back) the transaction the service prepared. The class-end
    sweep discovered while refusing a message is persisted exactly like
    the handshake persists it — an auto-ended class must broadcast
    ``class.ended`` even though THIS send failed.

    Returns a transport-neutral verdict; it never raises, so a database
    hiccup becomes the advisory INTERNAL_ERROR frame instead of killing
    the loop. Bodies, tickets and exception text are never logged.
    """
    session = SessionLocal()
    try:
        user = session.get(User, connection.user_id)
        if user is None:
            return {"outcome": "error", "code": cc.ERROR_NOT_AUTHORIZED}
        try:
            kind, event = classroom_service.send_class_message(
                session,
                user,
                connection.class_id,
                frame.client_message_id,
                frame.body,
            )
        except classroom_service.MessageRejected as exc:
            if exc.code == cc.ERROR_CLASS_NOT_LIVE:
                # Persist an overdue sweep this call may have triggered.
                session.commit()
            else:
                session.rollback()
            return {"outcome": "error", "code": exc.code}
        session.commit()
        if kind == "duplicate":
            return {"outcome": "duplicate", "event": event}
        return {"outcome": "created"}
    except Exception:  # noqa: BLE001 — advisory INTERNAL_ERROR, never a traceback
        session.rollback()
        logger.exception(
            "websocket message send failed",
            extra={"class_id": str(connection.class_id)},
        )
        return {"outcome": "error", "code": cc.ERROR_INTERNAL_ERROR}
    finally:
        session.close()


# --- connection lifecycle (async, event-loop side) -------------------------------------


async def _refuse(websocket: WebSocket, close_code: int, reason: str) -> None:
    """Close a socket that never registered — code only, no detail (§45)."""
    logger.info(
        "websocket handshake refused",
        extra={"reason": reason, "close_code": close_code},
    )
    try:
        await websocket.close(code=close_code)
    except Exception:  # noqa: BLE001 — the client may already be gone
        pass


def _presence_of(connection: LiveConnection) -> dict:
    return {
        "role": connection.role,
        "display_name": connection.display_name,
        "participant_ref": connection.participant_ref,
    }


#: Keys of ``message.created`` that may reach a client. The bus payload
#: also carries ``class_id`` (its routing key) — routing keys never
#: reach the wire (§19), so delivery always goes through this picker.
_CLIENT_EVENT_KEYS = (
    "type",
    "message_id",
    "sequence",
    "sender",
    "body",
    "sent_at",
    "client_message_id",
)


def _client_event(event: dict) -> dict:
    """The wire view of a ``message.created`` payload (no routing keys)."""
    return {key: event[key] for key in _CLIENT_EVENT_KEYS if key in event}


async def _handle_message_send(connection: LiveConnection, payload: dict) -> None:
    """One ``message.send`` frame: validate, delegate, deliver the answer.

    Validation happens HERE (transport parsing, §19) so a malformed
    payload never opens a database session; every domain decision is the
    service's. Outcomes:

    - ``created`` — silent: the committed ``message.created`` broadcast
      is already on its way to everyone, the sender included;
    - ``duplicate`` — the canonical message, sent ONLY to this socket so
      a retry is acknowledged without a second room-wide broadcast;
    - refusal — the stable error code; NOT_AUTHORIZED and
      CLASS_NOT_LIVE additionally terminate the socket (4003 / 4008)
      through the one idempotent cleanup path, because an authorization
      or lifecycle change invalidates the connection itself (§18),
      while payload/quota/conflict failures are merely advisory.
    """
    try:
        frame = MessageSendFrame.model_validate(payload)
    except ValidationError:
        await connection.send_json(
            {"type": cc.EVENT_ERROR, "code": cc.ERROR_INVALID_MESSAGE}
        )
        return

    result = await asyncio.to_thread(_send_message_worker, connection, frame)
    if result["outcome"] == "duplicate":
        await connection.send_json(_client_event(result["event"]))
        return
    if result["outcome"] == "error":
        code = result["code"]
        if code == cc.ERROR_NOT_AUTHORIZED:
            await _reject(
                connection, error_code=code, close_code=cc.WS_NOT_AUTHORIZED
            )
        elif code == cc.ERROR_CLASS_NOT_LIVE:
            await _reject(connection, error_code=code, close_code=cc.CLASS_NOT_LIVE)
        else:
            await connection.send_json({"type": cc.EVENT_ERROR, "code": code})


async def _cleanup(connection: LiveConnection, close_code: int | None) -> None:
    """Cancellation-proof wrapper around the ONE teardown path.

    A test client (or any ASGI server) may cancel this connection's task
    while teardown is awaiting a thread worker — after ``cleaned`` was
    already set but before ``unregister`` ran, which would strand the
    registry entry forever. The shielded inner task is a separate task,
    so the cancellation hits this await instead and teardown still
    finishes on the loop.
    """
    await asyncio.shield(_cleanup_now(connection, close_code))


async def _cleanup_now(connection: LiveConnection, close_code: int | None) -> None:
    """The ONE teardown path (§21/§22/§29) — idempotent by construction.

    Segment (if still live and this is a student) → close (if asked) →
    unregister → ``presence.left`` (only while the class is live: a class
    end already bulk-closed everyone and sent ``class.ended``).
    """
    async with connection._cleanup_lock:
        if connection.cleaned:
            if close_code is not None:
                try:
                    await connection.websocket.close(code=close_code)
                except Exception:  # noqa: BLE001
                    pass
            return
        connection.cleaned = True

    status = await asyncio.to_thread(_class_status, connection.class_id)
    live = status == OnlineClassStatus.LIVE.value

    if live and connection.student_id is not None:
        await asyncio.to_thread(_discard_segment, connection)

    if close_code is not None:
        try:
            await connection.websocket.close(code=close_code)
        except Exception:  # noqa: BLE001 — already closed by the peer
            pass

    registry.unregister(connection)

    if live:
        try:
            await asyncio.to_thread(
                _publish,
                {
                    "type": cc.EVENT_PRESENCE_LEFT,
                    "class_id": str(connection.class_id),
                    "participant": _presence_of(connection),
                },
            )
        except Exception:  # noqa: BLE001 — teardown must not fail on publish
            logger.exception(
                "failed to publish presence.left",
                extra={"class_id": str(connection.class_id)},
            )


def _install_hooks(connection: LiveConnection) -> None:
    """Wire bus delivery and cleanup onto a freshly registered socket."""

    async def handle_event(event: dict) -> None:
        event_type = event.get("type")
        if event_type in (cc.EVENT_PRESENCE_JOINED, cc.EVENT_PRESENCE_LEFT):
            participant = event.get("participant")
            if isinstance(participant, dict):
                # §19 shape exactly — routing keys never reach a client.
                await connection.send_json(
                    {"type": event_type, "participant": participant}
                )
        elif event_type in (cc.EVENT_CLASS_STARTED, cc.EVENT_CLASS_ENDED):
            await connection.send_json(
                {"type": event_type, "class_id": event.get("class_id")}
            )
            if event_type == cc.EVENT_CLASS_ENDED:
                # §22 order: the event arrived, now close 1000 → cleanup
                # unregisters (segments were finalized by the transition).
                await connection.cleanup(close_code=cc.NORMAL_CLOSURE)
        elif event_type == cc.EVENT_MESSAGE_CREATED:
            # Already committed (the publish rode its transaction); the
            # sender's own echo takes this exact path, so a participant
            # sees each message once regardless of worker locality.
            await connection.send_json(_client_event(event))
        elif event_type == cc.EVENT_CONNECTION_REVOKE:
            # §29: the student's enrollment ended — say something safe,
            # then close 4003 and finalize their segment.
            await connection.send_json(
                {"type": cc.EVENT_ERROR, "code": cc.ERROR_NOT_AUTHORIZED}
            )
            await connection.cleanup(close_code=cc.WS_NOT_AUTHORIZED)

    async def cleanup(*, close_code: int | None = None) -> None:
        await _cleanup(connection, close_code)

    connection.handle_event = handle_event
    connection.cleanup = cleanup


async def _handle_frame(connection: LiveConnection, message: dict) -> None:
    """One client frame: heartbeat, ``message.send``, or a refused frame (§34)."""
    text = message.get("text")
    if text is None:
        await connection.send_json(
            {"type": cc.EVENT_ERROR, "code": cc.ERROR_INVALID_CONTROL_MESSAGE}
        )
        return
    try:
        payload = json.loads(text)
        event_type = payload.get("type") if isinstance(payload, dict) else None
    except ValueError:
        event_type = None

    if event_type == cc.EVENT_MESSAGE_SEND:
        await _handle_message_send(connection, payload)
        return

    if event_type != cc.EVENT_HEARTBEAT:
        # Unrecognized frames (chat, random JSON, wrong-shaped objects)
        # are refused, never executed (§28).
        await connection.send_json(
            {"type": cc.EVENT_ERROR, "code": cc.ERROR_INVALID_CONTROL_MESSAGE}
        )
        return

    outcome = await asyncio.to_thread(_heartbeat_worker, connection)
    if outcome in ("ok", "ended"):
        # "ok": silent by design (client keeps its own cadence).
        # "ended": class.ended is already committed and on its way.
        return
    await _reject(
        connection,
        error_code={
            "class_not_live": cc.ERROR_CLASS_NOT_LIVE,
            "rejected": cc.ERROR_HEARTBEAT_REJECTED,
            "not_authorized": cc.ERROR_NOT_AUTHORIZED,
        }.get(outcome, cc.ERROR_INTERNAL_ERROR),
        close_code={
            "class_not_live": cc.CLASS_NOT_LIVE,
            "rejected": cc.POLICY_VIOLATION,
            "not_authorized": cc.WS_NOT_AUTHORIZED,
        }.get(outcome, cc.INTERNAL_ERROR),
    )


async def _reject(connection: LiveConnection, *, error_code: str, close_code: int) -> None:
    """Send a safe error frame, then close with the mapped code (§35)."""
    await connection.send_json({"type": cc.EVENT_ERROR, "code": error_code})
    await connection.cleanup(close_code=close_code)


async def _watchdog(connection: LiveConnection, settings) -> None:
    """Terminate a socket that stopped sending anything (§33)."""
    while True:
        # Read per iteration so tests can shrink the timeout live.
        stale_timeout = float(settings.WS_STALE_TIMEOUT_SECONDS)
        await asyncio.sleep(max(0.05, min(1.0, stale_timeout / 3.0)))
        if time.monotonic() - connection.last_activity > stale_timeout:
            logger.info(
                "websocket stale; terminating",
                extra={
                    "class_id": str(connection.class_id),
                    "user_id": str(connection.user_id),
                },
            )
            await connection.cleanup(close_code=cc.POLICY_VIOLATION)
            return


# --- the endpoint ----------------------------------------------------------------------


@router.websocket("/ws/classes/{class_id}")
async def classroom_websocket(websocket: WebSocket, class_id: uuid.UUID) -> None:
    """Accept → redeem → revalidate → register → snapshot → live loop."""
    try:
        await websocket.accept()
    except Exception:  # noqa: BLE001 — client vanished during the handshake
        return

    settings = get_settings()

    raw_ticket = websocket.query_params.get("ticket") or ""
    if not raw_ticket:
        await _refuse(websocket, cc.WS_TICKET_INVALID, "missing ticket")
        return

    # §11 steps 1-2: redeem (one atomic UPDATE — commit the single use).
    try:
        user = await asyncio.to_thread(_consume_ticket, class_id, raw_ticket)
    except Exception:
        logger.exception("ticket redemption failed", extra={"class_id": str(class_id)})
        await _refuse(websocket, cc.INTERNAL_ERROR, "ticket redemption error")
        return
    if user is None:
        await _refuse(websocket, cc.WS_TICKET_INVALID, "invalid ticket")
        return

    # §11 steps 3-6: revalidate identity/class; students join here.
    connection_id = secrets.token_urlsafe(_SEGMENT_ID_BYTES)
    try:
        identity = await asyncio.to_thread(
            _authorize, user.id, class_id, connection_id
        )
    except classroom_service.ClassroomConnectionRefused as exc:
        await _refuse(websocket, cc.CLOSE_BY_REASON[exc.reason], exc.reason)
        return
    except Exception:
        logger.exception(
            "handshake authorization failed", extra={"class_id": str(class_id)}
        )
        await _refuse(websocket, cc.INTERNAL_ERROR, "authorization error")
        return

    # §11 step 7: one live connection per participant per class (§15).
    connection = LiveConnection(
        websocket=websocket,
        class_id=class_id,
        user_id=identity.user_id,
        role=identity.role,
        display_name=identity.display_name,
        participant_ref=participant_ref(class_id, identity.user_id),
        connection_id=connection_id,
        teaching_offering_id=identity.teaching_offering_id,
        student_id=identity.student_id,
    )
    if not registry.try_register(connection):
        # Cross-worker twin backstopped at the segment unique index;
        # here (same worker) the join above already opened a segment for
        # a student — discard it so this refused handshake leaves no
        # attendance trace (§15: rejection creates no second segment).
        await asyncio.to_thread(_discard_segment, connection)
        await _refuse(websocket, cc.POLICY_VIOLATION, "duplicate connection")
        return

    _install_hooks(connection)
    bus = get_event_bus()
    bus.bind_loop(asyncio.get_running_loop())

    # From registration on, EVERY exit path — including a task cancelled
    # mid-snapshot by an ASGI/test client tearing the socket down — must
    # reach the ONE teardown, or the registry entry would leak (§21).
    watchdog: asyncio.Task | None = None
    close_code: int | None = None
    try:
        # §38 race "class ending during handshake": the transition may
        # have committed between authorization and this registration, so
        # re-check BEFORE any snapshot or broadcast — an ended class
        # never receives presence. Nothing is left to close: joining and
        # ending serialize on the class row lock, so the segment (if one
        # opened) was already finalized at actual_ended_at by that very
        # transition, and the cleanup below skips presence.left (§36).
        status = await asyncio.to_thread(_class_status, class_id)
        if status != OnlineClassStatus.LIVE.value:
            await connection.cleanup(close_code=cc.CLASS_NOT_LIVE)
            return

        try:
            await asyncio.to_thread(bus.ensure_listening)
        except Exception:  # noqa: BLE001 — degrade to no delivery, never refuse
            logger.exception("realtime listener failed to start")

        # §11 steps 8-11: snapshot (others → self) then broadcast the join.
        local_connections = registry.connections_for_class(class_id)
        local_others = [
            other
            for other in local_connections
            if other.connection_id != connection.connection_id
        ]
        local_student_ids = {
            other.student_id for other in local_connections if other.student_id
        }
        remote_students = await asyncio.to_thread(
            _remote_students, class_id, identity, local_student_ids
        )
        for entry in [_presence_of(other) for other in local_others] + remote_students:
            await connection.send_json(
                {"type": cc.EVENT_PRESENCE_JOINED, "participant": entry}
            )
        await connection.send_json(
            {"type": cc.EVENT_PRESENCE_JOINED, "participant": _presence_of(connection)}
        )
        await asyncio.to_thread(
            _publish,
            {
                "type": cc.EVENT_PRESENCE_JOINED,
                "class_id": str(class_id),
                "participant": _presence_of(connection),
                "except_ref": connection.participant_ref,
            },
        )

        # §11 step 12: the live loop.
        watchdog = asyncio.create_task(_watchdog(connection, settings))
        while True:
            message = await websocket.receive()
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                break
            if message_type != "websocket.receive":
                continue
            connection.last_activity = time.monotonic()
            await _handle_frame(connection, message)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(
            "websocket session failed",
            extra={"class_id": str(class_id), "user_id": str(identity.user_id)},
        )
        close_code = cc.INTERNAL_ERROR
    finally:
        if watchdog is not None:
            watchdog.cancel()
        await connection.cleanup(close_code=close_code)
