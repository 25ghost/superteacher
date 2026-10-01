"""0008 users lockout columns

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29

Phase B, slice 7 (login lockout) adds two columns to ``users``:

1. ``failed_login_count`` — consecutive refused login attempts; reset to 0
   on a successful login. NOT NULL with a zero default so the counter can
   be incremented without a NULL check.
2. ``locked_until`` — the instant until which the account may not
   authenticate (``failed_login_count`` reaches LOGIN_LOCKOUT_THRESHOLD).
   Nullable (NULL = not locked), timezone-aware like every other
   timestamp in the schema.

Both are modelled in ``app.models.user``; the drift script picks the
columns up through ``op.add_column``.

Downgrade drops the columns: a lockout is operational state, never
history, so losing it on a rollback is safe (the account simply gets a
fresh budget).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
