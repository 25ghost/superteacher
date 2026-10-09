"""0016 classroom participation indexes (Phase 3, slice 3B)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-08

Slice 3B turns ``class_attendance_segments`` into an enforced invariant
instead of a convention. Two indexes, no new tables and no new columns:

1. ``uq_class_attendance_segments_open_key`` — a PARTIAL unique index on
   ``(student_id, class_session_id) WHERE left_at IS NULL``: at most one
   OPEN (still-connected) segment per student per class. The service takes
   the class row lock before writing, so this index is the backstop that
   makes a duplicate-join race impossible even if two connections interleave
   — PostgreSQL rejects the second insert outright. It is partial on
   purpose: finished history must never collide, otherwise a student could
   only ever have ONE lifetime segment and reconnects would be unrecorded.
2. ``uq_class_attendance_segments_connection_id_key`` — the plain lookup
   index on ``connection_id`` (migration 0015) becomes unique: one
   WebSocket connection produces exactly one segment, so a reconnect can
   never be mistaken for the original connection. Same columns, so every
   "which segment belongs to this connection" query keeps its index.

Both indexes follow the house pattern (``postgresql_where`` +
``sqlite_where``) so the scratch-SQLite metadata smoke test and the
PostgreSQL live check see the same DDL. Downgrade restores 0015
byte-for-byte: the partial index is dropped and the connection index goes
back to its plain, non-unique form.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. at most one OPEN segment per student per class (partial unique).
    op.create_index(
        "uq_class_attendance_segments_open_key",
        "class_attendance_segments",
        ["student_id", "class_session_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL"),
        sqlite_where=sa.text("left_at IS NULL"),
    )

    # 2. connection_id: plain lookup index -> unique (one segment per
    #    connection). Written as drop-then-create so the name is a literal
    #    scripts/verify_database.py can follow with its static ast scan.
    op.drop_index(
        "class_attendance_segments_connection_id_idx",
        table_name="class_attendance_segments",
    )
    op.create_index(
        "uq_class_attendance_segments_connection_id_key",
        "class_attendance_segments",
        ["connection_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_class_attendance_segments_connection_id_key",
        table_name="class_attendance_segments",
    )
    op.create_index(
        "class_attendance_segments_connection_id_idx",
        "class_attendance_segments",
        ["connection_id"],
    )

    op.drop_index(
        "uq_class_attendance_segments_open_key",
        table_name="class_attendance_segments",
    )
