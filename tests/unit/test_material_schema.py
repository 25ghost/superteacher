"""Unit tests: the Phase 2 slice 2B material tables, migration 0012.

Slice 2B adds ``file_assets``, ``materials`` and ``material_moderations``
under an existing teaching offering. Model, migration and the offline
verification script must agree — the drift tests below run
``scripts/verify_database.py``'s own schema descriptors.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, Index

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.enums import (
    FileAssetStatus,
    MaterialStatus,
    MaterialType,
    sql_in_list,
)
from app.schemas.material import (
    FileAssetRead,
    MaterialDetailRead,
    MaterialRead,
    MaterialRejectRequest,
    MaterialUpdate,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_spec = importlib.util.spec_from_file_location(
    "verify_database", BACKEND_DIR / "scripts" / "verify_database.py"
)
verify_database = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_database", verify_database)
_spec.loader.exec_module(verify_database)

_NEW_TABLES = ("file_assets", "materials", "material_moderations")


def _table(name: str):
    return Base.metadata.tables[name]


def test_material_tables_are_registered_with_expected_columns() -> None:
    assert set(_NEW_TABLES) <= set(Base.metadata.tables)
    assert set(_table("file_assets").columns.keys()) == {
        "id",
        "uploaded_by_user_id",
        "original_filename",
        "content_type",
        "size_bytes",
        "checksum_sha256",
        "storage_key",
        "validation_status",
        "validation_error",
        "created_at",
        "updated_at",
    }
    assets = _table("file_assets")
    assert assets.columns["uploaded_by_user_id"].nullable is False
    assert assets.columns["original_filename"].nullable is False
    assert assets.columns["original_filename"].type.length == 255
    assert assets.columns["content_type"].type.length == 100
    assert assets.columns["checksum_sha256"].type.length == 64
    assert assets.columns["storage_key"].type.length == 512
    assert assets.columns["validation_status"].nullable is False
    assert assets.columns["validation_error"].nullable is True

    assert set(_table("materials").columns.keys()) == {
        "id",
        "teaching_offering_id",
        "lesson_id",
        "title",
        "description",
        "material_type",
        "status",
        "file_asset_id",
        "created_at",
        "updated_at",
    }
    materials = _table("materials")
    assert materials.columns["teaching_offering_id"].nullable is False
    assert materials.columns["lesson_id"].nullable is True
    assert materials.columns["title"].nullable is False
    assert materials.columns["title"].type.length == 200
    assert materials.columns["material_type"].nullable is False
    assert materials.columns["status"].nullable is False
    assert materials.columns["file_asset_id"].nullable is False

    assert set(_table("material_moderations").columns.keys()) == {
        "id",
        "material_id",
        "reviewer_user_id",
        "decision",
        "reason",
        "created_at",
        "updated_at",
    }
    moderations = _table("material_moderations")
    assert moderations.columns["material_id"].nullable is False
    assert moderations.columns["reviewer_user_id"].nullable is False
    assert moderations.columns["decision"].nullable is False
    assert moderations.columns["reason"].nullable is True


def test_material_foreign_keys_follow_the_naming_convention() -> None:
    expectations = {
        "file_assets": {
            "file_assets_uploaded_by_user_id_fkey": "users.id",
        },
        "materials": {
            "materials_teaching_offering_id_fkey": "teaching_offerings.id",
            "materials_lesson_id_fkey": "lessons.id",
            "materials_file_asset_id_fkey": "file_assets.id",
        },
        "material_moderations": {
            "material_moderations_material_id_fkey": "materials.id",
            "material_moderations_reviewer_user_id_fkey": "users.id",
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
            constraint = fks[fk_name]
            targets = {
                element.target_fullname for element in constraint.elements
            }
            assert referred in targets, (
                f"{table_name}.{fk_name} should reference {referred}, got {targets}"
            )


def test_material_check_constraints_match_the_enum_vocabulary() -> None:
    def _literals(check) -> list[str]:
        return sorted(verify_database._check_literals(str(check.sqltext)))

    materials = _table("materials")
    material_checks = {
        c.name: c
        for c in materials.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "materials_material_type_check" in material_checks
    assert "materials_status_check" in material_checks
    assert _literals(material_checks["materials_material_type_check"]) == sorted(
        m.value for m in MaterialType
    )
    assert _literals(material_checks["materials_status_check"]) == sorted(
        m.value for m in MaterialStatus
    )

    assets = _table("file_assets")
    asset_checks = {
        c.name: c
        for c in assets.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "file_assets_validation_status_check" in asset_checks
    assert "file_assets_size_bytes_check" in asset_checks
    assert _literals(asset_checks["file_assets_validation_status_check"]) == sorted(
        m.value for m in FileAssetStatus
    )

    moderations = _table("material_moderations")
    mod_checks = {
        c.name: c
        for c in moderations.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "material_moderations_decision_check" in mod_checks
    assert _literals(mod_checks["material_moderations_decision_check"]) == [
        "approved",
        "rejected",
    ]  # already sorted


def test_material_tables_have_no_model_migration_drift() -> None:
    """Migration 0014's data migration + CHECK swap must land on the models.

    0012 creates ``materials`` with the old ``book/note/exercise`` CHECK and
    0014 swaps it for the MVP vocabulary; the *combined* chain (what
    ``alembic upgrade head`` produces) has to match the ORM exactly.
    """
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

    assert verify_database._checks_match(
        metadata_schema["materials"]["checks"]["materials_material_type_check"],
        migration_schema["materials"]["checks"]["materials_material_type_check"],
    )
    assert verify_database._check_literals(
        migration_schema["materials"]["checks"]["materials_material_type_check"]
    ) == ("pdf_document", "video")


def test_material_indexes_are_declared() -> None:
    expectations = {
        "file_assets": {"file_assets_uploaded_by_user_id_idx"},
        "materials": {
            "materials_teaching_offering_id_idx",
            "materials_lesson_id_idx",
            "materials_file_asset_id_idx",
            "materials_status_idx",
        },
        "material_moderations": {
            "material_moderations_material_id_idx",
            "material_moderations_reviewer_user_id_idx",
        },
    }
    for table_name, expected in expectations.items():
        indexes = {
            index.name
            for index in _table(table_name).indexes
            if isinstance(index, Index)
        }
        assert expected <= indexes, (
            f"{table_name}: missing indexes {sorted(expected - indexes)}"
        )


def test_material_tables_appear_in_verify_database_expectations() -> None:
    for table in _NEW_TABLES:
        assert table in verify_database.EXPECTED_TABLES
        assert table in verify_database.LATER_MIGRATION_TABLES
    # Head may advance beyond slice 2B (slice 2C added material_progress).
    assert verify_database.EXPECTED_REVISION >= "0012"
    assert verify_database.EXPECTED_REVISION in {
        p.stem.split("_")[0]
        for p in (verify_database.MIGRATION_DIR).glob("0*.py")
        if p.name != "__init__.py"
    }
    assert "materials_teaching_offering_id_idx" in verify_database.EXPECTED_FK_INDEXES
    assert "file_assets_uploaded_by_user_id_idx" in verify_database.EXPECTED_FK_INDEXES
    assert (
        "material_progress_student_id_idx" in verify_database.EXPECTED_FK_INDEXES
    )
    assert "material_progress" in verify_database.EXPECTED_TABLES


def test_material_tables_appear_in_application_truncation_order() -> None:
    from tests import conftest as tests_conftest

    for table in _NEW_TABLES:
        assert table in tests_conftest.APPLICATION_TABLES
    # Children before parents: moderations → materials → file_assets → offerings.
    order = tests_conftest.APPLICATION_TABLES
    assert order.index("material_moderations") < order.index("materials")
    assert order.index("materials") < order.index("file_assets")
    assert order.index("file_assets") < order.index("teaching_offerings")


def test_material_schemas_reject_unknown_keys_and_require_reason() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MaterialUpdate(title=None)  # explicit null title
    with pytest.raises(ValidationError):
        MaterialUpdate()  # no fields
    with pytest.raises(ValidationError):
        MaterialUpdate.model_validate({"title": "ok", "teacher_id": "x"})

    with pytest.raises(ValidationError):
        MaterialRejectRequest(reason="")
    with pytest.raises(ValidationError):
        MaterialRejectRequest.model_validate({"reason": "x", "admin_id": "x"})
    ok = MaterialRejectRequest(reason="incomplete exercise")
    assert ok.reason == "incomplete exercise"

    # Read schemas expose file metadata but never a storage path.
    read_fields = MaterialRead.model_fields
    assert "file_asset_id" not in read_fields  # nested under file_asset
    assert "storage_key" not in FileAssetRead.model_fields
    assert "storage_key" not in MaterialRead.model_fields
    assert "storage_key" not in MaterialDetailRead.model_fields


def test_material_enums_use_the_expected_values() -> None:
    assert [m.value for m in MaterialType] == ["video", "pdf_document"]
    assert [m.value for m in MaterialStatus] == [
        "draft",
        "pending_review",
        "rejected",
        "published",
        "archived",
    ]
    assert [m.value for m in FileAssetStatus] == [
        "upload",
        "validating",
        "invalid",
        "valid",
        "stored",
    ]
    assert sql_in_list(MaterialType) == "'video', 'pdf_document'"
