"""Data access for ``class_messages`` reads and writes (Phase 3, slice 3D).

- :func:`list_messages_after` pages one class's messages with a cursor
  (``sequence > after_sequence``), ASC — reconnect recovery and the
  historical transcript are the same query from different starting
  points, and per-class sequence uniqueness makes the cursor unambiguous;
- :func:`find_by_idempotency_key` resolves a client retry to its
  canonical row (the unique index behind it is the real guarantee);
- :func:`next_sequence` + :func:`create_message` are the write half: the
  caller MUST already hold the class-session row lock (``SELECT ... FOR
  UPDATE`` via ``online_class_repository.get_by_id_for_update``), which
  is what makes ``MAX(sequence) + 1`` safe across workers and what makes
  "still live?" and ordering one atomic decision. No business rules live
  here — authorization, class state, idempotency policy and rate limits
  belong to ``classroom_service``;
- no mutation helpers exist at all: messages are immutable once written.

Lock order (inherited from ``online_class_repository``):

    teaching_offerings  ->  online_class_sessions  ->  (messages, segments)
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.class_message import ClassMessage


def list_messages_after(
    session: Session,
    class_session_id: uuid.UUID,
    *,
    after_sequence: int = 0,
    limit: int = 100,
) -> list[ClassMessage]:
    """Messages of one class with ``sequence > after_sequence``, ASC.

    The cursor is exclusive (``>``), so replaying the last seen sequence
    never duplicates a row; ASC order is the classroom's historical order
    (sequence is unique per class). ``limit`` bounds one page — a huge
    transcript pages instead of loading at once.
    """
    stmt = (
        select(ClassMessage)
        .where(
            ClassMessage.class_session_id == class_session_id,
            ClassMessage.sequence > after_sequence,
        )
        .order_by(ClassMessage.sequence)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def find_by_idempotency_key(
    session: Session,
    *,
    class_session_id: uuid.UUID,
    sender_user_id: uuid.UUID,
    client_message_id: str,
) -> ClassMessage | None:
    """The canonical row of one sender's retry key in one class, or ``None``.

    The scope is exactly the unique index
    ``uq_class_messages_class_sender_client_key`` — two different senders
    (or the same sender in two classes) may reuse the same
    ``client_message_id`` freely; only THIS combination is one message.
    """
    stmt = (
        select(ClassMessage)
        .where(
            ClassMessage.class_session_id == class_session_id,
            ClassMessage.sender_user_id == sender_user_id,
            ClassMessage.client_message_id == client_message_id,
        )
        .limit(1)
    )
    return session.scalar(stmt)


def next_sequence(session: Session, class_session_id: uuid.UUID) -> int:
    """The next per-class sequence: ``MAX(sequence) + 1`` (0 when empty).

    NOT safe on its own: the caller MUST hold the class-session row lock
    (``get_by_id_for_update``) so concurrent senders of the same class
    serialize here instead of picking the same number. Recomputed from
    committed rows inside that lock, so a rolled-back attempt consumes
    nothing and the committed sequences stay contiguous.
    """
    highest = session.scalar(
        select(func.max(ClassMessage.sequence)).where(
            ClassMessage.class_session_id == class_session_id
        )
    )
    return (highest or 0) + 1


def create_message(
    session: Session,
    *,
    class_session_id: uuid.UUID,
    sender_user_id: uuid.UUID,
    client_message_id: str,
    body: str,
    sequence: int,
) -> ClassMessage:
    """Insert one immutable message (flushed, not committed).

    The row carries the authoritative ``sequence`` chosen by the caller
    under the class lock; ``created_at`` comes from the database clock.
    Uniqueness of both the sequence and the idempotency key is enforced
    by the indexes — a racing duplicate loses with an ``IntegrityError``
    rather than silently writing a second row.
    """
    message = ClassMessage(
        class_session_id=class_session_id,
        sender_user_id=sender_user_id,
        client_message_id=client_message_id,
        body=body,
        sequence=sequence,
    )
    session.add(message)
    session.flush()
    return message
