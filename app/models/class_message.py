"""class_messages table — durable text messages of ONE online class.

Every message a participant sends during a ``live`` class session is
persisted before it is broadcast: the WebSocket is transport, this table is
the record. Two database guarantees back the classroom's ordering and
retry contract:

- ``uq_class_messages_class_session_id_sequence_key`` — per-class
  ``sequence`` numbers are unique, so reconnect/recovery by sequence can
  never be ambiguous;
- ``uq_class_messages_class_sender_client_key`` — a retried
  ``client_message_id`` from the same sender cannot create a second row,
  so client-side retries are idempotent.

The ``sequence`` is allocated under a row lock on the owning
``online_class_sessions`` row, which also makes "still live?" and ordering
one atomic decision. Nothing in the codebase updates a message after it is
written, so ``updated_at`` always equals ``created_at`` — the column pair
exists only so the platform-wide "every table with created_at also has
updated_at" invariant holds.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class ClassMessage(TimestampMixin, Base):
    """One chat message sent by one participant of one online class."""

    __tablename__ = "class_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    class_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("online_class_sessions.id", ondelete="CASCADE"), nullable=False
    )
    sender_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    # Client-generated id: the retry key, never authoritative.
    client_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(String(2000), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        Index(
            "uq_class_messages_class_session_id_sequence_key",
            "class_session_id",
            "sequence",
            unique=True,
        ),
        Index(
            "uq_class_messages_class_sender_client_key",
            "class_session_id",
            "sender_user_id",
            "client_message_id",
            unique=True,
        ),
        CheckConstraint(
            "sequence >= 1",
            name="class_messages_sequence_check",
        ),
        Index("class_messages_class_session_id_idx", "class_session_id"),
        Index("class_messages_sender_user_id_idx", "sender_user_id"),
    )

    class_session = relationship("OnlineClassSession")
    sender = relationship("User")
