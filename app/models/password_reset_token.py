"""password_reset_tokens table — server-side tracked password reset tokens.

Design decision: like auth_sessions, password reset tokens are single-use
and server-side tracked. The raw JWT is never stored — only its SHA-256
digest. This prevents token reuse and enables explicit expiry/revocation.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class PasswordResetToken(TimestampMixin, Base):
    """One password reset token for one user account."""

    __tablename__ = "password_reset_tokens"

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
        Index("password_reset_tokens_user_id_idx", "user_id"),
        Index("password_reset_tokens_expires_at_idx", "expires_at"),
    )
