"""Data access for ``lessons``.

Insert/lookup helpers only — whether a topic belongs to the caller's
offering is a **service** decision; this module only decides *how* rows
are written and found.

All inserts are ``flush``ed, never committed: the request-scoped session
remains the single transaction owner.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.lesson import Lesson


def get_by_id(session: Session, lesson_id: uuid.UUID) -> Lesson | None:
    """One lesson row (or None). Ownership is the service's decision."""
    stmt = select(Lesson).where(Lesson.id == lesson_id)
    return session.scalar(stmt)


def get_by_id_for_update(session: Session, lesson_id: uuid.UUID) -> Lesson | None:
    """One lesson with its row locked (``SELECT ... FOR UPDATE``)."""
    stmt = (
        select(Lesson)
        .where(Lesson.id == lesson_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalar(stmt)


def list_for_topic(session: Session, topic_id: uuid.UUID) -> list[Lesson]:
    """Every lesson of one topic, in the stored order (stable: order, id)."""
    stmt = (
        select(Lesson)
        .where(Lesson.topic_id == topic_id)
        .order_by(Lesson.display_order, Lesson.id)
    )
    return list(session.scalars(stmt))


def count_for_topic(session: Session, topic_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count()).where(Lesson.topic_id == topic_id)
    ) or 0


def delete_many_for_topic(session: Session, topic_id: uuid.UUID) -> None:
    """Remove every lesson of one topic (flushed, not committed).

    Done explicitly before the topic row goes away so SQLite — which does
    not enforce ``ON DELETE CASCADE`` unless the connection opts in —
    keeps the same behaviour as PostgreSQL.
    """
    lessons = session.scalars(
        select(Lesson).where(Lesson.topic_id == topic_id)
    ).all()
    for lesson in lessons:
        session.delete(lesson)
    session.flush()


def next_display_order(session: Session, topic_id: uuid.UUID) -> int:
    """The next free position in this topic (max + 1, or 1 when empty)."""
    current = session.scalar(
        select(func.max(Lesson.display_order)).where(Lesson.topic_id == topic_id)
    )
    return (current or 0) + 1


def create(
    session: Session,
    *,
    topic_id: uuid.UUID,
    title: str,
    description: str | None,
    display_order: int,
) -> Lesson:
    """Insert one lesson row (flushed, not committed)."""
    lesson = Lesson(
        topic_id=topic_id,
        title=title,
        description=description,
        display_order=display_order,
    )
    session.add(lesson)
    session.flush()  # assign the PK so the audit event can name it
    return lesson


def delete(session: Session, lesson: Lesson) -> None:
    """Remove one lesson row (flushed, not committed)."""
    session.delete(lesson)
    session.flush()
