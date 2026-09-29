"""programs table."""
import uuid

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import ProgramType, RecordStatus, sql_in_list


class Program(TimestampMixin, Base):
    """A program of study (combination, TVET program, stream, or other).

    Concrete offerings for a given academic year live in ProgramVersion.
    """

    __tablename__ = "programs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    program_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProgramType.OTHER.value
    )
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecordStatus.ACTIVE.value
    )

    __table_args__ = (
        CheckConstraint(
            f"program_type IN ({sql_in_list(ProgramType)})",
            name="programs_program_type_check",
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(RecordStatus)})",
            name="programs_status_check",
        ),
    )

    versions = relationship("ProgramVersion", back_populates="program")
    tvet_profile = relationship(
        "TVETProgram", back_populates="program", uselist=False
    )
