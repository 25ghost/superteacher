"""0011 topics and lessons (Phase 2, slice 2A)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-06

Phase 2 slice 2A introduces the curriculum structure beneath an existing
teaching offering:

1. ``topics`` — the teacher's own outline sections inside ONE offering.
   They do not duplicate the Admin-owned ``learning_contexts`` catalog:
   a topic hangs off ``teaching_offerings`` and therefore stays inside
   that offering's educational context. ``display_order`` is unique within
   the offering and must be >= 1, so the sequence a student will read is
   stored, not recomputed.
2. ``lessons`` — one teachable step beneath a topic. Same ordering rule,
   unique within the topic. Deleting a topic removes its lessons
   (``ON DELETE CASCADE`` on the FK), so a section cannot lose children.

Published/materials behaviour (slice 2B/2C) is intentionally absent: no
status columns, no moderation fields — only the structure required to
hang content on later.

Columns and constraints mirror the ORM models exactly (verified offline
by ``scripts/verify_database.py`` and the unit tests). Every new
identifier stays well inside PostgreSQL's 63-byte limit.

Downgrade drops the two tables; the curriculum under an offering is
teacher-authored outline data, so no data-preserving step applies.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. topics under a teaching offering ----------------------------------
    op.create_table(
        "topics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teaching_offering_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="topics_pkey"),
        sa.ForeignKeyConstraint(
            ["teaching_offering_id"],
            ["teaching_offerings.id"],
            name="topics_teaching_offering_id_fkey",
        ),
        sa.CheckConstraint(
            "display_order >= 1",
            name="topics_display_order_check",
        ),
    )
    op.create_index(
        "uq_topics_teaching_offering_display_order_key",
        "topics",
        ["teaching_offering_id", "display_order"],
        unique=True,
    )
    op.create_index(
        "topics_teaching_offering_id_idx", "topics", ["teaching_offering_id"]
    )

    # --- 2. lessons under a topic ---------------------------------------------
    op.create_table(
        "lessons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="lessons_pkey"),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name="lessons_topic_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "display_order >= 1",
            name="lessons_display_order_check",
        ),
    )
    op.create_index(
        "uq_lessons_topic_display_order_key",
        "lessons",
        ["topic_id", "display_order"],
        unique=True,
    )
    op.create_index("lessons_topic_id_idx", "lessons", ["topic_id"])


def downgrade() -> None:
    op.drop_table("lessons")
    op.drop_table("topics")
