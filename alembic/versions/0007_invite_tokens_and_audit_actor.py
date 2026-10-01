"""0007 invite tokens + the audit actor column

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29

Phase B, slice 4 (administrative user management) adds two things:

1. ``invite_tokens`` — server-side tracked, single-use teacher invitation
   tokens (SHA-256 digest only; the raw JWT lives in the email). Mirrors
   ``password_reset_tokens`` exactly, including the user cascade and both
   indexes, so ``app.models.invite_token`` and this DDL agree.
2. ``auth_events.actor_user_id`` — who performed the action, when that is
   not the subject of the event. Nullable with ``ON DELETE SET NULL``: a
   deleted administrator must not block audit rows from being read.

Constraint/index names follow the project convention
(``<table>_<column>_fkey`` / ``uq_<table>_<column>_key`` / ``<name>_idx``).

Downgrade removes the actor column and the invitation table; audit rows
already written keep their history (only the actor reference goes away).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. invitation tokens ------------------------------------------------
    op.create_table(
        "invite_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="invite_tokens_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="invite_tokens_user_id_fkey", ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_invite_tokens_token_hash_key"),
    )
    op.create_index("invite_tokens_user_id_idx", "invite_tokens", ["user_id"])
    op.create_index("invite_tokens_expires_at_idx", "invite_tokens", ["expires_at"])

    # --- 2. audit actor ------------------------------------------------------
    op.add_column(
        "auth_events",
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "auth_events_actor_user_id_fkey",
        "auth_events",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "auth_events_actor_user_id_idx", "auth_events", ["actor_user_id"]
    )


def downgrade() -> None:
    op.drop_index("auth_events_actor_user_id_idx", table_name="auth_events")
    op.drop_constraint(
        "auth_events_actor_user_id_fkey", "auth_events", type_="foreignkey"
    )
    op.drop_column("auth_events", "actor_user_id")
    op.drop_index("invite_tokens_expires_at_idx", table_name="invite_tokens")
    op.drop_index("invite_tokens_user_id_idx", table_name="invite_tokens")
    op.drop_table("invite_tokens")
