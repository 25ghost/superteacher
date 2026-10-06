"""material_progress table — one student's progress on ONE material.

Progress is personal: ``(student_id, material_id)`` uniquely identifies a
row (``uq_material_progress_student_material_key``). It never describes the
material globally, and one student's progress is never visible to another.

Lifecycle is forward-only and basic (slice 2C):

    not_started → in_progress → completed

``started_at`` is stamped on the first move into ``in_progress``;
``completed_at`` on the first move into ``completed``. Historical progress
stays with the material row it was earned on — switching teachers (or
enrolling in another offering of the same context) does not transfer it.
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import MaterialProgressStatus, sql_in_list


class MaterialProgress(TimestampMixin, Base):
    """One student's recorded progress on one published material."""

    __tablename__ = "material_progress"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False
    )
    material_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("materials.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=MaterialProgressStatus.NOT_STARTED.value
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # One progress record per (student, material) — enforced by the DB,
        # mirrored by the service for a readable 409 on a lost race.
        Index(
            "uq_material_progress_student_material_key",
            "student_id",
            "material_id",
            unique=True,
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(MaterialProgressStatus)})",
            name="material_progress_status_check",
        ),
        Index("material_progress_student_id_idx", "student_id"),
        Index("material_progress_material_id_idx", "material_id"),
        Index("material_progress_status_idx", "status"),
    )

    student = relationship("Student", back_populates="material_progress_rows")
    material = relationship("Material", back_populates="progress_rows")
