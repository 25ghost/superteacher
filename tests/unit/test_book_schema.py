"""Unit tests: the administrator book tables, migrations 0018-0021.

Book intake adds ``books``, ``book_revisions`` and
``book_revision_contexts`` (0018); content extraction adds the
``book_units`` tree plus two ``book_revisions`` bookkeeping columns
(``extraction_summary``/``extraction_error``, 0019); review and
publication add the publication pointer + rejection reason and the
per-unit review state (``is_current_publication``/``rejection_reason``,
``review_status``/``reviewed_by_user_id``/``reviewed_at``, 0020);
objective assessment adds the per-unit passing threshold plus
``book_questions``/``book_question_options``/``book_attempts``/
``book_attempt_answers`` (0021). Model, migration and the offline
verification script must agree — the drift tests below run
``scripts/verify_database.py``'s own schema descriptors, and the
truncation-order test proves the children are cleared before their
parents.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, Index

from app.core.database import Base
import app.models  # noqa: F401
from app.models.enums import (
    AssessmentRequirement,
    BookQuestionType,
    BookRevisionStatus,
    BookUnitKind,
    BookUnitReviewStatus,
    FileAssetStatus,
    sql_in_list,
)
from app.schemas.book import (
    BookRead,
    BookRevisionDetailRead,
    BookRevisionRead,
    BookRevisionRejectRequest,
    BookUnitCorrectRequest,
    BookUnitRead,
)
from app.schemas.book_assessment import (
    AssessmentAttemptRead,
    AssessmentQuestionFeedback,
    BookQuestionCreate,
    BookQuestionCorrectRequest,
    BookQuestionRead,
    BookQuestionVerifyRequest,
    BookUnitThresholdRequest,
)
from app.schemas.material import FileAssetRead

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_spec = importlib.util.spec_from_file_location(
    "verify_database", BACKEND_DIR / "scripts" / "verify_database.py"
)
verify_database = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_database", verify_database)
_spec.loader.exec_module(verify_database)

_NEW_TABLES = (
    "books",
    "book_revisions",
    "book_revision_contexts",
    "book_units",
    # P4 (0021)
    "book_questions",
    "book_question_options",
    "book_attempts",
    "book_attempt_answers",
)


def _table(name: str):
    return Base.metadata.tables[name]


def test_book_tables_are_registered_with_expected_columns() -> None:
    assert set(_NEW_TABLES) <= set(Base.metadata.tables)

    assert set(_table("books").columns.keys()) == {
        "id",
        "title",
        "description",
        "created_by_user_id",
        "created_at",
        "updated_at",
    }
    books = _table("books")
    assert books.columns["title"].nullable is False
    assert books.columns["title"].type.length == 200
    assert books.columns["description"].nullable is True
    assert books.columns["created_by_user_id"].nullable is False

    assert set(_table("book_revisions").columns.keys()) == {
        "id",
        "book_id",
        "revision_number",
        "file_asset_id",
        "uploaded_by_user_id",
        "status",
        "extraction_summary",
        "extraction_error",
        # P3: the publication pointer and the recorded rejection reason.
        "is_current_publication",
        "rejection_reason",
        "created_at",
        "updated_at",
    }
    revisions = _table("book_revisions")
    assert revisions.columns["book_id"].nullable is False
    assert revisions.columns["revision_number"].nullable is False
    assert revisions.columns["file_asset_id"].nullable is False
    assert revisions.columns["uploaded_by_user_id"].nullable is False
    assert revisions.columns["status"].nullable is False
    assert revisions.columns["status"].type.length == 32
    # P2 bookkeeping: source-derived metadata only, NULL until extraction.
    assert revisions.columns["extraction_summary"].nullable is True
    assert revisions.columns["extraction_error"].nullable is True
    # P3: the pointer is NOT NULL (default false — no NULL-comparison
    # semantics for "is this the current edition?"), the reason is NULL
    # for every status except rejected.
    assert revisions.columns["is_current_publication"].nullable is False
    assert revisions.columns["rejection_reason"].nullable is True

    # P3 per-unit review state + P4 passing threshold: every extracted
    # unit carries a durable verdict plus WHO/WHEN (both nullable —
    # NULL while pending) and an optional exercise-set threshold (NULL
    # means the default 70).
    assert set(_table("book_units").columns.keys()) == {
        "id",
        "book_revision_id",
        "parent_unit_id",
        "kind",
        "title",
        "content",
        "display_order",
        "source_page_start",
        "source_page_end",
        "extraction_warnings",
        "assessment_requirement",
        "review_status",
        "reviewed_by_user_id",
        "reviewed_at",
        # P4: the per-exercise-set passing threshold (NULL = default 70).
        "passing_threshold_percent",
        "created_at",
        "updated_at",
    }
    units = _table("book_units")
    assert units.columns["review_status"].nullable is False
    assert units.columns["review_status"].type.length == 32
    assert units.columns["reviewed_by_user_id"].nullable is True
    assert units.columns["reviewed_at"].nullable is True
    assert units.columns["passing_threshold_percent"].nullable is True

    assert set(_table("book_revision_contexts").columns.keys()) == {
        "id",
        "book_revision_id",
        "learning_context_id",
        "created_at",
        "updated_at",
    }
    links = _table("book_revision_contexts")
    assert links.columns["book_revision_id"].nullable is False
    assert links.columns["learning_context_id"].nullable is False
    # Eligibility is exactly these rows — never free-text path/level/subject.
    assert "pathway" not in links.columns
    assert "level" not in links.columns
    assert "subject" not in links.columns

    # P4 questions: authored on an exercise-set unit, answer-key
    # verification is a two-column lockstep (reviewer id + timestamp).
    assert set(_table("book_questions").columns.keys()) == {
        "id",
        "book_revision_id",
        "book_unit_id",
        "question_type",
        "prompt",
        "explanation",
        "display_order",
        "answer_key_verified_by_user_id",
        "answer_key_verified_at",
        "created_at",
        "updated_at",
    }
    questions = _table("book_questions")
    assert questions.columns["book_unit_id"].nullable is False
    assert questions.columns["question_type"].nullable is False
    assert questions.columns["question_type"].type.length == 32
    assert questions.columns["prompt"].nullable is False
    assert questions.columns["explanation"].nullable is False
    assert questions.columns["answer_key_verified_by_user_id"].nullable is True
    assert questions.columns["answer_key_verified_at"].nullable is True

    # P4 options: the answer key is per-option; correctness is only ever
    # set by an explicit verification action.
    assert set(_table("book_question_options").columns.keys()) == {
        "id",
        "book_question_id",
        "option_text",
        "display_order",
        "is_correct",
        "created_at",
        "updated_at",
    }
    options = _table("book_question_options")
    assert options.columns["book_question_id"].nullable is False
    assert options.columns["option_text"].nullable is False
    assert options.columns["is_correct"].nullable is False

    # P4 attempts: an immutable, stored grading outcome (not recomputed).
    assert set(_table("book_attempts").columns.keys()) == {
        "id",
        "student_id",
        "book_revision_id",
        "book_unit_id",
        "total_questions",
        "correct_answers",
        "score_percent",
        "passed",
        "created_at",
        "updated_at",
    }
    attempts = _table("book_attempts")
    assert attempts.columns["student_id"].nullable is False
    assert attempts.columns["book_unit_id"].nullable is False
    assert attempts.columns["total_questions"].nullable is False
    assert attempts.columns["passed"].nullable is False

    # P4 answers: one selected option per question per attempt, with the
    # grading verdict frozen at submission.
    assert set(_table("book_attempt_answers").columns.keys()) == {
        "id",
        "book_attempt_id",
        "book_question_id",
        "selected_option_id",
        "is_correct",
        "created_at",
        "updated_at",
    }
    answers = _table("book_attempt_answers")
    assert answers.columns["book_attempt_id"].nullable is False
    assert answers.columns["book_question_id"].nullable is False
    assert answers.columns["selected_option_id"].nullable is False
    assert answers.columns["is_correct"].nullable is False


def test_book_foreign_keys_follow_the_naming_convention() -> None:
    expectations = {
        "books": {
            "books_created_by_user_id_fkey": ("users.id",),
        },
        "book_revisions": {
            "book_revisions_book_id_fkey": ("books.id",),
            "book_revisions_file_asset_id_fkey": ("file_assets.id",),
            "book_revisions_uploaded_by_user_id_fkey": ("users.id",),
        },
        "book_revision_contexts": {
            "book_revision_contexts_book_revision_id_fkey": ("book_revisions.id",),
            "book_revision_contexts_learning_context_id_fkey": ("learning_contexts.id",),
        },
        "book_units": {
            "book_units_book_revision_id_fkey": ("book_revisions.id",),
            # Composite self-FK: a child can only ever reference a parent
            # of the SAME revision (both columns, enforced by the database).
            "book_units_parent_unit_id_fkey": ("book_units.id",),
            # P3: WHO approved the unit (NO ACTION — review history is
            # never cascade-deleted).
            "book_units_reviewed_by_user_id_fkey": ("users.id",),
        },
        # P4: questions and attempts bind to BOTH the unit and its
        # revision in one composite constraint (same pattern as the
        # book_units self-FK), so a row can never be grafted from one
        # edition onto another.
        "book_questions": {
            "book_questions_book_revision_id_fkey": ("book_revisions.id",),
            "book_questions_book_unit_id_fkey": (
                "book_units.id",
                "book_units.book_revision_id",
            ),
            "book_questions_answer_key_verified_by_user_id_fkey": ("users.id",),
        },
        "book_question_options": {
            "book_question_options_book_question_id_fkey": ("book_questions.id",),
        },
        "book_attempts": {
            "book_attempts_student_id_fkey": ("students.id",),
            "book_attempts_book_revision_id_fkey": ("book_revisions.id",),
            "book_attempts_book_unit_id_fkey": (
                "book_units.id",
                "book_units.book_revision_id",
            ),
        },
        "book_attempt_answers": {
            "book_attempt_answers_book_attempt_id_fkey": ("book_attempts.id",),
            # Composite: the selected option must belong to the answered
            # question — a cross-question pairing is impossible.
            "book_attempt_answers_selected_option_id_fkey": (
                "book_question_options.id",
                "book_question_options.book_question_id",
            ),
        },
    }
    for table_name, expected in expectations.items():
        table = _table(table_name)
        fks = {
            constraint.name: constraint
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        assert set(expected) <= set(fks), (
            f"{table_name}: missing FKs {sorted(set(expected) - set(fks))}"
        )
        for fk_name, referred in expected.items():
            targets = {
                element.target_fullname for element in fks[fk_name].elements
            }
            assert set(referred) <= targets, (
                f"{table_name}.{fk_name} should reference {referred}, got {targets}"
            )
            # History preservation: no ON DELETE cascade may silently drop a
            # published book, its source asset or a canonical association.
            assert fks[fk_name].ondelete is None, fk_name


def test_book_check_constraints_match_the_enum_vocabulary() -> None:
    def _literals(check) -> list[str]:
        return sorted(verify_database._check_literals(str(check.sqltext)))

    revisions = _table("book_revisions")
    checks = {
        c.name: c
        for c in revisions.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "book_revisions_status_check" in checks
    assert "book_revisions_revision_number_check" in checks
    assert _literals(checks["book_revisions_status_check"]) == sorted(
        status.value for status in BookRevisionStatus
    )
    assert "revision_number >= 1" in str(
        checks["book_revisions_revision_number_check"].sqltext
    )
    # P3: a rejected revision without a recorded reason is impossible.
    assert "book_revisions_rejected_reason_check" in checks
    assert str(checks["book_revisions_rejected_reason_check"].sqltext) == (
        "status <> 'rejected' OR rejection_reason IS NOT NULL"
    )

    units = _table("book_units")
    unit_checks = {
        c.name: c
        for c in units.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "book_units_kind_check" in unit_checks
    assert _literals(unit_checks["book_units_kind_check"]) == sorted(
        kind.value for kind in BookUnitKind
    )
    assert "book_units_assessment_requirement_check" in unit_checks
    assert _literals(unit_checks["book_units_assessment_requirement_check"]) == sorted(
        requirement.value for requirement in AssessmentRequirement
    )
    assert "book_units_title_check" in unit_checks
    assert "book_units_display_order_check" in unit_checks
    assert "book_units_page_start_check" in unit_checks
    assert "book_units_page_range_check" in unit_checks

    # P3 review-state CHECKs: the vocabulary, and the paired identity
    # rules (approved REQUIRES reviewer+timestamp, pending REQUIRES none).
    assert "book_units_review_status_check" in unit_checks
    assert _literals(unit_checks["book_units_review_status_check"]) == sorted(
        state.value for state in BookUnitReviewStatus
    )
    assert str(unit_checks["book_units_review_approved_check"].sqltext) == (
        "review_status <> 'approved' OR "
        "(reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL)"
    )
    assert str(unit_checks["book_units_review_pending_check"].sqltext) == (
        "review_status <> 'pending' OR "
        "(reviewed_by_user_id IS NULL AND reviewed_at IS NULL)"
    )
    # P4: the per-exercise-set passing threshold is NULL (default 70) or
    # a 1-100 percent on an exercise_set unit — never on a chapter/topic.
    assert "book_units_passing_threshold_check" in unit_checks
    assert str(unit_checks["book_units_passing_threshold_check"].sqltext) == (
        "passing_threshold_percent IS NULL OR "
        "(kind = 'exercise_set' AND passing_threshold_percent BETWEEN 1 AND 100)"
    )

    questions = _table("book_questions")
    question_checks = {
        c.name: c
        for c in questions.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "book_questions_question_type_check" in question_checks
    assert _literals(question_checks["book_questions_question_type_check"]) == sorted(
        qtype.value for qtype in BookQuestionType
    )
    # The prompt and the corrective explanation are never blank — the
    # explanation is the feedback shown for a wrong answer.
    assert str(question_checks["book_questions_prompt_check"].sqltext) == "prompt <> ''"
    assert str(question_checks["book_questions_explanation_check"].sqltext) == (
        "explanation <> ''"
    )
    assert "book_questions_display_order_check" in question_checks
    # Answer-key verification is a two-column lockstep, exactly like the
    # book_units review state: reviewer id and timestamp are set (or
    # both NULL) together — correctness is never half-recorded.
    assert str(question_checks["book_questions_verified_check"].sqltext) == (
        "answer_key_verified_by_user_id IS NULL "
        "OR answer_key_verified_at IS NOT NULL"
    )
    assert str(question_checks["book_questions_unverified_check"].sqltext) == (
        "answer_key_verified_at IS NULL "
        "OR answer_key_verified_by_user_id IS NOT NULL"
    )

    option_checks = {
        c.name: c
        for c in _table("book_question_options").constraints
        if isinstance(c, CheckConstraint)
    }
    assert str(option_checks["book_question_options_text_check"].sqltext) == (
        "option_text <> ''"
    )
    assert "book_question_options_display_order_check" in option_checks

    attempt_checks = {
        c.name: c
        for c in _table("book_attempts").constraints
        if isinstance(c, CheckConstraint)
    }
    assert "book_attempts_total_questions_check" in attempt_checks
    assert str(attempt_checks["book_attempts_total_questions_check"].sqltext) == (
        "total_questions >= 1"
    )
    assert "book_attempts_correct_answers_check" in attempt_checks
    assert "book_attempts_score_percent_check" in attempt_checks


def test_book_indexes_are_declared_including_the_unique_edition_keys() -> None:
    expectations = {
        "books": {"books_created_by_user_id_idx"},
        "book_revisions": {
            "uq_book_revisions_book_revision_number_key",
            "book_revisions_book_id_idx",
            "book_revisions_file_asset_id_idx",
            "book_revisions_uploaded_by_user_id_idx",
            "book_revisions_status_idx",
            # P3: at most ONE current publication per book (partial).
            "uq_book_revisions_book_current_key",
        },
        "book_revision_contexts": {
            "uq_book_revision_contexts_revision_context_key",
            "book_revision_contexts_context_id_idx",
        },
        "book_units": {
            "book_units_book_revision_id_idx",
            "book_units_parent_unit_id_idx",
            # P3: the reviewer FK column carries its own index.
            "book_units_reviewed_by_user_id_idx",
            # Sibling order is collision-free for children AND roots.
            "uq_book_units_revision_parent_order_key",
            "uq_book_units_revision_root_order_key",
        },
        # P4: one order slot per exercise set, verifier FK column, and
        # the attempt/answer FK columns (PostgreSQL does not index FKs).
        "book_questions": {
            "book_questions_book_revision_id_idx",
            "book_questions_book_unit_id_idx",
            "book_questions_answer_key_verified_by_user_id_idx",
            "uq_book_questions_unit_order_key",
        },
        "book_question_options": {
            "book_question_options_book_question_id_idx",
            "uq_book_question_options_question_order_key",
            # At most ONE correct option per question (partial — a
            # draft question may have zero correct options, never two).
            "uq_book_question_options_question_correct_key",
        },
        "book_attempts": {
            "book_attempts_student_id_idx",
            "book_attempts_book_revision_id_idx",
            "book_attempts_book_unit_id_idx",
            # Attempt history for one student on one exercise set (P5).
            "book_attempts_student_unit_idx",
        },
        "book_attempt_answers": {
            "book_attempt_answers_book_attempt_id_idx",
            "book_attempt_answers_book_question_id_idx",
            "book_attempt_answers_selected_option_id_idx",
        },
    }
    for table_name, expected in expectations.items():
        indexes = {
            index.name for index in _table(table_name).indexes
            if isinstance(index, Index)
        }
        assert expected <= indexes, (
            f"{table_name}: missing indexes {sorted(expected - indexes)}"
        )

    unique_editions = next(
        i for i in _table("book_revisions").indexes
        if i.name == "uq_book_revisions_book_revision_number_key"
    )
    assert unique_editions.unique is True
    assert list(unique_editions.columns.keys()) == ["book_id", "revision_number"]

    unique_link = next(
        i for i in _table("book_revision_contexts").indexes
        if i.name == "uq_book_revision_contexts_revision_context_key"
    )
    assert unique_link.unique is True
    assert list(unique_link.columns.keys()) == [
        "book_revision_id",
        "learning_context_id",
    ]

    # The current-publication pointer is a PARTIAL unique index: historical
    # published revisions (flag false) never collide with the live one.
    current_pointer = next(
        i for i in _table("book_revisions").indexes
        if i.name == "uq_book_revisions_book_current_key"
    )
    assert current_pointer.unique is True
    assert list(current_pointer.columns.keys()) == ["book_id"]
    assert current_pointer.dialect_options.get("postgresql", {}).get("where") is not None

    # The composite self-FK target: a table-level UNIQUE on (id, revision).
    from sqlalchemy import UniqueConstraint

    unit_uniques = {
        c.name: c
        for c in _table("book_units").constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "book_units_id_revision_key" in unit_uniques
    assert sorted(col.name for col in unit_uniques["book_units_id_revision_key"].columns) == [
        "book_revision_id",
        "id",
    ]
    # Both sibling-order indexes are PARTIAL: roots (NULL parent) are
    # protected too, which a plain UNIQUE constraint could never do.
    partial_names = {
        i.name
        for i in _table("book_units").indexes
        if i.name
        in {
            "uq_book_units_revision_parent_order_key",
            "uq_book_units_revision_root_order_key",
        }
        and (i.dialect_options.get("postgresql", {}).get("where") is not None)
    }
    assert partial_names == {
        "uq_book_units_revision_parent_order_key",
        "uq_book_units_revision_root_order_key",
    }

    # P4: composite FK targets (id + owning row) on questions, options
    # and attempt answers — the same table-level UNIQUE pattern as
    # book_units' (id, revision) pair.
    question_uniques = {
        c.name: c
        for c in _table("book_questions").constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "book_questions_id_revision_key" in question_uniques
    assert sorted(
        col.name for col in question_uniques["book_questions_id_revision_key"].columns
    ) == ["book_revision_id", "id"]

    option_uniques = {
        c.name: c
        for c in _table("book_question_options").constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "book_question_options_id_question_key" in option_uniques
    assert sorted(
        col.name
        for col in option_uniques["book_question_options_id_question_key"].columns
    ) == ["book_question_id", "id"]

    answer_uniques = {
        c.name: c
        for c in _table("book_attempt_answers").constraints
        if isinstance(c, UniqueConstraint)
    }
    # One answer per question per attempt.
    assert "uq_book_attempt_answers_attempt_question_key" in answer_uniques
    assert sorted(
        col.name
        for col in answer_uniques["uq_book_attempt_answers_attempt_question_key"].columns
    ) == ["book_attempt_id", "book_question_id"]

    # The "at most one correct option per question" index is PARTIAL —
    # correctness stays optional until an explicit verification.
    correct_key = next(
        i
        for i in _table("book_question_options").indexes
        if i.name == "uq_book_question_options_question_correct_key"
    )
    assert correct_key.unique is True
    assert list(correct_key.columns.keys()) == ["book_question_id"]
    assert (
        correct_key.dialect_options.get("postgresql", {}).get("where") is not None
    )


def test_book_tables_have_no_model_migration_drift() -> None:
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )
    problems = verify_database.diff_schemas(
        {t: metadata_schema[t] for t in _NEW_TABLES},
        {t: migration_schema[t] for t in _NEW_TABLES},
        "models",
        "migrations",
    )
    assert problems == [], problems

    migration_checks = migration_schema["book_revisions"]["checks"]
    assert sorted(
        verify_database._check_literals(
            migration_checks["book_revisions_status_check"]
        )
    ) == sorted(status.value for status in BookRevisionStatus)


def test_book_tables_appear_in_verify_database_expectations() -> None:
    for table in _NEW_TABLES:
        assert table in verify_database.EXPECTED_TABLES
        assert table in verify_database.LATER_MIGRATION_TABLES
    assert verify_database.EXPECTED_REVISION == "0021"
    assert verify_database.EXPECTED_REVISION in {
        p.stem.split("_")[0]
        for p in (verify_database.MIGRATION_DIR).glob("0*.py")
        if p.name != "__init__.py"
    }
    for index_name in (
        "books_created_by_user_id_idx",
        "book_revisions_book_id_idx",
        "book_revisions_file_asset_id_idx",
        "book_revisions_uploaded_by_user_id_idx",
        "book_revision_contexts_context_id_idx",
        "book_units_book_revision_id_idx",
        "book_units_parent_unit_id_idx",
        "book_units_reviewed_by_user_id_idx",
        # P4 FK columns.
        "book_questions_book_revision_id_idx",
        "book_questions_book_unit_id_idx",
        "book_questions_answer_key_verified_by_user_id_idx",
        "book_question_options_book_question_id_idx",
        "book_attempts_student_id_idx",
        "book_attempts_book_revision_id_idx",
        "book_attempts_book_unit_id_idx",
        "book_attempts_student_unit_idx",
        "book_attempt_answers_book_attempt_id_idx",
        "book_attempt_answers_book_question_id_idx",
        "book_attempt_answers_selected_option_id_idx",
    ):
        assert index_name in verify_database.EXPECTED_FK_INDEXES


def test_book_tables_appear_in_application_truncation_order() -> None:
    from tests import conftest as tests_conftest

    for table in _NEW_TABLES:
        assert table in tests_conftest.APPLICATION_TABLES
    order = tests_conftest.APPLICATION_TABLES
    # Children before parents: answers → attempts, options → questions,
    # questions/attempts → units → context links → revisions → books →
    # assets/users.
    assert order.index("book_attempt_answers") < order.index("book_attempts")
    assert order.index("book_question_options") < order.index("book_questions")
    assert order.index("book_questions") < order.index("book_units")
    assert order.index("book_attempts") < order.index("book_units")
    assert order.index("book_units") < order.index("book_revisions")
    assert order.index("book_revision_contexts") < order.index("book_revisions")
    assert order.index("book_revisions") < order.index("books")
    assert order.index("books") < order.index("file_assets")
    assert order.index("books") < order.index("users")


def test_book_read_schemas_require_their_fields_and_expose_no_paths() -> None:
    with pytest.raises(ValidationError):
        BookRead.model_validate({"title": "no id"})
    with pytest.raises(ValidationError):
        BookRevisionRead.model_validate({"revision_id": "x"})
    with pytest.raises(ValidationError):
        BookRead.model_validate(
            {"book_id": "x", "title": "t", "storage_key": "file_assets/1"}
        )

    assert "storage_key" not in BookRead.model_fields
    assert "storage_key" not in BookRevisionRead.model_fields
    assert "storage_key" not in FileAssetRead.model_fields
    # The uploader comes from the token, never from a request body.
    assert "uploaded_by_user_id" in BookRevisionRead.model_fields
    assert "learning_context_ids" in BookRead.model_fields
    # P2 extraction outcome: counts/warnings and a fixed failure code —
    # never extracted document text or storage details.
    assert "extraction_summary" in BookRevisionRead.model_fields
    assert "extraction_error" in BookRevisionRead.model_fields
    assert "content" not in BookRevisionRead.model_fields
    assert "units" not in BookRevisionRead.model_fields

    # P3: the publication pointer is part of the plain revision read, but
    # the unit tree and the rejection reason live ONLY on the detail read
    # — a book listing never drags every unit of every edition along.
    assert "is_current_publication" in BookRevisionRead.model_fields
    assert BookRevisionRead.model_fields["is_current_publication"].default is False
    assert "rejection_reason" not in BookRevisionRead.model_fields
    assert "units" in BookRevisionDetailRead.model_fields
    assert "rejection_reason" in BookRevisionDetailRead.model_fields
    assert set(BookRevisionDetailRead.model_fields) == (
        set(BookRevisionRead.model_fields) | {"units", "rejection_reason"}
    )
    # Inspection never leaks storage details either.
    assert "storage_key" not in BookRevisionDetailRead.model_fields
    assert "storage_key" not in BookUnitRead.model_fields


def test_book_review_request_schemas_are_strict_and_forgiving_in_the_right_ways() -> None:
    """Corrections take ONLY supplied fields; rejection demands a reason."""
    # Every correction field is optional — an empty body is valid schema
    # (the SERVICE refuses it as "no correction fields supplied", 422).
    assert all(
        not field.is_required() for field in BookUnitCorrectRequest.model_fields.values()
    )
    # unknown fields are refused, not silently dropped (extra=forbid).
    assert BookUnitCorrectRequest.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        BookUnitCorrectRequest.model_validate({"storage_key": "x"})
    # Explicit null is representable for schema purposes (the service
    # allows it only for parent_unit_id; other nulls → 422 there).
    assert BookUnitCorrectRequest.model_validate({"content": None}) is not None

    # The rejection reason is mandatory and bounded like material reject.
    with pytest.raises(ValidationError):
        BookRevisionRejectRequest.model_validate({})
    with pytest.raises(ValidationError):
        BookRevisionRejectRequest.model_validate({"reason": ""})
    with pytest.raises(ValidationError):
        BookRevisionRejectRequest.model_validate({"reason": "x" * 2001})
    assert BookRevisionRejectRequest.model_config.get("extra") == "forbid"
    assert BookRevisionRejectRequest.model_validate(
        {"reason": "spurious pages"}
    ).reason == "spurious pages"

    # A blank/whitespace reason is refused by the SERVICE (strip first).
    assert BookRevisionRejectRequest.model_validate({"reason": "   "}).reason == "   "


def test_book_revision_status_vocabulary_is_the_six_intake_states() -> None:
    assert [s.value for s in BookRevisionStatus] == [
        "uploaded",
        "extracting",
        "extraction_failed",
        "pending_review",
        "published",
        "rejected",
    ]
    assert sql_in_list(BookRevisionStatus) == (
        "'uploaded', 'extracting', 'extraction_failed', "
        "'pending_review', 'published', 'rejected'"
    )
    # P1 writes exactly one of them; the rest are reserved for later phases.
    assert BookRevisionStatus.UPLOADED.value == "uploaded"


def test_the_p1_endpoint_names_bypass_the_banned_segment_tripwire() -> None:
    """``/admin/books`` carries none of the forbidden path substrings."""
    from fastapi.routing import APIRoute

    from app.api.v1.router import api_router

    banned = (
        "message", "conversation", "chat", "thread", "payment", "price",
        "purchase", "checkout", "rating", "review", "assignment", "homework",
        "quiz", "feedback", "search", "directory",
    )
    book_routes = [
        (method, route.path)
        for route in api_router.routes
        if isinstance(route, APIRoute) and "book" in route.path
        for method in route.methods
        if method not in ("HEAD", "OPTIONS")
    ]
    assert book_routes, "the book routes must be registered"
    violations = [
        (method, path, segment)
        for method, path in book_routes
        for segment in banned
        if segment in path
    ]
    assert violations == [], violations


def test_the_book_status_enum_never_drifts_from_the_asset_vocabulary() -> None:
    """Intake reuses the shared file-asset states it already depends on."""
    assert "stored" in {s.value for s in FileAssetStatus}
    assert "validating" in {s.value for s in FileAssetStatus}


# --- P4 objective assessment schemas -------------------------------------------------


def test_the_question_type_enum_is_the_two_objective_kinds() -> None:
    assert [q.value for q in BookQuestionType] == [
        "multiple_choice",
        "true_false",
    ]
    assert sql_in_list(BookQuestionType) == (
        "'multiple_choice', 'true_false'"
    )


def test_question_read_exposes_the_key_but_attempt_feedback_never_does() -> None:
    """The answer key lives on the ADMIN surface only.

    ``BookQuestionRead`` carries ``is_correct`` (administrators must
    see the key), but the student-shaped attempt payload's feedback is
    exactly ``question_id`` + ``explanation`` — never an option id,
    never a correctness flag.
    """
    from app.schemas.book_assessment import BookQuestionOptionRead

    # The admin question surface carries the key via its options list.
    assert "options" in BookQuestionRead.model_fields
    assert "is_correct" in BookQuestionOptionRead.model_fields
    assert set(AssessmentQuestionFeedback.model_fields) == {
        "question_id",
        "explanation",
    }
    assert "correct_option_id" not in AssessmentQuestionFeedback.model_fields
    assert "is_correct" not in AssessmentQuestionFeedback.model_fields
    # The attempt result carries the frozen verdict and feedback —
    # never the key.
    assert set(AssessmentAttemptRead.model_fields) == {
        "attempt_id",
        "book_revision_id",
        "book_unit_id",
        "total_questions",
        "correct_answers",
        "score_percent",
        "passed",
        "submitted_at",
        "feedback",
    }
    assert "correct_option_id" not in AssessmentAttemptRead.model_fields


def test_question_authoring_schemas_are_strict_in_the_right_ways() -> None:
    # Creation demands every field; correctness is never among them.
    assert set(BookQuestionCreate.model_fields) == {
        "question_type",
        "prompt",
        "explanation",
        "display_order",
        "options",
    }
    assert "is_correct" not in BookQuestionCreate.model_fields
    assert BookQuestionCreate.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        BookQuestionCreate.model_validate({})
    # Options are bounded at the schema level (2-10); the service
    # additionally enforces exactly-2 for true_false.
    from pydantic.types import MaxLen, MinLen

    options_meta = BookQuestionCreate.model_fields["options"].metadata
    assert any(isinstance(m, MinLen) and m.min_length == 2 for m in options_meta), (
        options_meta
    )
    assert any(isinstance(m, MaxLen) and m.max_length == 10 for m in options_meta), (
        options_meta
    )

    # Correction: every field optional (empty body is a service-level
    # 422, not a schema error), extra fields refused, correctness never
    # settable through a correction either.
    assert all(
        not field.is_required()
        for field in BookQuestionCorrectRequest.model_fields.values()
    )
    assert BookQuestionCorrectRequest.model_config.get("extra") == "forbid"
    assert "is_correct" not in BookQuestionCorrectRequest.model_fields

    # Verification names THE correct option and nothing else.
    assert set(BookQuestionVerifyRequest.model_fields) == {"correct_option_id"}
    assert BookQuestionVerifyRequest.model_config.get("extra") == "forbid"

    # The threshold is 1-100 or null (default 70); never a free number.
    assert BookUnitThresholdRequest.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        BookUnitThresholdRequest.model_validate({"passing_threshold_percent": 0})
    with pytest.raises(ValidationError):
        BookUnitThresholdRequest.model_validate({"passing_threshold_percent": 101})
    assert (
        BookUnitThresholdRequest.model_validate(
            {"passing_threshold_percent": None}
        ).passing_threshold_percent
        is None
    )
    assert (
        BookUnitThresholdRequest.model_validate(
            {"passing_threshold_percent": 85}
        ).passing_threshold_percent
        == 85
    )
