"""Unit tests: the Phase 2 slice 2A curriculum tables, migration 0011.

Slice 2A adds ``topics`` and ``lessons`` beneath an existing teaching
offering. Model, migration and the offline verification script must agree
— the drift tests below run ``scripts/verify_database.py``'s own schema
descriptors, which compare the ORM metadata against the parsed migration
DDL column by column, constraint by constraint.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, Index

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_spec = importlib.util.spec_from_file_location(
    "verify_database", BACKEND_DIR / "scripts" / "verify_database.py"
)
verify_database = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_database", verify_database)
_spec.loader.exec_module(verify_database)

_NEW_TABLES = ("topics", "lessons")


def _table(name: str):
    return Base.metadata.tables[name]


def test_curriculum_tables_are_registered_with_expected_columns() -> None:
    assert set(_NEW_TABLES) <= set(Base.metadata.tables)
    assert set(_table("topics").columns.keys()) == {
        "id",
        "teaching_offering_id",
        "title",
        "description",
        "display_order",
        "created_at",
        "updated_at",
    }
    topics = _table("topics")
    assert topics.columns["teaching_offering_id"].nullable is False
    assert topics.columns["title"].nullable is False
    assert topics.columns["title"].type.length == 200
    assert topics.columns["description"].nullable is True
    assert topics.columns["display_order"].nullable is False

    assert set(_table("lessons").columns.keys()) == {
        "id",
        "topic_id",
        "title",
        "description",
        "display_order",
        "created_at",
        "updated_at",
    }
    lessons = _table("lessons")
    assert lessons.columns["topic_id"].nullable is False
    assert lessons.columns["title"].nullable is False
    assert lessons.columns["title"].type.length == 200
    assert lessons.columns["description"].nullable is True
    assert lessons.columns["display_order"].nullable is False


def test_curriculum_foreign_keys_follow_the_naming_convention() -> None:
    expectations = {
        "topics": {
            "topics_teaching_offering_id_fkey": "teaching_offerings.id",
        },
        "lessons": {
            "lessons_topic_id_fkey": "topics.id",
        },
    }
    for table_name, expected in expectations.items():
        fks = {
            c.name: c
            for c in _table(table_name).constraints
            if isinstance(c, ForeignKeyConstraint)
        }
        assert set(fks) == set(expected), (table_name, sorted(fks))
        for name, target in expected.items():
            assert fks[name].elements[0].target_fullname == target


def test_display_order_is_unique_within_its_parent() -> None:
    """Stable ordering is a database invariant, not a read-time guess.

    Declared as ``Index(... unique=True)`` — the same shape Phase 1 uses
    for its offering uniqueness — so the ORM and migration 0011 agree.
    """
    topics = _table("topics")
    topic_unique = next(
        i for i in topics.indexes if i.name == "uq_topics_teaching_offering_display_order_key"
    )
    assert topic_unique.unique is True
    assert [c.name for c in topic_unique.columns] == [
        "teaching_offering_id",
        "display_order",
    ]

    lessons = _table("lessons")
    lesson_unique = next(
        i for i in lessons.indexes if i.name == "uq_lessons_topic_display_order_key"
    )
    assert lesson_unique.unique is True
    assert [c.name for c in lesson_unique.columns] == ["topic_id", "display_order"]


def test_display_order_checks_require_a_meaningful_position() -> None:
    for table_name, check_name in (
        ("topics", "topics_display_order_check"),
        ("lessons", "lessons_display_order_check"),
    ):
        table = _table(table_name)
        check = next(
            c
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name == check_name
        )
        text = str(check.sqltext)
        assert "display_order" in text and ">=" in text and "1" in text


def test_lesson_topic_foreign_key_is_cascade() -> None:
    lessons = _table("lessons")
    fk = next(
        c
        for c in lessons.constraints
        if isinstance(c, ForeignKeyConstraint) and c.name == "lessons_topic_id_fkey"
    )
    assert "CASCADE" in str(fk.ondelete).upper()


def _migration_head() -> str:
    """The last revision declared across the migration files (in file order)."""
    import ast

    head = ""
    for path in verify_database._migration_files():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "revision":
                head = ast.literal_eval(node.value)
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "revision"
            ):
                head = ast.literal_eval(node.value)
    return head


def test_verification_script_covers_the_new_tables_and_head() -> None:
    assert verify_database.EXPECTED_REVISION == _migration_head()
    for table in _NEW_TABLES:
        assert table in verify_database.EXPECTED_TABLES
        assert table in verify_database.LATER_MIGRATION_TABLES


def test_truncation_list_covers_the_new_tables() -> None:
    """Integration cleanup must truncate the new tables too."""
    import conftest as tests_conftest

    for table in _NEW_TABLES:
        assert table in tests_conftest.APPLICATION_TABLES


def test_new_tables_have_no_model_migration_drift() -> None:
    """The objects this slice introduces must match migration 0011 exactly."""
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


def test_slice_adds_no_new_drift_problems() -> None:
    """The whole-schema comparison may not gain a problem naming the new tables."""
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )
    problems = verify_database.diff_schemas(
        metadata_schema, migration_schema, "models", "migrations"
    )
    introduced = [
        p for p in problems if any(name in p for name in _NEW_TABLES)
    ]
    assert introduced == [], introduced
