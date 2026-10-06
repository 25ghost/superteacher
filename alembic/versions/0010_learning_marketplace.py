"""0010 learning marketplace (Phase 1)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06

Phase 1 introduces the teacher → offering → student pipeline:

1. ``teachers.verification_status`` — the vetting axis, separate from the
   account status: an account may log in (``users.status = 'active'``)
   while its profile still awaits an administrator's decision. New column
   + CHECK, defaulting existing rows to ``pending`` (least privilege: no
   account silently inherits publishing rights it never asked for).
2. ``learning_contexts`` — the teachable unit (year, pathway, level,
   optional program version, subject). Its natural key is enforced by TWO
   partial unique indexes because ``program_version_id`` is nullable and
   NULLs are distinct in a unique index.
3. ``teaching_offerings`` — a verified teacher's offer of one context;
   at most one ``active`` offer per (teacher, context), via a partial
   unique index that leaves paused/archived history intact.
4. ``learning_enrollments`` — a student's membership of an offering,
   denormalising the context so at most one ``active`` enrollment can
   exist per (student, context): the database-level form of "leave
   before you switch teacher".

Columns and constraints mirror the ORM models exactly (verified offline
by ``scripts/verify_database.py`` and the unit tests). Every new
identifier stays well inside PostgreSQL's 63-byte limit.

Downgrade drops the three tables (cascade of child rows only — the FKs
have no ``ondelete``, so leftover offerings/enrollments are removed with
their tables) and removes the verification column; accounts left
``approved`` need no normalisation because the column disappears.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. teacher verification axis ---------------------------------------
    op.add_column(
        "teachers",
        sa.Column(
            "verification_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_check_constraint(
        "teachers_verification_status_check",
        "teachers",
        "verification_status IN ('pending', 'approved', 'rejected', 'suspended')",
    )

    # --- 2. learning contexts ------------------------------------------------
    op.create_table(
        "learning_contexts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("pathway_id", sa.Uuid(), nullable=False),
        sa.Column("education_level_id", sa.Uuid(), nullable=False),
        sa.Column("program_version_id", sa.Uuid(), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="learning_contexts_pkey"),
        sa.ForeignKeyConstraint(["academic_year_id"], ["academic_years.id"], name="learning_contexts_academic_year_id_fkey"),
        sa.ForeignKeyConstraint(["pathway_id"], ["pathways.id"], name="learning_contexts_pathway_id_fkey"),
        sa.ForeignKeyConstraint(["education_level_id"], ["education_levels.id"], name="learning_contexts_education_level_id_fkey"),
        sa.ForeignKeyConstraint(["program_version_id"], ["program_versions.id"], name="learning_contexts_program_version_id_fkey"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], name="learning_contexts_subject_id_fkey"),
    )
    # Uniqueness of the natural key, split around the nullable program.
    op.create_index(
        "uq_learning_contexts_with_program_key",
        "learning_contexts",
        [
            "academic_year_id",
            "pathway_id",
            "education_level_id",
            "program_version_id",
            "subject_id",
        ],
        unique=True,
        postgresql_where=sa.text("program_version_id IS NOT NULL"),
        sqlite_where=sa.text("program_version_id IS NOT NULL"),
    )
    op.create_index(
        "uq_learning_contexts_without_program_key",
        "learning_contexts",
        ["academic_year_id", "pathway_id", "education_level_id", "subject_id"],
        unique=True,
        postgresql_where=sa.text("program_version_id IS NULL"),
        sqlite_where=sa.text("program_version_id IS NULL"),
    )
    op.create_index(
        "learning_contexts_pathway_id_idx", "learning_contexts", ["pathway_id"]
    )
    op.create_index(
        "learning_contexts_education_level_id_idx",
        "learning_contexts",
        ["education_level_id"],
    )
    op.create_index(
        "learning_contexts_program_version_id_idx",
        "learning_contexts",
        ["program_version_id"],
    )
    op.create_index(
        "learning_contexts_subject_id_idx", "learning_contexts", ["subject_id"]
    )

    # --- 3. teaching offerings ----------------------------------------------
    op.create_table(
        "teaching_offerings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("learning_context_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teaching_offerings_pkey"),
        sa.ForeignKeyConstraint(["teacher_id"], ["teachers.id"], name="teaching_offerings_teacher_id_fkey"),
        sa.ForeignKeyConstraint(["learning_context_id"], ["learning_contexts.id"], name="teaching_offerings_learning_context_id_fkey"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="teaching_offerings_status_check",
        ),
    )
    op.create_index(
        "uq_teaching_offerings_teacher_context_active_key",
        "teaching_offerings",
        ["teacher_id", "learning_context_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "teaching_offerings_teacher_id_idx", "teaching_offerings", ["teacher_id"]
    )
    op.create_index(
        "teaching_offerings_learning_context_id_idx",
        "teaching_offerings",
        ["learning_context_id"],
    )

    # --- 4. learning enrollments --------------------------------------------
    op.create_table(
        "learning_enrollments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("teaching_offering_id", sa.Uuid(), nullable=False),
        sa.Column("learning_context_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="learning_enrollments_pkey"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], name="learning_enrollments_student_id_fkey"),
        sa.ForeignKeyConstraint(["teaching_offering_id"], ["teaching_offerings.id"], name="learning_enrollments_teaching_offering_id_fkey"),
        sa.ForeignKeyConstraint(["learning_context_id"], ["learning_contexts.id"], name="learning_enrollments_learning_context_id_fkey"),
        sa.CheckConstraint(
            "status IN ('active', 'ended')",
            name="learning_enrollments_status_check",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="learning_enrollments_date_order_check",
        ),
    )
    op.create_index(
        "uq_learning_enrollments_student_context_active_key",
        "learning_enrollments",
        ["student_id", "learning_context_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "learning_enrollments_student_id_idx",
        "learning_enrollments",
        ["student_id"],
    )
    op.create_index(
        "learning_enrollments_teaching_offering_id_idx",
        "learning_enrollments",
        ["teaching_offering_id"],
    )
    op.create_index(
        "learning_enrollments_learning_context_id_idx",
        "learning_enrollments",
        ["learning_context_id"],
    )


def downgrade() -> None:
    op.drop_table("learning_enrollments")
    op.drop_table("teaching_offerings")
    op.drop_table("learning_contexts")

    op.drop_constraint(
        "teachers_verification_status_check", "teachers", type_="check"
    )
    op.drop_column("teachers", "verification_status")
