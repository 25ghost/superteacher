"""Data access for ``class_messages`` reads (Phase 3, slice 3B).

Write-side message persistence (insert + sequence allocation under the
class row lock) arrives with the classroom transport in slice 3D; slice 3B
only needs the transcript read:

- :func:`list_messages_after` pages one class's messages with a cursor
  (``sequence > after_sequence``), ASC — reconnect recovery and the
  historical transcript are the same query from different starting
  points, and per-class sequence uniqueness makes the cursor unambiguous;
- no mutation helpers exist at all: messages are immutable once written.

Lock order (inherited from ``online_class_repository``):

    teaching_offerings  ->  online_class_sessions  ->  (messages, segments)

All functions here are read-only (no lock, flush or commit).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
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
