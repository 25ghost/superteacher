"""Data access for ``material_progress``.

Progress rows are personal: lookups always carry ``student_id``. Ownership
and lifecycle transitions (idempotency, forward-only moves) are **service**
decisions; this module only decides *how* rows are written and found.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.material import Material
from app.models.material_progress import MaterialProgress


def get_for_student_material(
    session: Session, student_id: uuid.UUID, material_id: uuid.UUID
) -> MaterialProgress | None:
    """The student's progress row for one material (or None)."""
    stmt = select(MaterialProgress).where(
        MaterialProgress.student_id == student_id,
        MaterialProgress.material_id == material_id,
    )
    return session.scalar(stmt)


def get_for_student_material_for_update(
    session: Session, student_id: uuid.UUID, material_id: uuid.UUID
) -> MaterialProgress | None:
    """Same lookup with the row locked (``SELECT ... FOR UPDATE``)."""
    stmt = (
        select(MaterialProgress)
        .where(
            MaterialProgress.student_id == student_id,
            MaterialProgress.material_id == material_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalar(stmt)


def list_published_for_offering(
    session: Session, student_id: uuid.UUID, teaching_offering_id: uuid.UUID
) -> list[MaterialProgress]:
    """The student's progress rows for published materials of one offering."""
    stmt = (
        select(MaterialProgress)
        .join(Material, Material.id == MaterialProgress.material_id)
        .where(
            MaterialProgress.student_id == student_id,
            Material.teaching_offering_id == teaching_offering_id,
            Material.status == "published",
        )
        .order_by(MaterialProgress.created_at, MaterialProgress.id)
    )
    return list(session.scalars(stmt))


def create(
    session: Session,
    *,
    student_id: uuid.UUID,
    material_id: uuid.UUID,
    status: str,
    started_at=None,
    completed_at=None,
) -> MaterialProgress:
    """Insert one progress row (flushed, not committed)."""
    row = MaterialProgress(
        student_id=student_id,
        material_id=material_id,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )
    session.add(row)
    session.flush()  # assign the PK so the audit event can name it
    return row
