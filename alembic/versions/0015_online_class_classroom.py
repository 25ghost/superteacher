"""0015 online classes and the text-only classroom (Phase 3)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08

Phase 3 adds a small, reliable, text-only live classroom owned by
SuperTeacher — no audio, no video, no WebRTC, no external meeting
provider. Four tables:

1. ``online_class_sessions`` — one scheduled/live class inside exactly
   ONE existing ``teaching_offerings`` row. The teacher is derived from
   the offering (never stored as an independently mutable relationship)
   and the ``lesson_id`` FK is deliberately nullable: a live class may
   cover several lessons or be a revision session. Status vocabulary
   ``scheduled → live → ended`` and ``scheduled → cancelled`` (both
   terminal, never reopened) is guarded by
   ``online_class_sessions_status_check``; ``scheduled_end_at >
   scheduled_start_at`` by ``online_class_sessions_schedule_check``.
   Overlapping scheduled/live sessions per offering are refused by the
   service under a row lock on the offering — no recurrence, no calendars.
2. ``class_messages`` — durable messages. Per-class ``sequence`` is
   unique (reconnect/recovery by sequence can never be ambiguous) and
   ``(class_session_id, sender_user_id, client_message_id)`` is unique so
   client retries are idempotent. ``sequence >= 1`` is a CHECK. Nothing
   ever updates a message, so ``updated_at`` tracks ``created_at`` (the
   platform-wide timestamp invariant), and messages are written
   *before* they are broadcast — PostgreSQL stays the source of
   truth, the WebSocket is only transport.
3. ``class_attendance_segments`` — participation intervals, never a
   single mutable ``attended`` boolean: a disconnect plus a reconnect
   becomes two rows so cumulative connected time (the future >= 50% of
   scheduled duration rule, and later teacher-rating eligibility) is
   derived from facts, not flags. Open segments are finalized when the
   connection closes or the class ends.
4. ``class_ws_tickets`` — short-lived, single-use WebSocket access
   tickets reusing the existing invite/reset token contract: SHA-256
   digest only, ``expires_at``, ``used_at``, one user, one class. This
   is *not* a second authentication system — the HTTP endpoint
   authorizes participation first, then mints the ticket.

All identifiers stay well inside PostgreSQL's 63-byte limit. Downgrade
drops the four tables in reverse dependency order; the classroom domain
is Phase 3 data with no data-preserving step.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. the class session (owns everything below) -------------------------
    op.create_table(
        "online_class_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teaching_offering_id", sa.Uuid(), nullable=False),
        sa.Column("lesson_id", sa.Uuid(), nullable=True),
        sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="online_class_sessions_pkey"),
        sa.ForeignKeyConstraint(
            ["teaching_offering_id"],
            ["teaching_offerings.id"],
            name="online_class_sessions_teaching_offering_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["lesson_id"],
            ["lessons.id"],
            name="online_class_sessions_lesson_id_fkey",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'live', 'ended', 'cancelled')",
            name="online_class_sessions_status_check",
        ),
        sa.CheckConstraint(
            "scheduled_end_at > scheduled_start_at",
            name="online_class_sessions_schedule_check",
        ),
    )
    op.create_index(
        "online_class_sessions_teaching_offering_id_idx",
        "online_class_sessions",
        ["teaching_offering_id"],
    )
    op.create_index(
        "online_class_sessions_lesson_id_idx",
        "online_class_sessions",
        ["lesson_id"],
    )

    # --- 2. durable class messages --------------------------------------------
    op.create_table(
        "class_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("class_session_id", sa.Uuid(), nullable=False),
        sa.Column("sender_user_id", sa.Uuid(), nullable=False),
        sa.Column("client_message_id", sa.String(length=64), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="class_messages_pkey"),
        sa.ForeignKeyConstraint(
            ["class_session_id"],
            ["online_class_sessions.id"],
            name="class_messages_class_session_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_user_id"],
            ["users.id"],
            name="class_messages_sender_user_id_fkey",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="class_messages_sequence_check",
        ),
    )
    op.create_index(
        "uq_class_messages_class_session_id_sequence_key",
        "class_messages",
        ["class_session_id", "sequence"],
        unique=True,
    )
    op.create_index(
        "uq_class_messages_class_sender_client_key",
        "class_messages",
        ["class_session_id", "sender_user_id", "client_message_id"],
        unique=True,
    )
    op.create_index(
        "class_messages_class_session_id_idx", "class_messages", ["class_session_id"]
    )
    op.create_index(
        "class_messages_sender_user_id_idx", "class_messages", ["sender_user_id"]
    )

    # --- 3. participation segments (attendance source of truth) ---------------
    op.create_table(
        "class_attendance_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("class_session_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="class_attendance_segments_pkey"),
        sa.ForeignKeyConstraint(
            ["class_session_id"],
            ["online_class_sessions.id"],
            name="class_attendance_segments_class_session_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["students.id"],
            name="class_attendance_segments_student_id_fkey",
        ),
        sa.CheckConstraint(
            "left_at IS NULL OR left_at >= joined_at",
            name="class_attendance_segments_date_order_check",
        ),
    )
    op.create_index(
        "class_attendance_segments_class_session_id_idx",
        "class_attendance_segments",
        ["class_session_id"],
    )
    op.create_index(
        "class_attendance_segments_student_id_idx",
        "class_attendance_segments",
        ["student_id"],
    )
    op.create_index(
        "class_attendance_segments_connection_id_idx",
        "class_attendance_segments",
        ["connection_id"],
    )

    # --- 4. single-use WebSocket access tickets --------------------------------
    op.create_table(
        "class_ws_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("class_session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="class_ws_tickets_pkey"),
        sa.ForeignKeyConstraint(
            ["class_session_id"],
            ["online_class_sessions.id"],
            name="class_ws_tickets_class_session_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="class_ws_tickets_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_class_ws_tickets_token_hash_key"),
    )
    op.create_index(
        "class_ws_tickets_class_session_id_idx",
        "class_ws_tickets",
        ["class_session_id"],
    )
    op.create_index("class_ws_tickets_user_id_idx", "class_ws_tickets", ["user_id"])
    op.create_index("class_ws_tickets_expires_at_idx", "class_ws_tickets", ["expires_at"])


def downgrade() -> None:
    # Reverse dependency order: leaf tables first, sessions last.
    op.drop_index("class_ws_tickets_expires_at_idx", table_name="class_ws_tickets")
    op.drop_index("class_ws_tickets_user_id_idx", table_name="class_ws_tickets")
    op.drop_index(
        "class_ws_tickets_class_session_id_idx", table_name="class_ws_tickets"
    )
    op.drop_table("class_ws_tickets")

    op.drop_index(
        "class_attendance_segments_connection_id_idx",
        table_name="class_attendance_segments",
    )
    op.drop_index(
        "class_attendance_segments_student_id_idx",
        table_name="class_attendance_segments",
    )
    op.drop_index(
        "class_attendance_segments_class_session_id_idx",
        table_name="class_attendance_segments",
    )
    op.drop_table("class_attendance_segments")

    op.drop_index("class_messages_sender_user_id_idx", table_name="class_messages")
    op.drop_index("class_messages_class_session_id_idx", table_name="class_messages")
    op.drop_index(
        "uq_class_messages_class_sender_client_key", table_name="class_messages"
    )
    op.drop_index(
        "uq_class_messages_class_session_id_sequence_key", table_name="class_messages"
    )
    op.drop_table("class_messages")

    op.drop_index(
        "online_class_sessions_lesson_id_idx", table_name="online_class_sessions"
    )
    op.drop_index(
        "online_class_sessions_teaching_offering_id_idx",
        table_name="online_class_sessions",
    )
    op.drop_table("online_class_sessions")
