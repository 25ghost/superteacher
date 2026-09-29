"""academic_years table."""
import uuid
from datetime import date

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import AcademicYearStatus, sql_in_list


class AcademicYear(TimestampMixin, Base):
    """A school year, e.g. 2026. end_date is enforced as >= start_date."""

    __tablename__ = "academic_years"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    start_date: Mapped[date] = mapped_column(nullable=False)
    end_date: Mapped[date] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AcademicYearStatus.PLANNED.value,
    )

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="academic_years_date_order_check"),
        CheckConstraint(
            f"status IN ({sql_in_list(AcademicYearStatus)})",
            name="academic_years_status_check",
        ),
    )

    enrollments = relationship("StudentEnrollment", back_populates="academic_year")
    program_versions = relationship("ProgramVersion", back_populates="academic_year")
