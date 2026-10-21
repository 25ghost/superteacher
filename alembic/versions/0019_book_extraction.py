"""0019 book content extraction (book P2)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-20

Implements the durable data model of asynchronous content extraction
(ADR-004):

1. ``book_revisions`` gains two nullable bookkeeping columns:
   ``extraction_summary`` (JSON describing what a successful pass
   extracted — page/unit counts, per-kind tallies, revision-level
   warnings) and ``extraction_error`` (the stable failure code/message
   when the document could not be parsed). Both are source-derived
   metadata only: never bytes, filesystem paths or storage keys.
2. ``book_units`` — one row per extracted structural unit (chapter,
   topic, explanation, example, exercise set) of one revision, with its
   source-derived title, extracted text, sibling ``display_order``, the
   1-based source page range it came from, newline-separated fixed
   warning codes and an ``assessment_requirement`` that P2 always
   writes ``unresolved``.

The two structural invariants:

- the COMPOSITE self-FK ``(parent_unit_id, book_revision_id) ->
  (id, book_revision_id)`` (backed by ``UNIQUE (id, book_revision_id)``)
  makes "a child belongs to its parent's revision" a database-enforced
  rule across every future writer, not a service-layer convention;
- two PARTIAL unique indexes — children
  ``(revision, parent, order) WHERE parent_unit_id IS NOT NULL`` and
  roots ``(revision, order) WHERE parent_unit_id IS NULL`` — make
  sibling ordering collision-free; a plain UNIQUE would ignore NULL
  parents and leave root order unprotected. Both carry
  ``postgresql_where`` + ``sqlite_where`` so the scratch-SQLite metadata
  smoke test and the PostgreSQL live check see the same DDL (0016
  house pattern).

No student-visible surface, review decision, question, attempt or
progress table is created here. Downgrade drops ``book_units`` and the
two bookkeeping columns; books, revisions, context links, teacher
materials, enrollments, classrooms and historical audit rows are
untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. book_revisions extraction bookkeeping -------------------------------
    op.add_column(
        "book_revisions",
        sa.Column("extraction_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "book_revisions",
        sa.Column("extraction_error", sa.Text(), nullable=True),
    )

    # --- 2. book_units ----------------------------------------------------------
    op.create_table(
        "book_units",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_revision_id", sa.Uuid(), nullable=False),
        sa.Column("parent_unit_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("source_page_start", sa.Integer(), nullable=False),
        sa.Column("source_page_end", sa.Integer(), nullable=False),
        sa.Column("extraction_warnings", sa.Text(), nullable=True),
        sa.Column(
            "assessment_requirement",
            sa.String(length=32),
            nullable=False,
            server_default="unresolved",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('chapter', 'topic', 'explanation', 'example', 'exercise_set')",
            name="book_units_kind_check",
        ),
        sa.CheckConstraint("title <> ''", name="book_units_title_check"),
        sa.CheckConstraint(
            "display_order >= 1",
            name="book_units_display_order_check",
        ),
        sa.CheckConstraint(
            "source_page_start >= 1",
            name="book_units_page_start_check",
        ),
        sa.CheckConstraint(
            "source_page_end >= source_page_start",
            name="book_units_page_range_check",
        ),
        sa.CheckConstraint(
            "assessment_requirement IN ('unresolved', 'required', 'not_required')",
            name="book_units_assessment_requirement_check",
        ),
        sa.ForeignKeyConstraint(
            ["book_revision_id"], ["book_revisions.id"],
            name="book_units_book_revision_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["parent_unit_id", "book_revision_id"],
            ["book_units.id", "book_units.book_revision_id"],
            name="book_units_parent_unit_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="book_units_pkey"),
        sa.UniqueConstraint("id", "book_revision_id", name="book_units_id_revision_key"),
    )
    op.create_index(
        "book_units_book_revision_id_idx",
        "book_units",
        ["book_revision_id"],
    )
    op.create_index(
        "book_units_parent_unit_id_idx",
        "book_units",
        ["parent_unit_id"],
    )
    op.create_index(
        "uq_book_units_revision_parent_order_key",
        "book_units",
        ["book_revision_id", "parent_unit_id", "display_order"],
        unique=True,
        postgresql_where=sa.text("parent_unit_id IS NOT NULL"),
        sqlite_where=sa.text("parent_unit_id IS NOT NULL"),
    )
    op.create_index(
        "uq_book_units_revision_root_order_key",
        "book_units",
        ["book_revision_id", "display_order"],
        unique=True,
        postgresql_where=sa.text("parent_unit_id IS NULL"),
        sqlite_where=sa.text("parent_unit_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_book_units_revision_root_order_key", table_name="book_units")
    op.drop_index("uq_book_units_revision_parent_order_key", table_name="book_units")
    op.drop_index("book_units_parent_unit_id_idx", table_name="book_units")
    op.drop_index("book_units_book_revision_id_idx", table_name="book_units")
    op.drop_table("book_units")
    op.drop_column("book_revisions", "extraction_error")
    op.drop_column("book_revisions", "extraction_summary")
