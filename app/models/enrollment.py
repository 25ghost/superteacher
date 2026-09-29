"""student_enrollments table — a student's enrollment in a school for a year."""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import EnrollmentStatus, sql_in_list


class StudentEnrollment(TimestampMixin, Base):
    """Enrollment of a student in a school for an academic year.

    A student enrolls at most once per academic year. Cross-entity
    consistency (pathway matches level, program version matches pathway and
    level, school actually offers the program) is enforced by the service
    layer, not by foreign keys.
    """

    __tablename__ = "student_enrollments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False
    )
    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_years.id"), nullable=False
    )
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False
    )
    pathway_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pathways.id"), nullable=False
    )
    education_level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("education_levels.id"), nullable=False
    )
    program_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("program_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnrollmentStatus.PENDING.value
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "student_id", "academic_year_id",
            name="uq_student_enrollments_student_id_academic_year_id_key",
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(EnrollmentStatus)})",
            name="student_enrollments_status_check",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="student_enrollments_date_order_check",
        ),
        # student_id is covered by the unique constraint above.
        Index("student_enrollments_school_id_idx", "school_id"),
        Index("student_enrollments_academic_year_id_idx", "academic_year_id"),
        Index("student_enrollments_pathway_id_idx", "pathway_id"),
        Index("student_enrollments_education_level_id_idx", "education_level_id"),
        Index("student_enrollments_program_version_id_idx", "program_version_id"),
    )

    student = relationship("Student", back_populates="enrollments")
    academic_year = relationship("AcademicYear", back_populates="enrollments")
    school = relationship("School", back_populates="enrollments")
    pathway = relationship("Pathway", back_populates="enrollments")
    education_level = relationship("EducationLevel", back_populates="enrollments")
    program_version = relationship("ProgramVersion", back_populates="enrollments")

    subjects = relationship(
        "StudentSubject",
        back_populates="enrollment",
        cascade="all, delete-orphan",
    )
