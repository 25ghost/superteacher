"""program_subjects table — subjects included in a program version."""
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ProgramSubjectType, sql_in_list


class ProgramSubject(Base):
    """Links a subject to a program version with its role and ordering."""

    __tablename__ = "program_subjects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    program_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("program_versions.id"), nullable=False
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProgramSubjectType.CORE.value
    )
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    display_order: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )

    __table_args__ = (
        UniqueConstraint(
            "program_version_id", "subject_id",
            name="uq_program_subjects_program_version_id_subject_id_key",
        ),
        CheckConstraint(
            f"subject_type IN ({sql_in_list(ProgramSubjectType)})",
            name="program_subjects_subject_type_check",
        ),
        # program_version_id is covered by the unique constraint above;
        # subject_id is trailing, so it needs its own index.
        Index("program_subjects_subject_id_idx", "subject_id"),
    )

    program_version = relationship("ProgramVersion", back_populates="subjects")
    subject = relationship("Subject", back_populates="program_subjects")
