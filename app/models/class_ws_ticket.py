"""class_ws_tickets table — short-lived, single-use classroom access tickets.

Browsers cannot conveniently attach an ``Authorization`` header to a
WebSocket handshake, so instead of a second authentication system the
existing JWT conventions are reused: the HTTP endpoint authorizes class
participation, then mints a ticket JWT (``typ="ws_ticket"``, claims
``sub`` + ``cls``) and stores only its SHA-256 digest here — the same
single-use pattern as ``invite_tokens`` and ``password_reset_tokens``.

The row is the authority: one user, one class session, one short expiry,
``used_at`` set by the handshake. Consuming a ticket twice, or against a
different class than the one it was minted for, fails. The row cascades on
user deletion (an unreachable ticket dies with its account).
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class ClassWsTicket(TimestampMixin, Base):
    """One short-lived, single-use WebSocket ticket for one user + class."""

    __tablename__ = "class_ws_tickets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    class_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("online_class_sessions.id", ondelete="CASCADE"), nullable=False
    )
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
        Index("class_ws_tickets_class_session_id_idx", "class_session_id"),
        Index("class_ws_tickets_user_id_idx", "user_id"),
        Index("class_ws_tickets_expires_at_idx", "expires_at"),
    )

    class_session = relationship("OnlineClassSession")
    user = relationship("User")
