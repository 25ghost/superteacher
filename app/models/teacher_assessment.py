"""teacher_assessments — recorded subject-specific assessment outcomes.

The independent-tutor experience route requires BOTH documented
subject-related professional experience AND a recorded PASSING result of
a subject-specific assessment. This table is the secure backend record of
that assessment: its canonical subject scope, the date it was taken, the
assessor, the outcome and supporting notes — recorded by an authorized
reviewer. The platform never scores or administers assessments and never
invents a passing threshold; only a recorded ``passed`` outcome satisfies
the policy.
"""
import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import AssessmentOutcome, sql_in_list


class TeacherAssessment(TimestampMixin, Base):
    """One recorded subject-specific assessment outcome."""

    __tablename__ = "teacher_assessments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teacher_applications.id"), nullable=False
    )
    #: The canonical subject scope the assessment covered.
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id"), nullable=False
    )
    assessment_date: Mapped[date] = mapped_column(Date, nullable=False)
    assessor: Mapped[str] = mapped_column(String(200), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"outcome IN ({sql_in_list(AssessmentOutcome)})",
            name="teacher_assessments_outcome_check",
        ),
        Index("teacher_assessments_application_id_idx", "application_id"),
        Index("teacher_assessments_subject_id_idx", "subject_id"),
    )

    application = relationship("TeacherApplication", back_populates="assessments")
    subject = relationship("Subject")
