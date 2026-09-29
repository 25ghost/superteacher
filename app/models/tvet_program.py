"""tvet_programs table — TVET-specific attributes of a program."""
import uuid

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class TVETProgram(TimestampMixin, Base):
    """Links a program to its TVET sector (one TVET profile per program)."""

    __tablename__ = "tvet_programs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("programs.id"), unique=True, nullable=False
    )
    sector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tvet_sectors.id"), nullable=False
    )

    __table_args__ = (
        # program_id is covered by its unique constraint; sector_id needs an
        # index of its own ("which programs belong to this sector?").
        Index("tvet_programs_sector_id_idx", "sector_id"),
    )

    program = relationship("Program", back_populates="tvet_profile", uselist=False)
    sector = relationship("TVETSector", back_populates="programs")
