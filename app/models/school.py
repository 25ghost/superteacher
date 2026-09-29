"""schools table."""
import uuid

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import RecordStatus, sql_in_list


class School(TimestampMixin, Base):
    """A school. Its pathway/program offering is represented through
    SchoolProgram, not a direct pathway column here."""

    __tablename__ = "schools"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    school_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    school_type: Mapped[str | None] = mapped_column(String(32))
    province: Mapped[str | None] = mapped_column(String(80))
    district: Mapped[str | None] = mapped_column(String(80))
    sector: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecordStatus.ACTIVE.value
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(RecordStatus)})",
            name="schools_status_check",
        ),
        Index("schools_name_idx", "name"),
        Index("schools_district_idx", "district"),
    )

    program_offerings = relationship("SchoolProgram", back_populates="school")
    enrollments = relationship("StudentEnrollment", back_populates="school")
