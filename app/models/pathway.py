"""pathways table — O_LEVEL, A_LEVEL, TVET are data rows, not logic."""
import uuid

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import RecordStatus, sql_in_list


class Pathway(TimestampMixin, Base):
    """A learning pathway (e.g. O Level, A Level, TVET).

    The conceptual codes O_LEVEL / A_LEVEL / TVET are seeded as rows; they are
    never hard-coded into application business logic.
    """

    __tablename__ = "pathways"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecordStatus.ACTIVE.value
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(RecordStatus)})",
            name="pathways_status_check",
        ),
    )

    levels = relationship("PathwayLevel", back_populates="pathway")
    program_versions = relationship("ProgramVersion", back_populates="pathway")
    enrollments = relationship("StudentEnrollment", back_populates="pathway")
