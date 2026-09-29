"""student_subjects table — subjects a student takes within an enrollment."""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import StudentSubjectStatus, sql_in_list


class StudentSubject(TimestampMixin, Base):
    """Links a subject to one of a student's enrollments."""

    __tablename__ = "student_subjects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student_enrollments.id"), nullable=False
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StudentSubjectStatus.ACTIVE.value
    )

    __table_args__ = (
        UniqueConstraint(
            "enrollment_id", "subject_id",
            name="uq_student_subjects_enrollment_id_subject_id_key",
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(StudentSubjectStatus)})",
            name="student_subjects_status_check",
        ),
        # enrollment_id is covered by the unique constraint above;
        # subject_id is trailing, so it needs its own index.
        Index("student_subjects_subject_id_idx", "subject_id"),
    )

    enrollment = relationship("StudentEnrollment", back_populates="subjects")
    subject = relationship("Subject", back_populates="student_subjects")
