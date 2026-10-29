"""0022 student guided-learning progress (book P5)

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-28

Implements the durable data model of student guided learning (ADR-007):
one student's sequential position in one administrator-published book
revision.

One new table, ``student_book_progress``:

- ``student_id`` — the student PROFILE owning the progress
  (``material_progress`` convention, NO ACTION FK);
- ``book_revision_id`` — the published edition the progress was earned
  on (NO ACTION FK). Progress never transfers between editions: a newly
  published revision has no rows yet and every student's guided walk
  starts fresh from its first unit;
- ``book_unit_id`` — the completed unit, bound to ONE unit of ONE
  specific edition via the COMPOSITE FK ``(book_unit_id,
  book_revision_id) -> book_units(id, book_revision_id)`` (target: the
  shared ``book_units_id_revision_key`` constraint) — progress can never
  be grafted onto a unit of a different revision;
- ``completed_at`` — when the unit was completed (content step finished
  or required checkpoint passed). A row IS a completion: the named CHECK
  proves the timestamp is always present, and historical completions are
  never rewritten;
- ``created_at`` / ``updated_at`` — the house timestamps.

The unique key ``uq_student_book_progress_key`` on
``(student_id, book_revision_id, book_unit_id)`` makes "one completion
record per unit per student per edition" a database rule, so concurrent
completion requests can never double-write (the service mirrors the
constraint with a readable 409 on a lost race).

Historical revisions and all other tables are preserved unchanged:
nothing here backfills or rewrites existing rows. Downgrade removes the
table, its constraints and its indexes; 0021 and every other table are
untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "student_book_progress",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("book_revision_id", sa.Uuid(), nullable=False),
        sa.Column("book_unit_id", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "completed_at IS NOT NULL",
            name="student_book_progress_completed_at_check",
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["students.id"],
            name="student_book_progress_student_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["book_revision_id"],
            ["book_revisions.id"],
            name="student_book_progress_book_revision_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["book_unit_id", "book_revision_id"],
            ["book_units.id", "book_units.book_revision_id"],
            name="student_book_progress_book_unit_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="student_book_progress_pkey"),
    )
    op.create_index(
        "uq_student_book_progress_key",
        "student_book_progress",
        ["student_id", "book_revision_id", "book_unit_id"],
        unique=True,
    )
    op.create_index(
        "student_book_progress_student_id_idx",
        "student_book_progress",
        ["student_id"],
    )
    op.create_index(
        "student_book_progress_book_revision_id_idx",
        "student_book_progress",
        ["book_revision_id"],
    )
    op.create_index(
        "student_book_progress_book_unit_id_idx",
        "student_book_progress",
        ["book_unit_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "student_book_progress_book_unit_id_idx",
        table_name="student_book_progress",
    )
    op.drop_index(
        "student_book_progress_book_revision_id_idx",
        table_name="student_book_progress",
    )
    op.drop_index(
        "student_book_progress_student_id_idx",
        table_name="student_book_progress",
    )
    op.drop_index(
        "uq_student_book_progress_key",
        table_name="student_book_progress",
    )
    op.drop_table("student_book_progress")
