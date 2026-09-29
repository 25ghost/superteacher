"""Data access for ``auth_events`` (authentication event audit trail)."""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.auth_event import AuthEvent


def log_event(
    session: Session,
    *,
    user_id: uuid.UUID,
    event_type: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    metadata_json: str | None = None,
) -> AuthEvent:
    """Record an authentication event (flushed, not committed)."""
    record = AuthEvent(
        user_id=user_id,
        event_type=event_type,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata_json=metadata_json,
    )
    session.add(record)
    session.flush()
    return record
