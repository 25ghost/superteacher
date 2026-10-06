"""learning_contexts table — a teachable unit, independent of who teaches it.

A context pins *what* is taught: an academic year, a pathway, an education
level, an optional program version and a subject. Teachers do not create
contexts directly — creating a teaching offering resolves (and, the first
time, inserts) the context that offering belongs to, so the same context is
shared by every teacher offering it and by every student enrolled in it.

That sharing is what makes the marketplace rules expressible: a student
holds at most one *active* enrollment per context (see
``learning_enrollments``), which is exactly "leave before you switch
teacher".

Natural key: ``(academic_year_id, pathway_id, education_level_id,
program_version_id, subject_id)``. ``program_version_id`` is nullable, and
NULLs are distinct in a unique index, so uniqueness is enforced by two
partial indexes — one for rows carrying a program version, one for rows
without — rather than by a plain unique constraint that would let the same
context exist twice with a NULL program.
"""
import uuid

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin

#: The natural-key columns shared by both partial unique indexes.
_NATURAL_KEY = (
    "academic_year_id",
    "pathway_id",
    "education_level_id",
    "program_version_id",
    "subject_id",
)


class LearningContext(TimestampMixin, Base):
    """One (year, pathway, level, program?, subject) unit of teaching."""

    __tablename__ = "learning_contexts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_years.id"), nullable=False
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
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id"), nullable=False
    )

    __table_args__ = (
        Index(
            "uq_learning_contexts_with_program_key",
            *_NATURAL_KEY,
            unique=True,
            # Both dialects: SQLite has no postgresql_* counterpart and would
            # otherwise enforce uniqueness over NULL program versions too,
            # splitting contexts that PostgreSQL treats as one.
            postgresql_where=text("program_version_id IS NOT NULL"),
            sqlite_where=text("program_version_id IS NOT NULL"),
        ),
        Index(
            "uq_learning_contexts_without_program_key",
            "academic_year_id",
            "pathway_id",
            "education_level_id",
            "subject_id",
            unique=True,
            postgresql_where=text("program_version_id IS NULL"),
            sqlite_where=text("program_version_id IS NULL"),
        ),
        # academic_year_id leads the first index above, so only the other
        # foreign keys need their own lookup index.
        Index("learning_contexts_pathway_id_idx", "pathway_id"),
        Index("learning_contexts_education_level_id_idx", "education_level_id"),
        Index("learning_contexts_program_version_id_idx", "program_version_id"),
        Index("learning_contexts_subject_id_idx", "subject_id"),
    )

    academic_year = relationship("AcademicYear")
    pathway = relationship("Pathway")
    education_level = relationship("EducationLevel")
    program_version = relationship("ProgramVersion")
    subject = relationship("Subject")

    teaching_offerings = relationship(
        "TeachingOffering", back_populates="learning_context"
    )
