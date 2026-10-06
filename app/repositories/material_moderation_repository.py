"""Data access for ``material_moderations``.

One row per administrator decision. Ownership is irrelevant here: the
service that inserts a moderation already proved the reviewer is an admin
and the material is in a moderatable state.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.material_moderation import MaterialModeration


def list_for_material(session: Session, material_id: uuid.UUID) -> list[MaterialModeration]:
    """Every decision on one material, newest first."""
    stmt = (
        select(MaterialModeration)
        .where(MaterialModeration.material_id == material_id)
        .order_by(MaterialModeration.created_at.desc(), MaterialModeration.id)
    )
    return list(session.scalars(stmt))


def create(
    session: Session,
    *,
    material_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    decision: str,
    reason: str | None,
) -> MaterialModeration:
    """Insert one moderation row (flushed, not committed)."""
    record = MaterialModeration(
        material_id=material_id,
        reviewer_user_id=reviewer_user_id,
        decision=decision,
        reason=reason,
    )
    session.add(record)
    session.flush()
    return record
