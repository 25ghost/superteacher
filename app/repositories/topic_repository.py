"""Data access for ``topics``.

Insert/lookup helpers only — whether an offering belongs to the caller and
which topic sits under which offering are **service** decisions (the same
Option-A split as the rest of the marketplace); this module only decides
*how* rows are written and found.

All inserts are ``flush``ed, never committed: the request-scoped session
remains the single transaction owner.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.topic import Topic


def get_by_id(session: Session, topic_id: uuid.UUID) -> Topic | None:
    """One topic row (or None). Ownership is the service's decision."""
    stmt = select(Topic).where(Topic.id == topic_id)
    return session.scalar(stmt)


def get_by_id_for_update(session: Session, topic_id: uuid.UUID) -> Topic | None:
    """One topic with its row locked (``SELECT ... FOR UPDATE``)."""
    stmt = (
        select(Topic)
        .where(Topic.id == topic_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalar(stmt)


def list_for_offering(session: Session, teaching_offering_id: uuid.UUID) -> list[Topic]:
    """Every topic of one offering, in the stored order (stable: order, id)."""
    stmt = (
        select(Topic)
        .where(Topic.teaching_offering_id == teaching_offering_id)
        .order_by(Topic.display_order, Topic.id)
    )
    return list(session.scalars(stmt))


def next_display_order(session: Session, teaching_offering_id: uuid.UUID) -> int:
    """The next free position in this offering (max + 1, or 1 when empty)."""
    current = session.scalar(
        select(func.max(Topic.display_order)).where(
            Topic.teaching_offering_id == teaching_offering_id
        )
    )
    return (current or 0) + 1


def create(
    session: Session,
    *,
    teaching_offering_id: uuid.UUID,
    title: str,
    description: str | None,
    display_order: int,
) -> Topic:
    """Insert one topic row (flushed, not committed)."""
    topic = Topic(
        teaching_offering_id=teaching_offering_id,
        title=title,
        description=description,
        display_order=display_order,
    )
    session.add(topic)
    session.flush()  # assign the PK so the audit event can name it
    return topic


def delete(session: Session, topic: Topic) -> None:
    """Remove one topic row (flushed, not committed).

    The caller must have removed (or relied on the cascade for) the topic's
    lessons first; SQLite does not enforce ``ON DELETE CASCADE`` unless the
    connection opts in, so the service deletes lessons explicitly.
    """
    session.delete(topic)
    session.flush()
