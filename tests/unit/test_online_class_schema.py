"""Unit tests: the Phase 3 slice 3A online-class tables, migration 0015.

Slice 3A adds ``online_class_sessions`` plus its three classroom children
(``class_messages``, ``class_attendance_segments``, ``class_ws_tickets``).
Model, migration and the offline verification script must agree — the
drift tests below run ``scripts/verify_database.py``'s own schema
descriptors. The Pydantic request schemas enforce the Phase 3 boundary:
timezone-aware windows, no role-shaped fields, no empty amendment.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.enums import OnlineClassStatus, sql_in_list
from app.schemas.online_class import (
    ClassMessageRead,
    MessageSenderRead,
    OnlineClassSessionCreate,
    OnlineClassSessionRead,
    OnlineClassSessionUpdate,
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

_NEW_TABLES = (
    "online_class_sessions",
    "class_messages",
    "class_attendance_segments",
    "class_ws_tickets",
)

START = datetime(2027, 3, 1, 10, 0, tzinfo=timezone.utc)
END = datetime(2027, 3, 1, 11, 0, tzinfo=timezone.utc)


def _table(name: str):
    return Base.metadata.tables[name]


# --- model <-> migration -------------------------------------------------------------


def test_class_tables_are_registered_with_expected_columns() -> None:
    assert set(_NEW_TABLES) <= set(Base.metadata.tables)
    assert set(_table("online_class_sessions").columns.keys()) == {
        "id",
        "teaching_offering_id",
        "lesson_id",
        "scheduled_start_at",
        "scheduled_end_at",
        "actual_started_at",
        "actual_ended_at",
        "status",
        "created_at",
        "updated_at",
    }
    sessions = _table("online_class_sessions")
    assert sessions.columns["teaching_offering_id"].nullable is False
    assert sessions.columns["lesson_id"].nullable is True  # a class may span lessons
    assert sessions.columns["status"].nullable is False
    assert sessions.columns["status"].type.length == 32

    assert set(_table("class_messages").columns.keys()) == {
        "id",
        "class_session_id",
        "sender_user_id",
        "client_message_id",
        "body",
        "sequence",
        "created_at",
        "updated_at",
    }
    messages = _table("class_messages")
    assert messages.columns["client_message_id"].type.length == 64
    assert messages.columns["body"].type.length == 2000

    assert set(_table("class_attendance_segments").columns.keys()) == {
        "id",
        "class_session_id",
        "student_id",
        "connection_id",
        "joined_at",
        "last_seen_at",
        "left_at",
        "created_at",
        "updated_at",
    }
    segments = _table("class_attendance_segments")
    assert segments.columns["left_at"].nullable is True  # NULL = still connected
    assert segments.columns["connection_id"].type.length == 64

    assert set(_table("class_ws_tickets").columns.keys()) == {
        "id",
        "class_session_id",
        "user_id",
        "token_hash",
        "expires_at",
        "used_at",
        "created_at",
        "updated_at",
    }
    tickets = _table("class_ws_tickets")
    assert tickets.columns["token_hash"].nullable is False
    assert tickets.columns["used_at"].nullable is True  # NULL = still unused


def test_class_foreign_keys_follow_the_naming_convention() -> None:
    expectations = {
        "online_class_sessions": {
            "online_class_sessions_teaching_offering_id_fkey": "teaching_offerings.id",
            "online_class_sessions_lesson_id_fkey": "lessons.id",
        },
        "class_messages": {
            "class_messages_class_session_id_fkey": "online_class_sessions.id",
            "class_messages_sender_user_id_fkey": "users.id",
        },
        "class_attendance_segments": {
            "class_attendance_segments_class_session_id_fkey": "online_class_sessions.id",
            "class_attendance_segments_student_id_fkey": "students.id",
        },
        "class_ws_tickets": {
            "class_ws_tickets_class_session_id_fkey": "online_class_sessions.id",
            "class_ws_tickets_user_id_fkey": "users.id",
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


def test_classroom_children_cascade_with_their_session() -> None:
    """A deleted class takes its messages, segments and tickets with it."""
    for table_name in ("class_messages", "class_attendance_segments", "class_ws_tickets"):
        fks = [
            c
            for c in _table(table_name).constraints
            if isinstance(c, ForeignKeyConstraint)
        ]
        session_fk = next(
            fk for fk in fks if fk.elements[0].target_fullname == "online_class_sessions.id"
        )
        assert "CASCADE" in str(session_fk.ondelete).upper(), table_name


def test_status_vocabulary_is_the_documented_state_machine() -> None:
    values = tuple(member.value for member in OnlineClassStatus)
    assert values == ("scheduled", "live", "ended", "cancelled")

    sessions = _table("online_class_sessions")
    status_check = next(
        c
        for c in sessions.constraints
        if isinstance(c, CheckConstraint) and c.name == "online_class_sessions_status_check"
    )
    assert set(verify_database._check_literals(str(status_check.sqltext))) == set(values)

    schedule_check = next(
        c
        for c in sessions.constraints
        if isinstance(c, CheckConstraint) and c.name == "online_class_sessions_schedule_check"
    )
    text = str(schedule_check.sqltext)
    assert "scheduled_end_at" in text and ">" in text and "scheduled_start_at" in text
    assert f"status IN ({sql_in_list(OnlineClassStatus)})" == str(status_check.sqltext)


def test_dedup_and_ordering_of_messages_are_database_invariants() -> None:
    """Sequence uniqueness and the retry key live in the schema, not in code."""
    messages = _table("class_messages")
    uniques = {
        i.name: i for i in messages.indexes if i.unique
    } | {
        c.name: c for c in messages.constraints if isinstance(c, UniqueConstraint)
    }
    sequence_key = uniques["uq_class_messages_class_session_id_sequence_key"]
    assert [c.name for c in sequence_key.columns] == ["class_session_id", "sequence"]
    retry_key = uniques["uq_class_messages_class_sender_client_key"]
    assert [c.name for c in retry_key.columns] == [
        "class_session_id",
        "sender_user_id",
        "client_message_id",
    ]

    sequence_check = next(
        c
        for c in messages.constraints
        if isinstance(c, CheckConstraint) and c.name == "class_messages_sequence_check"
    )
    assert str(sequence_check.sqltext) == "sequence >= 1"


def test_attendance_segments_never_close_before_they_open() -> None:
    segments = _table("class_attendance_segments")
    check = next(
        c
        for c in segments.constraints
        if isinstance(c, CheckConstraint)
        and c.name == "class_attendance_segments_date_order_check"
    )
    text = str(check.sqltext)
    assert "left_at" in text and "joined_at" in text


def test_ws_tickets_stay_single_use_through_a_unique_digest() -> None:
    tickets = _table("class_ws_tickets")
    unique = next(
        c for c in tickets.constraints if isinstance(c, UniqueConstraint)
    )
    assert unique.name == "uq_class_ws_tickets_token_hash_key"
    assert [c.name for c in unique.columns] == ["token_hash"]

    # Lookup paths the handshake needs: one index each.
    index_names = {i.name for i in tickets.indexes}
    assert {
        "class_ws_tickets_class_session_id_idx",
        "class_ws_tickets_user_id_idx",
        "class_ws_tickets_expires_at_idx",
    } <= index_names


def test_verification_script_covers_the_new_tables_and_head() -> None:
    assert verify_database.EXPECTED_REVISION == "0016"
    assert verify_database.EXPECTED_REVISION == _migration_head()
    for table in _NEW_TABLES:
        assert table in verify_database.EXPECTED_TABLES
        assert table in verify_database.LATER_MIGRATION_TABLES


def test_truncation_list_covers_the_new_tables() -> None:
    """Integration cleanup must truncate the new tables too, children first."""
    import conftest as tests_conftest

    for table in _NEW_TABLES:
        assert table in tests_conftest.APPLICATION_TABLES
    order = tests_conftest.APPLICATION_TABLES
    assert order.index("class_messages") < order.index("online_class_sessions")
    assert order.index("class_attendance_segments") < order.index("online_class_sessions")
    assert order.index("class_ws_tickets") < order.index("online_class_sessions")


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


def test_new_tables_have_no_model_migration_drift() -> None:
    """The objects this slice introduces must match migration 0015 exactly."""
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
    introduced = [p for p in problems if any(name in p for name in _NEW_TABLES)]
    assert introduced == [], introduced


# --- request schemas ------------------------------------------------------------------


def test_create_requires_timezone_aware_windows() -> None:
    naive_start = datetime(2027, 3, 1, 10, 0)
    naive_end = datetime(2027, 3, 1, 11, 0)

    with pytest.raises(ValidationError) as excinfo:
        OnlineClassSessionCreate(scheduled_start_at=naive_start, scheduled_end_at=END)
    assert "must include a timezone offset" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        OnlineClassSessionCreate(scheduled_start_at=START, scheduled_end_at=naive_end)
    assert "must include a timezone offset" in str(excinfo.value)


def test_create_refuses_an_inverted_or_empty_window() -> None:
    with pytest.raises(ValidationError) as excinfo:
        OnlineClassSessionCreate(scheduled_start_at=END, scheduled_end_at=START)
    assert "scheduled_end_at must be after scheduled_start_at" in str(excinfo.value)

    # Equal bounds are not a class either.
    with pytest.raises(ValidationError):
        OnlineClassSessionCreate(scheduled_start_at=START, scheduled_end_at=START)


def test_create_forbids_role_shaped_and_unknown_fields() -> None:
    for key in ("teaching_offering_id", "teacher_id", "status", "class_id"):
        with pytest.raises(ValidationError) as excinfo:
            OnlineClassSessionCreate(
                scheduled_start_at=START,
                scheduled_end_at=END,
                **{key: uuid4()},
            )
        assert "Extra inputs are not permitted" in str(excinfo.value)


def test_update_never_accepts_an_empty_or_null_window() -> None:
    with pytest.raises(ValidationError) as excinfo:
        OnlineClassSessionUpdate()
    assert "supply at least one of" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        OnlineClassSessionUpdate(scheduled_start_at=None)
    assert "must not be null" in str(excinfo.value)

    # An explicit null LESSON is legal - it clears the optional link.
    payload = OnlineClassSessionUpdate(lesson_id=None)
    assert payload.model_fields_set == {"lesson_id"}


def test_update_uses_model_fields_set_so_a_recheck_can_stay_aware() -> None:
    payload = OnlineClassSessionUpdate(scheduled_end_at=END)
    assert payload.model_fields_set == {"scheduled_end_at"}
    assert payload.scheduled_start_at is None  # "not supplied", not "cleared"


def test_read_shapes_expose_no_credential_material() -> None:
    read = OnlineClassSessionRead(
        class_id=uuid4(),
        teaching_offering_id=uuid4(),
        scheduled_start_at=START,
        scheduled_end_at=END,
        status="scheduled",
        created_at=START,
        updated_at=START,
    )
    assert set(read.model_dump()) == {
        "class_id",
        "teaching_offering_id",
        "lesson_id",
        "scheduled_start_at",
        "scheduled_end_at",
        "actual_started_at",
        "actual_ended_at",
        "status",
        "created_at",
        "updated_at",
    }
    assert "token" not in read.model_dump()
    assert set(MessageSenderRead.model_fields) == {"role", "display_name"}
    assert "email" not in ClassMessageRead.model_fields
