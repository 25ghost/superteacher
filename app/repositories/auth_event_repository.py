"""Data access for ``auth_events`` (authentication event audit trail)."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth_event import AuthEvent
from app.models.user import User


def log_event(
    session: Session,
    *,
    user_id: uuid.UUID,
    event_type: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    metadata_json: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> AuthEvent:
    """Record an authentication event (flushed, not committed).

    ``user_id`` is the account the event is *about*; ``actor_user_id`` is
    the administrator who performed it when that is someone else (Phase B
    audit, G10). Both are never the same row's secret material — this
    function stores identifiers only, never tokens or passwords.
    """
    record = AuthEvent(
        user_id=user_id,
        event_type=event_type,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata_json=metadata_json,
        actor_user_id=actor_user_id,
    )
    session.add(record)
    session.flush()
    return record


def list_recent_for_user(
    session: Session, user_id: uuid.UUID, *, limit: int = 10
) -> list[tuple[AuthEvent, User | None]]:
    """The newest ``limit`` events *about* one user, actor resolved in-place.

    The outer join on ``actor_user_id`` puts the acting administrator's
    email in the same round-trip (no per-row actor lookup), and ``LIMIT``
    keeps the query bounded whatever the audit trail's size. The actor
    column is a self-reference, so a deleted actor comes back as ``None``.
    """
    stmt = (
        select(AuthEvent, User)
        .outerjoin(User, User.id == AuthEvent.actor_user_id)
        .where(AuthEvent.user_id == user_id)
        .order_by(AuthEvent.created_at.desc(), AuthEvent.id.desc())
        .limit(limit)
    )
    return list(session.execute(stmt))
