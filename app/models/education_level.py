"""education_levels table — S1..S6 and L3..L5 are data rows, not logic."""
import uuid

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import RecordStatus, sql_in_list


class EducationLevel(TimestampMixin, Base):
    """An education level (e.g. S1, S4, L3)."""

    __tablename__ = "education_levels"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    level_number: Mapped[int] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecordStatus.ACTIVE.value
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(RecordStatus)})",
            name="education_levels_status_check",
        ),
    )

    pathways = relationship("PathwayLevel", back_populates="education_level")
    program_versions = relationship("ProgramVersion", back_populates="education_level")
    enrollments = relationship("StudentEnrollment", back_populates="education_level")
