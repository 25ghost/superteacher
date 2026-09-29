"""tvet_sectors table."""
import uuid

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import RecordStatus, sql_in_list


class TVETSector(TimestampMixin, Base):
    """A TVET sector (e.g. construction, ICT, agriculture)."""

    __tablename__ = "tvet_sectors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecordStatus.ACTIVE.value
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(RecordStatus)})",
            name="tvet_sectors_status_check",
        ),
    )

    programs = relationship("TVETProgram", back_populates="sector")
