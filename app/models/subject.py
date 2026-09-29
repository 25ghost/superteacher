"""subjects table."""
import uuid

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import RecordStatus, sql_in_list


class Subject(TimestampMixin, Base):
    """A subject/module that a program can include."""

    __tablename__ = "subjects"

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
            name="subjects_status_check",
        ),
    )

    program_subjects = relationship("ProgramSubject", back_populates="subject")
    student_subjects = relationship("StudentSubject", back_populates="subject")
