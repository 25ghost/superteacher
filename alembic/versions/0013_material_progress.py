"""0013 material progress (Phase 2, slice 2C)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-06

Phase 2 slice 2C introduces basic per-student material progress:

``material_progress`` — one row per ``(student, material)``, unique so a
student cannot hold two progress records for the same material. Status is
forward-only: not_started → in_progress → completed. ``started_at`` /
``completed_at`` are stamped on the first transition into each state.

Progress never describes the material globally, and it never transfers
when a student switches teachers: it stays keyed to the material row it
was earned on. Advanced analytics, time tracking and gamification are
out of scope (Phase 3+).

Columns and constraints mirror the ORM model exactly (verified offline
by ``scripts/verify_database.py`` and the unit tests).

Downgrade drops the table; progress history is slice 2C-owned.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "material_progress",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="material_progress_pkey"),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["students.id"],
            name="material_progress_student_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name="material_progress_material_id_fkey",
        ),
        sa.CheckConstraint(
            "status IN ('not_started', 'in_progress', 'completed')",
            name="material_progress_status_check",
        ),
    )
    op.create_index(
        "uq_material_progress_student_material_key",
        "material_progress",
        ["student_id", "material_id"],
        unique=True,
    )
    op.create_index("material_progress_student_id_idx", "material_progress", ["student_id"])
    op.create_index("material_progress_material_id_idx", "material_progress", ["material_id"])
    op.create_index("material_progress_status_idx", "material_progress", ["status"])


def downgrade() -> None:
    op.drop_table("material_progress")
