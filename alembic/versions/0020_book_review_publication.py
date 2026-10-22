"""0020 book review, correction and publication (book P3)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-21

Implements the durable data model of administrative inspection,
correction, per-unit review, rejection and versioned publication (ADR-004
/ADR-005):

1. ``book_revisions`` gains two additive columns:

   - ``is_current_publication`` (NOT NULL, default false) — the book's
     current-publication pointer lives ON the revision, protected by a
     PARTIAL unique index ``uq_book_revisions_book_current_key (book_id)
     WHERE is_current_publication``: at most one current revision per
     book is a database-enforced rule, and previous published revisions
     keep ``status = 'published'`` with the flag cleared, so history is
     never rewritten to make room for the newer edition. A pointer
     column on ``books`` was rejected: it would create a books ->
     book_revisions -> books FK cycle the scratch-SQLite ``create_all``
     smoke test cannot express;
   - ``rejection_reason`` (nullable text) — the administrator's recorded
     reason for ``status = 'rejected'``. The named CHECK
     ``book_revisions_rejected_reason_check`` makes "rejected without a
     reason" impossible for every future writer, not just this API.

2. ``book_units`` gains the durable per-unit review state:

   - ``review_status`` (NOT NULL, default ``pending``; ``pending`` |
     ``approved`` via ``book_units_review_status_check``) — every
     extracted unit is individually reviewable; a correction resets it
     to ``pending``;
   - ``reviewed_by_user_id`` / ``reviewed_at`` (nullable) — WHO
     approved the unit and WHEN, with the shared ``%s_%s_fkey``
     naming convention FK to ``users`` and its own index. The paired
     CHECKs ``book_units_review_approved_check`` /
     ``book_units_review_pending_check`` keep identity and state in
     lockstep: approved REQUIRES reviewer + timestamp, pending REQUIRES
     both to be NULL (a reset can never leave stale identity behind).

No new table, no student-visible column, no answer-key/progress field
is created here; extraction warnings, source-page ranges and assessment
designations stay exactly as 0019 wrote them. Downgrade removes the five
columns, three CHECKs, one FK, one index and the partial unique index;
0018/0019 and every other table are untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. book_revisions: publication pointer + rejection reason ---------------
    op.add_column(
        "book_revisions",
        sa.Column(
            "is_current_publication",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "book_revisions",
        sa.Column("rejection_reason", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "book_revisions_rejected_reason_check",
        "book_revisions",
        "status <> 'rejected' OR rejection_reason IS NOT NULL",
    )
    # At most ONE current published revision per book (the pointer is a
    # partial unique index, not a nullable FK, so no books<->book_revisions
    # cycle exists for the SQLite metadata smoke test to sort).
    op.create_index(
        "uq_book_revisions_book_current_key",
        "book_revisions",
        ["book_id"],
        unique=True,
        postgresql_where=sa.text("is_current_publication"),
        sqlite_where=sa.text("is_current_publication"),
    )

    # --- 2. book_units: durable per-unit review state ----------------------------
    op.add_column(
        "book_units",
        sa.Column(
            "review_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "book_units",
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "book_units",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "book_units_review_status_check",
        "book_units",
        "review_status IN ('pending', 'approved')",
    )
    op.create_check_constraint(
        "book_units_review_approved_check",
        "book_units",
        "review_status <> 'approved' OR (reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "book_units_review_pending_check",
        "book_units",
        "review_status <> 'pending' OR (reviewed_by_user_id IS NULL AND reviewed_at IS NULL)",
    )
    op.create_foreign_key(
        "book_units_reviewed_by_user_id_fkey",
        "book_units",
        "users",
        ["reviewed_by_user_id"],
        ["id"],
    )
    op.create_index(
        "book_units_reviewed_by_user_id_idx",
        "book_units",
        ["reviewed_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("book_units_reviewed_by_user_id_idx", table_name="book_units")
    op.drop_constraint(
        "book_units_reviewed_by_user_id_fkey", "book_units", type_="foreignkey"
    )
    op.drop_constraint(
        "book_units_review_pending_check", "book_units", type_="check"
    )
    op.drop_constraint(
        "book_units_review_approved_check", "book_units", type_="check"
    )
    op.drop_constraint("book_units_review_status_check", "book_units", type_="check")
    op.drop_column("book_units", "reviewed_at")
    op.drop_column("book_units", "reviewed_by_user_id")
    op.drop_column("book_units", "review_status")

    op.drop_index("uq_book_revisions_book_current_key", table_name="book_revisions")
    op.drop_constraint(
        "book_revisions_rejected_reason_check", "book_revisions", type_="check"
    )
    op.drop_column("book_revisions", "rejection_reason")
    op.drop_column("book_revisions", "is_current_publication")
