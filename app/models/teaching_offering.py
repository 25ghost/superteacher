"""teaching_offerings table — one teacher's offer to teach one context.

A verified teacher publishes an offering for a learning context; students
discover those offerings in the marketplace and enroll into them. The
offering is the join between *who* teaches (``teacher_id``) and *what* is
taught (``learning_context_id``).

Two offerings by the same teacher for the same context are only allowed
when at most one of them is ``active`` — enforced by the partial unique
index ``uq_teaching_offerings_teacher_context_active_key``, which leaves a
paused/archived history intact while making a duplicate live offer
impossible even under a race.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import TeachingOfferingStatus, sql_in_list


class TeachingOffering(TimestampMixin, Base):
    """A teacher offering one learning context (publish/pause/archive)."""

    __tablename__ = "teaching_offerings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teachers.id"), nullable=False
    )
    learning_context_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_contexts.id"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TeachingOfferingStatus.ACTIVE.value
    )

    __table_args__ = (
        Index(
            "uq_teaching_offerings_teacher_context_active_key",
            "teacher_id",
            "learning_context_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(TeachingOfferingStatus)})",
            name="teaching_offerings_status_check",
        ),
        Index("teaching_offerings_teacher_id_idx", "teacher_id"),
        Index("teaching_offerings_learning_context_id_idx", "learning_context_id"),
    )

    teacher = relationship("Teacher")
    learning_context = relationship("LearningContext", back_populates="teaching_offerings")

    enrollments = relationship(
        "LearningEnrollment", back_populates="teaching_offering"
    )
    topics = relationship("Topic", back_populates="teaching_offering")
    materials = relationship("Material", back_populates="teaching_offering")
