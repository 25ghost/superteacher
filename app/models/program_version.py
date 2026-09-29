"""program_versions table — a program's offering in a given academic year."""
import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import RecordStatus, sql_in_list


class ProgramVersion(TimestampMixin, Base):
    """A program as offered in a specific academic year, pathway, and level.

    Program history is preserved as rows: a program changing between years
    produces a new version rather than mutating a single row.

    The same program cannot be offered twice for the same academic year,
    pathway, and education level — that tuple is the natural key (see
    ``uq_program_versions_offering_key``), which also lets reference-data
    seeders upsert an offering idempotently.
    """

    __tablename__ = "program_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("programs.id"), nullable=False
    )
    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_years.id"), nullable=False
    )
    pathway_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pathways.id"), nullable=False
    )
    education_level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("education_levels.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    effective_from: Mapped[date | None] = mapped_column()
    effective_until: Mapped[date | None] = mapped_column()
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecordStatus.ACTIVE.value
    )

    __table_args__ = (
        # Natural key of an offering. Named compactly on purpose: spelling out
        # all four columns would exceed PostgreSQL's 63-byte identifier limit.
        UniqueConstraint(
            "program_id", "academic_year_id", "pathway_id", "education_level_id",
            name="uq_program_versions_offering_key",
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(RecordStatus)})",
            name="program_versions_status_check",
        ),
        CheckConstraint(
            "effective_until IS NULL OR effective_from IS NULL "
            "OR effective_until >= effective_from",
            name="program_versions_effective_window_check",
        ),
        # NOTE: no standalone program_id index — the natural-key unique
        # constraint above already leads with program_id, so a separate index
        # would be redundant (PostgreSQL can use the leading column of it).
        Index("program_versions_academic_year_id_idx", "academic_year_id"),
        Index("program_versions_pathway_id_idx", "pathway_id"),
        Index("program_versions_education_level_id_idx", "education_level_id"),
    )

    program = relationship("Program", back_populates="versions")
    academic_year = relationship("AcademicYear", back_populates="program_versions")
    pathway = relationship("Pathway", back_populates="program_versions")
    education_level = relationship("EducationLevel", back_populates="program_versions")

    subjects = relationship("ProgramSubject", back_populates="program_version")
    school_offerings = relationship("SchoolProgram", back_populates="program_version")
    enrollments = relationship("StudentEnrollment", back_populates="program_version")
