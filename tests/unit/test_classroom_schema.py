"""Unit tests: participation indexes (migration 0016) and classroom read schemas.

Slice 3B adds no tables and no columns — only the two indexes that turn
``class_attendance_segments`` into an enforced invariant, plus the read
payloads of the four historical endpoints. Model, migration and the
offline verification script must agree; the drift tests run
``scripts/verify_database.py``'s own schema descriptors.

The read schemas stay derived and credential-free: attendance verdicts
are recomputed (never stored as an editable flag), participants expose
identity + liveness only, and no payload carries an email address.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.schemas.online_class import (
    AttendanceSegmentRead,
    AttendanceStatusLiteral,
    ClassAttendanceRead,
    ClassMessageRead,
    ClassParticipantRead,
    MessageSenderRead,
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

TABLE = "class_attendance_segments"
NOW = datetime(2027, 3, 1, 10, 0, tzinfo=timezone.utc)


def _migration_0016() -> Path:
    matches = sorted((verify_database.MIGRATION_DIR).glob("0016_*.py"))
    assert len(matches) == 1, matches
    return matches[0]


def _tree() -> ast.Module:
    return ast.parse(_migration_0016().read_text(encoding="utf-8"))


def _function(name: str) -> ast.FunctionDef:
    return next(
        node
        for node in _tree().body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _index_calls(function: str) -> list[ast.Call]:
    calls = [
        node
        for node in ast.walk(_function(function))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) in {"create_index", "drop_index"}
    ]
    return sorted(calls, key=lambda node: node.lineno)


def _call_parts(call: ast.Call) -> tuple[str, str, str, list[str], dict[str, str]]:
    """``(operation, index_name, table, columns, kwargs)`` of one index DDL call."""
    name = call.args[0].value
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    table_name_kw = next(
        (kw for kw in call.keywords if kw.arg == "table_name"), None
    )
    table = (
        ast.literal_eval(table_name_kw.value)
        if table_name_kw is not None
        else call.args[1].value
    )
    columns: list[str] = []
    for arg in call.args[2:]:
        if isinstance(arg, ast.List):
            columns = list(ast.literal_eval(arg))
        else:
            columns.append(arg.value)
    return getattr(call.func, "attr"), name, table, columns, kwargs


def _module_constants() -> dict[str, object]:
    values: dict[str, object] = {}
    for node in _tree().body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            values[node.target.id] = ast.literal_eval(node.value)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def _migration_head() -> str:
    """The last revision declared across the migration files (in file order)."""
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


# --- model <-> migration -------------------------------------------------------------


def test_migration_0016_declares_the_two_participation_indexes() -> None:
    constants = _module_constants()
    assert constants["revision"] == "0016"
    assert constants["down_revision"] == "0015"

    create_open, drop_plain, create_unique = _index_calls("upgrade")
    assert [getattr(c.func, "attr") for c in (create_open, drop_plain, create_unique)] == [
        "create_index",
        "drop_index",
        "create_index",
    ]

    # 1. partial unique: one OPEN segment per student per class.
    _, name, table, columns, kwargs = _call_parts(create_open)
    assert name == "uq_class_attendance_segments_open_key"
    assert table == TABLE
    assert columns == ["student_id", "class_session_id"]
    assert kwargs["unique"] == "True"
    assert "left_at IS NULL" in kwargs["postgresql_where"]
    assert "left_at IS NULL" in kwargs["sqlite_where"]

    # 2. connection_id: plain lookup index becomes unique (drop-then-create).
    _, dropped, dropped_table, _, _ = _call_parts(drop_plain)
    assert dropped == "class_attendance_segments_connection_id_idx"
    assert dropped_table == TABLE
    _, name, table, columns, kwargs = _call_parts(create_unique)
    assert name == "uq_class_attendance_segments_connection_id_key"
    assert table == TABLE
    assert columns == ["connection_id"]
    assert kwargs["unique"] == "True"
    assert "where" not in kwargs  # plain unique, history is unrestricted


def test_migration_0016_downgrade_restores_the_0015_shape() -> None:
    drop_unique, create_plain, drop_open = _index_calls("downgrade")
    assert getattr(drop_unique.func, "attr") == "drop_index"
    assert _call_parts(drop_unique)[1] == "uq_class_attendance_segments_connection_id_key"
    assert getattr(create_plain.func, "attr") == "create_index"
    _, name, table, columns, kwargs = _call_parts(create_plain)
    assert name == "class_attendance_segments_connection_id_idx"
    assert table == TABLE
    assert columns == ["connection_id"]
    assert "unique" not in kwargs  # back to the plain index
    assert getattr(drop_open.func, "attr") == "drop_index"
    assert _call_parts(drop_open)[1] == "uq_class_attendance_segments_open_key"


def test_model_indexes_match_migration_0016_exactly() -> None:
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateIndex

    indexes = {index.name: index for index in Base.metadata.tables[TABLE].indexes}

    open_key = indexes["uq_class_attendance_segments_open_key"]
    assert open_key.unique is True
    assert [c.name for c in open_key.columns] == ["student_id", "class_session_id"]
    # The partial predicate must render on BOTH dialects the platform uses.
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        ddl = str(CreateIndex(open_key).compile(dialect=dialect))
        assert "UNIQUE" in ddl
        assert "WHERE left_at IS NULL" in ddl

    connection_key = indexes["uq_class_attendance_segments_connection_id_key"]
    assert connection_key.unique is True
    assert [c.name for c in connection_key.columns] == ["connection_id"]
    ddl = str(CreateIndex(connection_key).compile(dialect=sqlite.dialect()))
    assert "UNIQUE" in ddl and "WHERE" not in ddl

    # The 0015 plain lookup index is gone — a re-created unique took its place.
    assert "class_attendance_segments_connection_id_idx" not in indexes


def test_participation_indexes_have_no_model_migration_drift() -> None:
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )
    problems = verify_database.diff_schemas(
        {TABLE: metadata_schema[TABLE]},
        {TABLE: migration_schema[TABLE]},
        "models",
        "migrations",
    )
    assert problems == [], problems


def test_verification_script_expects_head_0016() -> None:
    assert verify_database.EXPECTED_REVISION == "0016"
    assert verify_database.EXPECTED_REVISION == _migration_head()


# --- read schemas --------------------------------------------------------------------


def test_attendance_verdict_is_derived_and_constrained_to_the_two_literals() -> None:
    assert get_args(AttendanceStatusLiteral) == ("attended", "not_attended")
    assert set(ClassAttendanceRead.model_fields) == {
        "student_id",
        "full_name",
        "online",
        "cumulative_seconds",
        "actual_seconds",
        "attendance_status",
        "segments",
    }
    # The draft's stored verdict names never made it into the contract.
    assert "scheduled_seconds" not in ClassAttendanceRead.model_fields
    assert "attended" not in ClassAttendanceRead.model_fields

    row = ClassAttendanceRead(
        student_id=uuid4(),
        full_name="Test Student",
        cumulative_seconds=1800,
        actual_seconds=3600,
        attendance_status="attended",
    )
    assert row.attendance_status == "attended"
    assert row.online is False  # default: not connected right now
    assert row.segments == []

    for verdict in ("absent", "present", "yes"):
        with pytest.raises(ValidationError):
            ClassAttendanceRead(
                student_id=uuid4(),
                full_name="Test Student",
                cumulative_seconds=0,
                actual_seconds=0,
                attendance_status=verdict,
            )


def test_participant_read_exposes_identity_and_liveness_only() -> None:
    assert set(ClassParticipantRead.model_fields) == {
        "student_id",
        "full_name",
        "online",
        "first_joined_at",
        "last_seen_at",
        "segment_count",
    }

    roster = ClassParticipantRead(
        student_id=uuid4(),
        full_name="Test Student",
        online=True,
        first_joined_at=NOW,
        last_seen_at=NOW,
        segment_count=2,
    )
    assert roster.online is True
    assert roster.segment_count == 2
    assert "email" not in ClassParticipantRead.model_fields


def test_classroom_payloads_carry_no_credential_material() -> None:
    segment = AttendanceSegmentRead(joined_at=NOW, last_seen_at=NOW)
    assert set(AttendanceSegmentRead.model_fields) == {
        "joined_at",
        "last_seen_at",
        "left_at",
    }
    assert segment.left_at is None  # an open segment needs no departure time

    assert set(MessageSenderRead.model_fields) == {"role", "display_name"}
    # The transcript payload is exactly these six presentation-safe fields —
    # never the DB's dedupe key (client_message_id), emails or connection ids.
    assert set(ClassMessageRead.model_fields) == {
        "message_id",
        "sequence",
        "sender",
        "body",
        "sent_at",
    }

    payloads = {
        "attendance": ClassAttendanceRead(
            student_id=uuid4(),
            full_name="Test Student",
            cumulative_seconds=600,
            actual_seconds=600,
            attendance_status="attended",
        ).model_dump(mode="json"),
        "participant": ClassParticipantRead(
            student_id=uuid4(),
            full_name="Test Student",
            online=False,
            first_joined_at=NOW,
            last_seen_at=NOW,
            segment_count=1,
        ).model_dump(mode="json"),
    }
    for name, payload in payloads.items():
        assert "token" not in payload, name
        assert "email" not in payload, name
        assert "password" not in payload, name
