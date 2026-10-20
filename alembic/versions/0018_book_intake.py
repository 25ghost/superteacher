"""0018 administrator book intake (book library)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-20

Implements the durable data model of the administrator-managed book
library intake (ADR-001, P1):

1. ``books`` â€” stable identity and administrative metadata for one
   library title; never a teacher material and never hung off a teaching
   offering. The creating-user FK uses the default NO ACTION behaviour so
   historical books survive account changes.
2. ``book_revisions`` â€” one row per uploaded (or, later, corrected)
   edition: the source ``file_assets`` reference, a per-book sequential
   edition number (unique per book), the uploading administrator, and the
   intake-to-publication status vocabulary. P1 writes only ``uploaded``;
   ``extracting`` / ``extraction_failed`` / ``pending_review`` /
   ``published`` / ``rejected`` exist so the approved extraction, review
   and republication phases extend this table instead of reshaping it
   (ADR-004/ADR-005). Source-asset and uploader FKs also use NO ACTION:
   published or historical revisions are never cascade-deleted.
3. ``book_revision_contexts`` â€” the canonical ``learning_contexts`` rows
   a revision is valid for, one per row, unique per (revision, context).
   Book eligibility is exact canonical matching only (ADR-002); no
   free-text pathway/level/subject column exists anywhere in this schema.

No student-visible surface, extraction result, review decision,
question, attempt or progress table is created here. Downgrade drops the
three tables; teacher materials, enrollments, classrooms and historical
audit rows are untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. books --------------------------------------------------------------
    op.create_table(
        "books",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="books_created_by_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="books_pkey"),
    )
    op.create_index("books_created_by_user_id_idx", "books", ["created_by_user_id"])

    # --- 2. book_revisions -----------------------------------------------------
    op.create_table(
        "book_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("file_asset_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="uploaded"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="book_revisions_revision_number_check",
        ),
        sa.CheckConstraint(
            "status IN ('uploaded', 'extracting', 'extraction_failed', "
            "'pending_review', 'published', 'rejected')",
            name="book_revisions_status_check",
        ),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], name="book_revisions_book_id_fkey"),
        sa.ForeignKeyConstraint(["file_asset_id"], ["file_assets.id"], name="book_revisions_file_asset_id_fkey"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], name="book_revisions_uploaded_by_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="book_revisions_pkey"),
    )
    op.create_index(
        "uq_book_revisions_book_revision_number_key",
        "book_revisions",
        ["book_id", "revision_number"],
        unique=True,
    )
    op.create_index("book_revisions_book_id_idx", "book_revisions", ["book_id"])
    op.create_index("book_revisions_file_asset_id_idx", "book_revisions", ["file_asset_id"])
    op.create_index("book_revisions_uploaded_by_user_id_idx", "book_revisions", ["uploaded_by_user_id"])
    op.create_index("book_revisions_status_idx", "book_revisions", ["status"])

    # --- 3. book_revision_contexts --------------------------------------------
    op.create_table(
        "book_revision_contexts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_revision_id", sa.Uuid(), nullable=False),
        sa.Column("learning_context_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["book_revision_id"], ["book_revisions.id"], name="book_revision_contexts_book_revision_id_fkey"),
        sa.ForeignKeyConstraint(["learning_context_id"], ["learning_contexts.id"], name="book_revision_contexts_learning_context_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="book_revision_contexts_pkey"),
    )
    op.create_index(
        "uq_book_revision_contexts_revision_context_key",
        "book_revision_contexts",
        ["book_revision_id", "learning_context_id"],
        unique=True,
    )
    op.create_index(
        "book_revision_contexts_context_id_idx",
        "book_revision_contexts",
        ["learning_context_id"],
    )


def downgrade() -> None:
    op.drop_index("book_revision_contexts_context_id_idx", table_name="book_revision_contexts")
    op.drop_index("uq_book_revision_contexts_revision_context_key", table_name="book_revision_contexts")
    op.drop_table("book_revision_contexts")
    op.drop_index("book_revisions_status_idx", table_name="book_revisions")
    op.drop_index("book_revisions_uploaded_by_user_id_idx", table_name="book_revisions")
    op.drop_index("book_revisions_file_asset_id_idx", table_name="book_revisions")
    op.drop_index("book_revisions_book_id_idx", table_name="book_revisions")
    op.drop_index("uq_book_revisions_book_revision_number_key", table_name="book_revisions")
    op.drop_table("book_revisions")
    op.drop_index("books_created_by_user_id_idx", table_name="books")
    op.drop_table("books")
