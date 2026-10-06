"""0012 materials, file assets and moderation records (Phase 2, slice 2B)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-06

Phase 2 slice 2B introduces the teaching-material system beneath an
existing teaching offering:

1. ``file_assets`` — one row per uploaded file: opaque storage reference
   (never a public path), original filename, content type, size, SHA-256
   checksum and the validation pipeline state
   (upload → validating → invalid|valid → stored).
2. ``materials`` — one teacher-authored artifact under an offering
   (and optionally one of its lessons). Moderation lifecycle:
   draft → pending_review → (published | rejected) → archived. Only an
   administrator approves/rejects; a teacher cannot publish directly.
3. ``material_moderations`` — one row per administrator decision
   (reviewer, decision, reason, timestamp) so the moderation trail is
   durable even after the live status moves on.

Teacher / academic year / pathway / level / subject are NOT duplicated on
``materials``: they are derivable through ``teaching_offerings`` →
``learning_contexts``. Published materials are effectively immutable in
the MVP; a change is a new revision (slice 2C).

Columns and constraints mirror the ORM models exactly (verified offline
by ``scripts/verify_database.py`` and the unit tests).

Downgrade drops the three tables; moderation history and file-asset
metadata are not migrated further — slice 2B owns them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. file_assets — one uploaded file + its validation state ----------
    op.create_table(
        "file_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("validation_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="file_assets_pkey"),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name="file_assets_uploaded_by_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "validation_status IN ('upload', 'validating', 'invalid', 'valid', 'stored')",
            name="file_assets_validation_status_check",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="file_assets_size_bytes_check",
        ),
    )
    op.create_index(
        "file_assets_uploaded_by_user_id_idx",
        "file_assets",
        ["uploaded_by_user_id"],
    )

    # --- 2. materials — one teacher-authored artifact under an offering ----
    op.create_table(
        "materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teaching_offering_id", sa.Uuid(), nullable=False),
        sa.Column("lesson_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("material_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("file_asset_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="materials_pkey"),
        sa.ForeignKeyConstraint(
            ["teaching_offering_id"],
            ["teaching_offerings.id"],
            name="materials_teaching_offering_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["lesson_id"],
            ["lessons.id"],
            name="materials_lesson_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["file_asset_id"],
            ["file_assets.id"],
            name="materials_file_asset_id_fkey",
        ),
        sa.CheckConstraint(
            "material_type IN ('book', 'note', 'exercise')",
            name="materials_material_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_review', 'rejected', 'published', 'archived')",
            name="materials_status_check",
        ),
    )
    op.create_index(
        "materials_teaching_offering_id_idx",
        "materials",
        ["teaching_offering_id"],
    )
    op.create_index("materials_lesson_id_idx", "materials", ["lesson_id"])
    op.create_index("materials_file_asset_id_idx", "materials", ["file_asset_id"])
    op.create_index("materials_status_idx", "materials", ["status"])

    # --- 3. material_moderations — one administrator decision --------------
    op.create_table(
        "material_moderations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="material_moderations_pkey"),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name="material_moderations_material_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name="material_moderations_reviewer_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="material_moderations_decision_check",
        ),
    )
    op.create_index(
        "material_moderations_material_id_idx",
        "material_moderations",
        ["material_id"],
    )
    op.create_index(
        "material_moderations_reviewer_user_id_idx",
        "material_moderations",
        ["reviewer_user_id"],
    )


def downgrade() -> None:
    op.drop_table("material_moderations")
    op.drop_table("materials")
    op.drop_table("file_assets")
