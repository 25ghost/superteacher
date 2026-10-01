"""0009 auth index drift fix (H1)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30

Migration 0003 created the audit/reset indexes under SQLAlchemy's default
``ix_<table>_<column>`` names while the models declare the project
convention ``<table>_<column>_idx`` — five indexes therefore existed under
two different names in the drift report (``password_reset_tokens_user_id``,
``password_reset_tokens_expires_at``, ``auth_events_user_id``,
``auth_events_event_type``, ``auth_events_created_at``). Migration 0002
created only one of the three indexes ``app.models.auth_session`` declares,
so ``auth_sessions_expires_at_idx`` and ``auth_sessions_user_revoked_idx``
were missing entirely.

This revision reconciles schema and models exactly:

1. drops the five differently-named ``ix_*`` indexes and recreates them
   under the model-declared names (same columns, same uniqueness);
2. creates the two missing ``auth_sessions`` indexes, including the
   composite ``(user_id, revoked_at)`` that serves the
   "revoke every active session for this user" lookup.

Downgrade restores the previous state byte-for-byte: the two new
``auth_sessions`` indexes are dropped and the five indexes are recreated
under their original ``ix_*`` names.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Index renames applied by upgrade(): (table, old ix_* name, model name,
# columns).  downgrade() reverses them.
#
#   password_reset_tokens  ix_password_reset_tokens_user_id   -> password_reset_tokens_user_id_idx    (user_id)
#   password_reset_tokens  ix_password_reset_tokens_expires_at -> password_reset_tokens_expires_at_idx (expires_at)
#   auth_events            ix_auth_events_user_id             -> auth_events_user_id_idx              (user_id)
#   auth_events            ix_auth_events_event_type          -> auth_events_event_type_idx           (event_type)
#   auth_events            ix_auth_events_created_at          -> auth_events_created_at_idx           (created_at)
#
# Indexes the models declare but no migration ever created:
#   auth_sessions_expires_at_idx (expires_at),
#   auth_sessions_user_revoked_idx (user_id, revoked_at)
#
# The DDL below is written out one literal ``op.*`` call per index rather
# than in a loop over a data table: ``scripts/verify_database.py`` reads
# migrations statically with ``ast`` and can only follow literal arguments.


def upgrade() -> None:
    # password_reset_tokens: ix_password_reset_tokens_user_id
    op.drop_index(
        "ix_password_reset_tokens_user_id", table_name="password_reset_tokens"
    )
    op.create_index(
        "password_reset_tokens_user_id_idx", "password_reset_tokens", ["user_id"]
    )

    # password_reset_tokens: ix_password_reset_tokens_expires_at
    op.drop_index(
        "ix_password_reset_tokens_expires_at", table_name="password_reset_tokens"
    )
    op.create_index(
        "password_reset_tokens_expires_at_idx",
        "password_reset_tokens",
        ["expires_at"],
    )

    # auth_events: ix_auth_events_user_id
    op.drop_index("ix_auth_events_user_id", table_name="auth_events")
    op.create_index("auth_events_user_id_idx", "auth_events", ["user_id"])

    # auth_events: ix_auth_events_event_type
    op.drop_index("ix_auth_events_event_type", table_name="auth_events")
    op.create_index(
        "auth_events_event_type_idx", "auth_events", ["event_type"]
    )

    # auth_events: ix_auth_events_created_at
    op.drop_index("ix_auth_events_created_at", table_name="auth_events")
    op.create_index("auth_events_created_at_idx", "auth_events", ["created_at"])

    # auth_sessions: the two indexes the models declare but 0002 never made.
    op.create_index(
        "auth_sessions_expires_at_idx", "auth_sessions", ["expires_at"]
    )
    op.create_index(
        "auth_sessions_user_revoked_idx",
        "auth_sessions",
        ["user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "auth_sessions_user_revoked_idx", table_name="auth_sessions"
    )
    op.drop_index("auth_sessions_expires_at_idx", table_name="auth_sessions")

    op.drop_index("auth_events_created_at_idx", table_name="auth_events")
    op.create_index("ix_auth_events_created_at", "auth_events", ["created_at"])

    op.drop_index("auth_events_event_type_idx", table_name="auth_events")
    op.create_index("ix_auth_events_event_type", "auth_events", ["event_type"])

    op.drop_index("auth_events_user_id_idx", table_name="auth_events")
    op.create_index("ix_auth_events_user_id", "auth_events", ["user_id"])

    op.drop_index(
        "password_reset_tokens_expires_at_idx",
        table_name="password_reset_tokens",
    )
    op.create_index(
        "ix_password_reset_tokens_expires_at",
        "password_reset_tokens",
        ["expires_at"],
    )

    op.drop_index(
        "password_reset_tokens_user_id_idx", table_name="password_reset_tokens"
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )
