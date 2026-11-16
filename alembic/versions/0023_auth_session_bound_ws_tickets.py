"""Bind classroom tickets to authentication sessions.

Revision ID: 0023
Revises: 0022
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("class_ws_tickets", sa.Column("auth_session_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("class_ws_tickets_auth_session_id_fkey", "class_ws_tickets", "auth_sessions", ["auth_session_id"], ["id"], ondelete="SET NULL")
    op.create_index("class_ws_tickets_auth_session_id_idx", "class_ws_tickets", ["auth_session_id"])


def downgrade() -> None:
    op.drop_index("class_ws_tickets_auth_session_id_idx", table_name="class_ws_tickets")
    op.drop_constraint("class_ws_tickets_auth_session_id_fkey", "class_ws_tickets", type_="foreignkey")
    op.drop_column("class_ws_tickets", "auth_session_id")
