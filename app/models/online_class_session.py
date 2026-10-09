"""online_class_sessions table — one scheduled/live text class (Phase 3).

A class belongs to exactly ONE ``teaching_offerings`` row; the teacher is
derived from that offering and is deliberately never stored here as an
independently mutable relationship. The optional ``lesson_id`` lets a class
name one lesson without forcing the mapping — a live session may cover
several lessons or be a revision session, so the FK is nullable.

Lifecycle (``online_class_sessions_status_check`` + the service's
transition map):

    scheduled -> live -> ended      (terminal)
    scheduled -> cancelled          (terminal)

No state is ever reopened. Overlapping ``scheduled``/``live`` sessions for
the same offering are refused by the service while the offering row is
held under ``SELECT ... FOR UPDATE``, so a race cannot double-book a
teacher's timetable.
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import OnlineClassStatus, sql_in_list


class OnlineClassSession(TimestampMixin, Base):
    """One scheduled or live online class hosted inside one offering."""

    __tablename__ = "online_class_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    teaching_offering_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teaching_offerings.id"), nullable=False
    )
    lesson_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lessons.id"), nullable=True
    )
    scheduled_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    actual_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OnlineClassStatus.SCHEDULED.value
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(OnlineClassStatus)})",
            name="online_class_sessions_status_check",
        ),
        CheckConstraint(
            "scheduled_end_at > scheduled_start_at",
            name="online_class_sessions_schedule_check",
        ),
        Index("online_class_sessions_teaching_offering_id_idx", "teaching_offering_id"),
        Index("online_class_sessions_lesson_id_idx", "lesson_id"),
    )

    teaching_offering = relationship("TeachingOffering")
    lesson = relationship("Lesson")
