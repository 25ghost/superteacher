"""auth_sessions table — server-side revocable refresh sessions (Phase 5G).

Design decision (Step 7): refresh tokens are **revocable**, which requires
server-side state. A purely stateless refresh JWT could not be revoked on
logout, suspension, or role change. The ``auth_sessions`` row is that state:

- the *raw* refresh token is never stored — only its SHA-256 digest (the
  token is also signed, but the digest protects against a database read
  being replayed as a valid credential);
- ``revoked_at`` marks an explicitly logged-out / invalidated session —
  revoked sessions can never refresh again (Step 22/23);
- ``expires_at`` bounds the session's lifetime; expired sessions are dead
  even if their digest is still present;
- ``last_used_at`` supports rotation audits and stale-session cleanup later.

This is shared core infrastructure (Step 41): every SuperTeacher module
that authenticates users (teacher portal, parent portal, admin) reuses it —
it is not a Student-Registration-only feature.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class AuthSession(TimestampMixin, Base):
    """One revocable refresh session for one user account."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 hex digest of the refresh token's opaque secret component.
    # The signed JWT itself is never persisted.
    token_hash: Mapped[str] = mapped_column(nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user = relationship("User", back_populates="auth_sessions")

    __table_args__ = (
        Index("auth_sessions_user_id_idx", "user_id"),
        Index("auth_sessions_expires_at_idx", "expires_at"),
        Index("auth_sessions_user_revoked_idx", "user_id", "revoked_at"),
    )
