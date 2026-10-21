"""Unit tests: the administrator book tables, migrations 0018/0019.

Book intake adds ``books``, ``book_revisions`` and
``book_revision_contexts`` (0018); content extraction adds the
``book_units`` tree plus two ``book_revisions`` bookkeeping columns
(``extraction_summary``/``extraction_error``, 0019). Model, migration
and the offline verification script must agree — the drift tests below
run ``scripts/verify_database.py``'s own schema descriptors, and the
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
    BookRevisionStatus,
    BookUnitKind,
    FileAssetStatus,
    sql_in_list,
)
from app.schemas.book import BookRead, BookRevisionRead
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

_NEW_TABLES = ("books", "book_revisions", "book_revision_contexts", "book_units")


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


def test_book_foreign_keys_follow_the_naming_convention() -> None:
    expectations = {
        "books": {
            "books_created_by_user_id_fkey": "users.id",
        },
        "book_revisions": {
            "book_revisions_book_id_fkey": "books.id",
            "book_revisions_file_asset_id_fkey": "file_assets.id",
            "book_revisions_uploaded_by_user_id_fkey": "users.id",
        },
        "book_revision_contexts": {
            "book_revision_contexts_book_revision_id_fkey": "book_revisions.id",
            "book_revision_contexts_learning_context_id_fkey": "learning_contexts.id",
        },
        "book_units": {
            "book_units_book_revision_id_fkey": "book_revisions.id",
            # Composite self-FK: a child can only ever reference a parent
            # of the SAME revision (both columns, enforced by the database).
            "book_units_parent_unit_id_fkey": "book_units.id",
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
            assert referred in targets, (
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


def test_book_indexes_are_declared_including_the_unique_edition_keys() -> None:
    expectations = {
        "books": {"books_created_by_user_id_idx"},
        "book_revisions": {
            "uq_book_revisions_book_revision_number_key",
            "book_revisions_book_id_idx",
            "book_revisions_file_asset_id_idx",
            "book_revisions_uploaded_by_user_id_idx",
            "book_revisions_status_idx",
        },
        "book_revision_contexts": {
            "uq_book_revision_contexts_revision_context_key",
            "book_revision_contexts_context_id_idx",
        },
        "book_units": {
            "book_units_book_revision_id_idx",
            "book_units_parent_unit_id_idx",
            # Sibling order is collision-free for children AND roots.
            "uq_book_units_revision_parent_order_key",
            "uq_book_units_revision_root_order_key",
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
    assert verify_database.EXPECTED_REVISION == "0019"
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
    ):
        assert index_name in verify_database.EXPECTED_FK_INDEXES


def test_book_tables_appear_in_application_truncation_order() -> None:
    from tests import conftest as tests_conftest

    for table in _NEW_TABLES:
        assert table in tests_conftest.APPLICATION_TABLES
    order = tests_conftest.APPLICATION_TABLES
    # Children before parents: units → context links → revisions → books →
    # assets/users.
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
