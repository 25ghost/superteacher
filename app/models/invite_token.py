"""invite_tokens table — server-side tracked teacher invitation tokens.

Same contract as ``password_reset_tokens``: the raw JWT is never stored,
only its SHA-256 digest, so the row supports single-use enforcement and
explicit expiry/revocation. A token is issued when an administrator creates
a teacher account (or re-sends the invitation) and consumed by
``POST /auth/accept-invite``.

``user_id`` cascades on user deletion — an invite can outlive no one.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class InviteToken(TimestampMixin, Base):
    """One invitation token for one account."""

    __tablename__ = "invite_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("invite_tokens_user_id_idx", "user_id"),
        Index("invite_tokens_expires_at_idx", "expires_at"),
    )
