"""0021 objective exercise assessment (book P4)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-22

Implements the durable data model of administrator-authored objective
assessment (ADR-006): multiple-choice / true-false questions on
exercise-set units, explicit answer-key verification, per-set passing
thresholds, and immutable attempt history.

1. ``book_units`` gains one additive column:

   - ``passing_threshold_percent`` (nullable integer) — the exercise
     set's passing threshold; NULL means the default 70. The named CHECK
     ``book_units_passing_threshold_check`` proves the column can only
     ever hold a value on an ``exercise_set`` unit and only within
     1-100, for every future writer — not just this API.

2. Four new tables (all source FKs NO ACTION — assessment history is
   never cascade-deleted, like the rest of the book family):

   - ``book_questions`` — one objective question bound to one
     exercise-set unit of one revision via the COMPOSITE FK
     ``(book_unit_id, book_revision_id) -> book_units(id,
     book_revision_id)`` (target: the shared ``book_units_id_revision_
     key`` constraint), so a question can never be grafted from one
     edition onto another. ``prompt``/``explanation`` are never blank;
     answer-key verification is a two-column lockstep (verifier id +
     timestamp, paired CHECKs exactly like ``book_units`` review
     state); sibling order is unique per unit.
   - ``book_question_options`` — one answer option per row; at most ONE
     correct option per question is a database rule via the PARTIAL
     unique index ``uq_book_question_options_question_correct_key``
     (at least one is the service's verification action — a CHECK
     cannot count sibling rows). The ``(id, book_question_id)`` unique
     constraint is the target of the attempt-answer composite FK.
   - ``book_attempts`` — one submitted attempt owning totals and the
     frozen pass outcome; composite FK binds it to one unit of one
     specific edition. Attempts are immutable: no service path ever
     updates or deletes them.
   - ``book_attempt_answers`` — one submitted answer per question per
     attempt (unique pair), with the composite FK
     ``(selected_option_id, book_question_id) -> book_question_options
     (id, book_question_id)`` proving in the database that the selected
     option is an option OF the question being answered.

Historical published revisions without assessment structures are
preserved unchanged: nothing here backfills or rewrites existing rows.
Downgrade removes the four tables, the one column, their constraints and
indexes; 0020 and every other table are untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. book_units: per-exercise-set passing threshold ------------------------
    op.add_column(
        "book_units",
        sa.Column("passing_threshold_percent", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "book_units_passing_threshold_check",
        "book_units",
        "passing_threshold_percent IS NULL OR "
        "(kind = 'exercise_set' AND passing_threshold_percent BETWEEN 1 AND 100)",
    )

    # --- 2. book_questions --------------------------------------------------------
    op.create_table(
        "book_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_revision_id", sa.Uuid(), nullable=False),
        sa.Column("book_unit_id", sa.Uuid(), nullable=False),
        sa.Column("question_type", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("answer_key_verified_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("answer_key_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "question_type IN ('multiple_choice', 'true_false')",
            name="book_questions_question_type_check",
        ),
        sa.CheckConstraint("prompt <> ''", name="book_questions_prompt_check"),
        sa.CheckConstraint("explanation <> ''", name="book_questions_explanation_check"),
        sa.CheckConstraint("display_order >= 1", name="book_questions_display_order_check"),
        sa.CheckConstraint(
            "answer_key_verified_by_user_id IS NULL "
            "OR answer_key_verified_at IS NOT NULL",
            name="book_questions_verified_check",
        ),
        sa.CheckConstraint(
            "answer_key_verified_at IS NULL "
            "OR answer_key_verified_by_user_id IS NOT NULL",
            name="book_questions_unverified_check",
        ),
        sa.ForeignKeyConstraint(
            ["book_revision_id"],
            ["book_revisions.id"],
            name="book_questions_book_revision_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["answer_key_verified_by_user_id"],
            ["users.id"],
            name="book_questions_answer_key_verified_by_user_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["book_unit_id", "book_revision_id"],
            ["book_units.id", "book_units.book_revision_id"],
            name="book_questions_book_unit_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="book_questions_pkey"),
        sa.UniqueConstraint(
            "id", "book_revision_id", name="book_questions_id_revision_key"
        ),
    )
    op.create_index(
        "uq_book_questions_unit_order_key",
        "book_questions",
        ["book_unit_id", "display_order"],
        unique=True,
    )
    op.create_index(
        "book_questions_book_revision_id_idx",
        "book_questions",
        ["book_revision_id"],
    )
    op.create_index(
        "book_questions_book_unit_id_idx",
        "book_questions",
        ["book_unit_id"],
    )
    op.create_index(
        "book_questions_answer_key_verified_by_user_id_idx",
        "book_questions",
        ["answer_key_verified_by_user_id"],
    )

    # --- 3. book_question_options -------------------------------------------------
    op.create_table(
        "book_question_options",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_question_id", sa.Uuid(), nullable=False),
        sa.Column("option_text", sa.Text(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("option_text <> ''", name="book_question_options_text_check"),
        sa.CheckConstraint(
            "display_order >= 1", name="book_question_options_display_order_check"
        ),
        sa.ForeignKeyConstraint(
            ["book_question_id"],
            ["book_questions.id"],
            name="book_question_options_book_question_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="book_question_options_pkey"),
        sa.UniqueConstraint(
            "id", "book_question_id", name="book_question_options_id_question_key"
        ),
    )
    op.create_index(
        "uq_book_question_options_question_order_key",
        "book_question_options",
        ["book_question_id", "display_order"],
        unique=True,
    )
    op.create_index(
        "uq_book_question_options_question_correct_key",
        "book_question_options",
        ["book_question_id"],
        unique=True,
        postgresql_where=sa.text("is_correct"),
        sqlite_where=sa.text("is_correct"),
    )
    op.create_index(
        "book_question_options_book_question_id_idx",
        "book_question_options",
        ["book_question_id"],
    )

    # --- 4. book_attempts ---------------------------------------------------------
    op.create_table(
        "book_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("book_revision_id", sa.Uuid(), nullable=False),
        sa.Column("book_unit_id", sa.Uuid(), nullable=False),
        sa.Column("total_questions", sa.Integer(), nullable=False),
        sa.Column("correct_answers", sa.Integer(), nullable=False),
        sa.Column("score_percent", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "total_questions >= 1", name="book_attempts_total_questions_check"
        ),
        sa.CheckConstraint(
            "correct_answers >= 0 AND correct_answers <= total_questions",
            name="book_attempts_correct_answers_check",
        ),
        sa.CheckConstraint(
            "score_percent >= 0 AND score_percent <= 100",
            name="book_attempts_score_percent_check",
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["students.id"],
            name="book_attempts_student_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["book_revision_id"],
            ["book_revisions.id"],
            name="book_attempts_book_revision_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["book_unit_id", "book_revision_id"],
            ["book_units.id", "book_units.book_revision_id"],
            name="book_attempts_book_unit_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="book_attempts_pkey"),
    )
    op.create_index(
        "book_attempts_student_id_idx", "book_attempts", ["student_id"]
    )
    op.create_index(
        "book_attempts_book_revision_id_idx",
        "book_attempts",
        ["book_revision_id"],
    )
    op.create_index(
        "book_attempts_book_unit_id_idx", "book_attempts", ["book_unit_id"]
    )
    op.create_index(
        "book_attempts_student_unit_idx",
        "book_attempts",
        ["student_id", "book_unit_id"],
    )

    # --- 5. book_attempt_answers --------------------------------------------------
    op.create_table(
        "book_attempt_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("book_question_id", sa.Uuid(), nullable=False),
        sa.Column("selected_option_id", sa.Uuid(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["book_attempt_id"],
            ["book_attempts.id"],
            name="book_attempt_answers_book_attempt_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["selected_option_id", "book_question_id"],
            ["book_question_options.id", "book_question_options.book_question_id"],
            name="book_attempt_answers_selected_option_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="book_attempt_answers_pkey"),
        sa.UniqueConstraint(
            "book_attempt_id",
            "book_question_id",
            name="uq_book_attempt_answers_attempt_question_key",
        ),
    )
    op.create_index(
        "book_attempt_answers_book_attempt_id_idx",
        "book_attempt_answers",
        ["book_attempt_id"],
    )
    op.create_index(
        "book_attempt_answers_book_question_id_idx",
        "book_attempt_answers",
        ["book_question_id"],
    )
    op.create_index(
        "book_attempt_answers_selected_option_id_idx",
        "book_attempt_answers",
        ["selected_option_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "book_attempt_answers_selected_option_id_idx",
        table_name="book_attempt_answers",
    )
    op.drop_index(
        "book_attempt_answers_book_question_id_idx",
        table_name="book_attempt_answers",
    )
    op.drop_index(
        "book_attempt_answers_book_attempt_id_idx",
        table_name="book_attempt_answers",
    )
    op.drop_table("book_attempt_answers")

    op.drop_index("book_attempts_student_unit_idx", table_name="book_attempts")
    op.drop_index("book_attempts_book_unit_id_idx", table_name="book_attempts")
    op.drop_index(
        "book_attempts_book_revision_id_idx", table_name="book_attempts"
    )
    op.drop_index("book_attempts_student_id_idx", table_name="book_attempts")
    op.drop_table("book_attempts")

    op.drop_index(
        "book_question_options_book_question_id_idx",
        table_name="book_question_options",
    )
    op.drop_index(
        "uq_book_question_options_question_correct_key",
        table_name="book_question_options",
    )
    op.drop_index(
        "uq_book_question_options_question_order_key",
        table_name="book_question_options",
    )
    op.drop_table("book_question_options")

    op.drop_index(
        "book_questions_answer_key_verified_by_user_id_idx",
        table_name="book_questions",
    )
    op.drop_index(
        "book_questions_book_unit_id_idx", table_name="book_questions"
    )
    op.drop_index(
        "book_questions_book_revision_id_idx", table_name="book_questions"
    )
    op.drop_index(
        "uq_book_questions_unit_order_key", table_name="book_questions"
    )
    op.drop_table("book_questions")

    op.drop_constraint(
        "book_units_passing_threshold_check", "book_units", type_="check"
    )
    op.drop_column("book_units", "passing_threshold_percent")
