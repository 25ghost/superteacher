"""authentication: users.password_hash + revocable auth_sessions

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

Phase 5G — the smallest schema change that supports secure authentication
(Steps 2 and 7):

1. ``users.password_hash`` — a nullable VARCHAR(255) holding a PHC-format
   Argon2id string. Nullable because existing rows (there are none with
   passwords yet) must remain valid; an account without a hash simply
   cannot authenticate until one is set. Plaintext is never stored.

2. ``auth_sessions`` — server-side revocable refresh sessions. The raw
   refresh token is never stored; only its SHA-256 digest (``token_hash``,
   UNIQUE). Sessions expire (``expires_at``) and are revoked
   (``revoked_at``) on logout/invalidation. ``last_used_at`` supports
   rotation audits. Deleting a user removes their sessions (FK CASCADE).

Fully reversible via downgrade().
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- users.password_hash -------------------------------------------------
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(length=255), nullable=True),
    )

    # --- auth_sessions ---------------------------------------------------------
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="auth_sessions_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="auth_sessions_user_id_fkey", ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash_key"),
    )
    op.create_index("auth_sessions_user_id_idx", "auth_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("auth_sessions_user_id_idx", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_column("users", "password_hash")
