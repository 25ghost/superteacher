"""class_attendance_segments table — durable participation intervals.

Attendance is never a single mutable ``attended`` boolean: it is the sum of
the participation segments below. A disconnect and a later reconnect
produce two rows, so the real history

    18:02 join -> 18:30 drop -> 18:34 reconnect -> 19:20 leave

survives as two intervals instead of collapsing into one flag. The MVP
derivation (computed at read time, never stored) is:

    ATTENDED  <=>  cumulative connected seconds >= 50% of the scheduled
                   class duration

The segments remain the authoritative source for a future teacher-rating
eligibility rule; nothing here is manually editable. ``connection_id``
names the WebSocket connection that produced the segment, which makes the
"one open segment per connection" invariant explicit and — since migration
0016 — a unique index: a reconnect can never be mistaken for its
predecessor. Open segments (``left_at IS NULL``) are finalized when the
connection closes or when the class ends — whichever happens first — and a
partial unique index keeps at most one open row per student per class.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class ClassAttendanceSegment(TimestampMixin, Base):
    """One continuous interval of one student inside one online class."""

    __tablename__ = "class_attendance_segments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    class_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("online_class_sessions.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    left_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "left_at IS NULL OR left_at >= joined_at",
            name="class_attendance_segments_date_order_check",
        ),
        # At most one OPEN segment per student per class: the backstop that
        # makes a duplicate-join race impossible (history is unrestricted —
        # a reconnect after close must add a second row, not collide).
        Index(
            "uq_class_attendance_segments_open_key",
            "student_id",
            "class_session_id",
            unique=True,
            postgresql_where=text("left_at IS NULL"),
            sqlite_where=text("left_at IS NULL"),
        ),
        Index(
            "class_attendance_segments_class_session_id_idx", "class_session_id"
        ),
        Index("class_attendance_segments_student_id_idx", "student_id"),
        # One WebSocket connection produces exactly one segment, ever.
        Index(
            "uq_class_attendance_segments_connection_id_key",
            "connection_id",
            unique=True,
        ),
    )

    class_session = relationship("OnlineClassSession")
    student = relationship("Student")
