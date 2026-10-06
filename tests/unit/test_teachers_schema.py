"""Unit tests: the ``teachers`` table, the ``pending`` status, migration 0006.

Slice 3 introduces the teacher profile row (1:1 with ``users``) and the
``pending`` account status used by the invite flow. Phase 1 adds the
``verification_status`` column (teacher marketplace vetting) — the columns
assertion below covers it, and the drift tests prove the model still
matches the migration DDL column by column, constraint by constraint.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.enums import UserStatus

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_spec = importlib.util.spec_from_file_location(
    "verify_database", BACKEND_DIR / "scripts" / "verify_database.py"
)
verify_database = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_database", verify_database)
_spec.loader.exec_module(verify_database)


def _teachers():
    return Base.metadata.tables["teachers"]


def test_teachers_table_registered_with_expected_columns() -> None:
    assert "teachers" in Base.metadata.tables
    columns = _teachers().columns
    assert set(columns.keys()) == {
        "id",
        "user_id",
        "full_name",
        "phone",
        "school_id",
        "subject",
        "verification_status",
        "created_at",
        "updated_at",
    }
    assert columns["full_name"].nullable is False
    assert columns["full_name"].type.length == 200
    assert columns["phone"].nullable is True
    assert columns["phone"].type.length == 32
    assert columns["subject"].nullable is True
    assert columns["subject"].type.length == 120
    assert columns["user_id"].nullable is False
    assert columns["school_id"].nullable is True
    # Phase 1: vetting axis — required, short enum code, never nullable.
    assert columns["verification_status"].nullable is False
    assert columns["verification_status"].type.length == 32
    # Both timestamps present and required (verify_database checks the pair).
    assert columns["created_at"].nullable is False
    assert columns["updated_at"].nullable is False


def test_teachers_keys_and_foreign_keys_follow_the_naming_convention() -> None:
    table = _teachers()
    assert list(table.primary_key.columns.keys()) == ["id"]

    unique = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    user_unique = [c for c in unique if [col.name for col in c.columns] == ["user_id"]]
    assert [c.name for c in user_unique] == ["uq_teachers_user_id_key"]

    fks = {
        c.name: c
        for c in table.constraints
        if isinstance(c, ForeignKeyConstraint)
    }
    assert set(fks) == {"teachers_user_id_fkey", "teachers_school_id_fkey"}
    assert fks["teachers_user_id_fkey"].elements[0].target_fullname == "users.id"
    assert fks["teachers_school_id_fkey"].elements[0].target_fullname == "schools.id"


def test_user_status_vocabulary_gains_pending() -> None:
    assert UserStatus.PENDING.value == "pending"
    users = Base.metadata.tables["users"]
    check = next(
        c
        for c in users.constraints
        if isinstance(c, CheckConstraint) and c.name == "users_status_check"
    )
    stored = sorted(re.findall(r"'([^']+)'", str(check.sqltext)))
    assert stored == sorted(member.value for member in UserStatus)


def _migration_head() -> str:
    """The last revision declared across the migration files (in file order).

    Handles both declaration styles in this repo: ``revision: str = "…"``
    (0001, 0002, 0005+) and ``revision = "…"`` (0003, 0004).
    """
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


def test_verification_script_covers_teachers_and_the_current_head() -> None:
    assert verify_database.EXPECTED_REVISION == _migration_head()
    assert "teachers" in verify_database.EXPECTED_TABLES


def test_teachers_and_status_check_have_no_model_migration_drift() -> None:
    """The objects this slice introduces must match the migration exactly.

    The script's whole-schema comparison is not used here: it reports
    pre-existing drift for tables introduced by migrations 0003/0004 that
    is out of scope for this slice. This asserts the slice's own contract —
    ``teachers`` and the swapped ``users_status_check`` agree between the
    ORM metadata and the parsed migration DDL.
    """
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )

    problems = verify_database.diff_schemas(
        {"teachers": metadata_schema["teachers"]},
        {"teachers": migration_schema["teachers"]},
        "models",
        "migrations",
    )
    assert problems == [], problems

    assert verify_database._checks_match(
        metadata_schema["users"]["checks"]["users_status_check"],
        migration_schema["users"]["checks"]["users_status_check"],
    )


def test_slice_adds_no_new_drift_problems() -> None:
    """The comparison may not gain a single problem mentioning teachers/status."""
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )
    problems = verify_database.diff_schemas(
        metadata_schema, migration_schema, "models", "migrations"
    )
    introduced = [
        p for p in problems if "teachers" in p or "users_status_check" in p
    ]
    assert introduced == [], introduced


def test_truncation_list_covers_teachers() -> None:
    """Integration cleanup must truncate the new table too."""
    import conftest as tests_conftest

    assert "teachers" in tests_conftest.APPLICATION_TABLES
