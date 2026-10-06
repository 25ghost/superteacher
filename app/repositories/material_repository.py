"""Data access for ``materials``.

Ownership and lifecycle transitions are **service** decisions; this module
only decides *how* rows are written and found.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.material import Material


def get_by_id(session: Session, material_id: uuid.UUID) -> Material | None:
    """One material row (or None). Ownership is the service's decision."""
    stmt = select(Material).where(Material.id == material_id)
    return session.scalar(stmt)


def get_by_id_for_update(session: Session, material_id: uuid.UUID) -> Material | None:
    """One material with its row locked (``SELECT ... FOR UPDATE``)."""
    stmt = (
        select(Material)
        .where(Material.id == material_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalar(stmt)


def list_for_offering(
    session: Session, teaching_offering_id: uuid.UUID
) -> list[Material]:
    """Every material of one offering, newest first (stable: created_at, id)."""
    stmt = (
        select(Material)
        .where(Material.teaching_offering_id == teaching_offering_id)
        .order_by(Material.created_at.desc(), Material.id)
    )
    return list(session.scalars(stmt))


def list_for_admin(
    session: Session,
    *,
    status: str | None = None,
    teaching_offering_id: uuid.UUID | None = None,
) -> list[Material]:
    """Moderation queue / archive listing (admin-only service entry point)."""
    stmt = select(Material)
    if status is not None:
        stmt = stmt.where(Material.status == status)
    if teaching_offering_id is not None:
        stmt = stmt.where(Material.teaching_offering_id == teaching_offering_id)
    stmt = stmt.order_by(Material.created_at.desc(), Material.id)
    return list(session.scalars(stmt))


def create(
    session: Session,
    *,
    teaching_offering_id: uuid.UUID,
    lesson_id: uuid.UUID | None,
    title: str,
    description: str | None,
    material_type: str,
    status: str,
    file_asset_id: uuid.UUID,
) -> Material:
    """Insert one material row (flushed, not committed)."""
    material = Material(
        teaching_offering_id=teaching_offering_id,
        lesson_id=lesson_id,
        title=title,
        description=description,
        material_type=material_type,
        status=status,
        file_asset_id=file_asset_id,
    )
    session.add(material)
    session.flush()  # assign the PK so the audit event can name it
    return material


def delete(session: Session, material: Material) -> None:
    """Remove one material row (flushed, not committed)."""
    session.delete(material)
    session.flush()
