"""school_programs table — which programs a school actually offers."""
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import RecordStatus, sql_in_list


class SchoolProgram(TimestampMixin, Base):
    """Associates a school with a program version it offers."""

    __tablename__ = "school_programs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False
    )
    program_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("program_versions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecordStatus.ACTIVE.value
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(RecordStatus)})",
            name="school_programs_status_check",
        ),
        UniqueConstraint(
            "school_id", "program_version_id",
            name="uq_school_programs_school_id_program_version_id_key",
        ),
        # school_id is covered by the unique constraint above; program_version_id
        # is the trailing column, so it needs its own index.
        Index("school_programs_program_version_id_idx", "program_version_id"),
    )

    school = relationship("School", back_populates="program_offerings")
    program_version = relationship("ProgramVersion", back_populates="school_offerings")
