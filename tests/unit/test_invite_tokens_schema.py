"""Unit tests: invitation tokens and the audit actor column (Phase B, slice 4).

Model ↔ migration agreement for the two objects slice 4 introduces, using
the offline drift comparator from ``scripts/verify_database.py`` — scoped to
this slice's objects, since the script's whole-schema comparison reports
pre-existing drift from migrations 0003/0004.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.core.database import Base
import app.models  # noqa: F401

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_spec = importlib.util.spec_from_file_location(
    "verify_database", BACKEND_DIR / "scripts" / "verify_database.py"
)
verify_database = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_database", verify_database)
_spec.loader.exec_module(verify_database)


def _invite_tokens():
    return Base.metadata.tables["invite_tokens"]


def test_invite_tokens_registered_with_expected_columns() -> None:
    assert "invite_tokens" in Base.metadata.tables
    columns = _invite_tokens().columns
    assert set(columns.keys()) == {
        "id",
        "user_id",
        "token_hash",
        "expires_at",
        "used_at",
        "created_at",
        "updated_at",
    }
    assert columns["token_hash"].nullable is False
    assert columns["expires_at"].nullable is False
    assert columns["used_at"].nullable is True
    assert columns["created_at"].nullable is False
    assert columns["updated_at"].nullable is False


def test_invite_tokens_keys_and_indexes_follow_the_convention() -> None:
    table = _invite_tokens()
    assert list(table.primary_key.columns.keys()) == ["id"]

    uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    assert [c.name for c in uniques if [col.name for col in c.columns] == ["token_hash"]] == [
        "uq_invite_tokens_token_hash_key"
    ]

    fks = {c.name: c for c in table.constraints if isinstance(c, ForeignKeyConstraint)}
    assert set(fks) == {"invite_tokens_user_id_fkey"}
    assert fks["invite_tokens_user_id_fkey"].elements[0].target_fullname == "users.id"

    assert {index.name for index in table.indexes} == {
        "invite_tokens_user_id_idx",
        "invite_tokens_expires_at_idx",
    }


def test_auth_events_gains_the_actor_column() -> None:
    columns = Base.metadata.tables["auth_events"].columns
    assert "actor_user_id" in columns
    assert columns["actor_user_id"].nullable is True
    actor_index = [
        index
        for index in Base.metadata.tables["auth_events"].indexes
        if index.name == "auth_events_actor_user_id_idx"
    ]
    assert len(actor_index) == 1
    assert [column.name for column in actor_index[0].columns] == ["actor_user_id"]


def test_verification_script_covers_invite_tokens_and_the_current_head() -> None:
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
    assert verify_database.EXPECTED_REVISION == head
    assert "invite_tokens" in verify_database.EXPECTED_TABLES


def test_truncation_list_covers_invite_tokens() -> None:
    import conftest as tests_conftest

    assert "invite_tokens" in tests_conftest.APPLICATION_TABLES


def test_slice_objects_have_no_model_migration_drift() -> None:
    """invite_tokens and the actor column match the parsed migration DDL."""
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )

    problems = verify_database.diff_schemas(
        {"invite_tokens": metadata_schema["invite_tokens"]},
        {"invite_tokens": migration_schema["invite_tokens"]},
        "models",
        "migrations",
    )
    assert problems == [], problems

    for label in ("actor_user_id",):
        assert (
            metadata_schema["auth_events"]["columns"][label]
            == migration_schema["auth_events"]["columns"][label]
        )
    assert (
        metadata_schema["auth_events"]["fks"]["auth_events_actor_user_id_fkey"]
        == migration_schema["auth_events"]["fks"]["auth_events_actor_user_id_fkey"]
    )
    assert (
        "auth_events_actor_user_id_idx"
        in metadata_schema["auth_events"]["indexes"]
        and "auth_events_actor_user_id_idx"
        in migration_schema["auth_events"]["indexes"]
    )


def test_slice_adds_no_new_drift_problems() -> None:
    """No problem line may mention this slice's tables/columns."""
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )
    problems = verify_database.diff_schemas(
        metadata_schema, migration_schema, "models", "migrations"
    )
    introduced = [
        p
        for p in problems
        if "invite_tokens" in p or "actor_user_id" in p
    ]
    assert introduced == [], introduced
