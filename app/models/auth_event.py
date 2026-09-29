"""auth_events table — audit trail for authentication events.

Logs every significant authentication event: login, logout, password
change, password reset, token refresh, and account deactivation. Supports
security auditing, incident investigation, and compliance requirements.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class AuthEvent(TimestampMixin, Base):
    """One authentication event for one user."""

    __tablename__ = "auth_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("auth_events_user_id_idx", "user_id"),
        Index("auth_events_event_type_idx", "event_type"),
        Index("auth_events_created_at_idx", "created_at"),
    )
