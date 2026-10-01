"""0006 teachers profile + the ``pending`` account status

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29

Phase B (teacher onboarding) adds two things:

1. The ``teachers`` profile table — one row per teacher account, 1:1 with
   ``users`` (unique ``user_id``), holding the profile an administrator
   supplies when creating the account. Columns and constraints mirror
   ``app.models.teacher.Teacher`` exactly (verified offline by
   ``scripts/verify_database.py`` and the unit tests).
2. ``pending`` joins the ``users_status_check`` vocabulary. An account
   created by an administrator starts ``pending``: it exists, but the
   status gate refuses to authenticate it until the invite is accepted
   (``POST /auth/accept-invite``) or an administrator activates it.

The constraint swap is a drop + recreate (same pattern as 0005); no rows
need normalizing because ``pending`` is purely additive.

Downgrade drops the table and restores the three-value status vocabulary —
accounts left in ``pending`` would then violate the CHECK, so they are
disabled first (least privilege: a downgraded system must not keep an
unauthenticated half-provisioned account around).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Literal CHECK texts (like 0005) so offline verification can compare them
# with the ORM-derived constraint from app.models.enums.UserStatus.
_UPGRADE_CHECK = "status IN ('active', 'pending', 'suspended', 'disabled')"
_DOWNGRADE_CHECK = "status IN ('active', 'suspended', 'disabled')"


def upgrade() -> None:
    # --- 1. teachers profile ------------------------------------------------
    op.create_table(
        "teachers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("school_id", sa.Uuid(), nullable=True),
        sa.Column("subject", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teachers_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="teachers_user_id_fkey"),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], name="teachers_school_id_fkey"),
        sa.UniqueConstraint("user_id", name="uq_teachers_user_id_key"),
    )

    # --- 2. status vocabulary swap ------------------------------------------
    op.drop_constraint("users_status_check", "users", type_="check")
    op.create_check_constraint("users_status_check", "users", _UPGRADE_CHECK)


def downgrade() -> None:
    # A 'pending' account cannot exist under the old vocabulary: disable those
    # accounts instead of failing the downgrade (least privilege).
    op.execute("UPDATE users SET status = 'disabled' WHERE status = 'pending'")
    op.drop_constraint("users_status_check", "users", type_="check")
    op.create_check_constraint("users_status_check", "users", _DOWNGRADE_CHECK)
    op.drop_table("teachers")
