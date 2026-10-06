"""lessons table — one teachable step beneath a topic.

A lesson is the teacher's own unit of delivery inside ONE topic of ONE
offering. It stays inside that offering's educational context through the
topic; it never carries a catalog identity of its own.

``display_order`` follows the same stable-sequence rule as topics: unique
within the topic (``uq_lessons_topic_display_order_key``), >= 1. Deleting
a topic removes its lessons — the FK is ``ON DELETE CASCADE`` at the
database and ``cascade="all, delete-orphan"`` on the ORM relationship, so
a curriculum section cannot lose its children by accident.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class Lesson(TimestampMixin, Base):
    """One teacher-authored lesson inside one of their topics."""

    __tablename__ = "lessons"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default=text("1")
    )

    __table_args__ = (
        # Declared as Index(unique=True), matching the topics table and
        # Phase 1's own uniqueness indexes, so migration and ORM agree.
        Index(
            "uq_lessons_topic_display_order_key",
            "topic_id",
            "display_order",
            unique=True,
        ),
        CheckConstraint(
            "display_order >= 1",
            name="lessons_display_order_check",
        ),
        Index("lessons_topic_id_idx", "topic_id"),
    )

    topic = relationship("Topic", back_populates="lessons")
    materials = relationship("Material", back_populates="lesson")
