"""learning_enrollments table — a student's membership of one offering.

Deliberately separate from ``student_enrollments`` (the academic-year
registration that links a student to a school): this row is the *marketplace*
relationship, and it duplicates ``learning_context_id`` from the offering so
the uniqueness rule can be enforced by the database alone:

    one ACTIVE enrollment per (student, learning context)

— ``uq_learning_enrollments_student_context_active_key``, a partial unique
index. Because it keys on the *context* rather than the offering, a student
who wants to study the same subject with a different teacher must first
leave (``POST /me/learning-enrollments/{id}/leave``), which is the explicit
"leave before you switch" step the product requires.
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import LearningEnrollmentStatus, sql_in_list


class LearningEnrollment(TimestampMixin, Base):
    """A student enrolled in a teaching offering, active or ended."""

    __tablename__ = "learning_enrollments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False
    )
    teaching_offering_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teaching_offerings.id"), nullable=False
    )
    # Denormalised from the offering so the partial unique index below can
    # key on the context without a join (and survive an offering swap).
    learning_context_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_contexts.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=LearningEnrollmentStatus.ACTIVE.value
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_learning_enrollments_student_context_active_key",
            "student_id",
            "learning_context_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(LearningEnrollmentStatus)})",
            name="learning_enrollments_status_check",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="learning_enrollments_date_order_check",
        ),
        # The partial index above only covers active rows, so the full
        # history (ended rows included) needs its own student_id index.
        Index("learning_enrollments_student_id_idx", "student_id"),
        Index("learning_enrollments_teaching_offering_id_idx", "teaching_offering_id"),
        Index("learning_enrollments_learning_context_id_idx", "learning_context_id"),
    )

    student = relationship("Student")
    teaching_offering = relationship("TeachingOffering", back_populates="enrollments")
    learning_context = relationship("LearningContext")
