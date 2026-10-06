"""materials table — one teacher-authored teaching artifact under an offering.

A material hangs off a teaching offering (and optionally one of its
lessons). It does NOT re-declare the Admin-owned catalog: teacher, academic
year, pathway, level and subject are all derivable through
``teaching_offerings`` → ``learning_contexts``, so the material never
carries those columns itself.

Moderation is a domain state machine:

    draft → pending_review → (published | rejected) → archived

Only an administrator approves or rejects; a teacher cannot publish
directly. A rejected material returns to ``draft`` through the revision
flow. Published materials are effectively immutable in the MVP — any change
is a new revision (slice 2C), never a silent overwrite of a live artifact.
``archived`` retires a published material without deleting its history.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import MaterialStatus, MaterialType, sql_in_list


class Material(TimestampMixin, Base):
    """One teacher-authored material under one of their teaching offerings."""

    __tablename__ = "materials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    teaching_offering_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teaching_offerings.id"), nullable=False
    )
    lesson_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lessons.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    material_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=MaterialStatus.DRAFT.value
    )
    file_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("file_assets.id"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"material_type IN ({sql_in_list(MaterialType)})",
            name="materials_material_type_check",
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(MaterialStatus)})",
            name="materials_status_check",
        ),
        Index("materials_teaching_offering_id_idx", "teaching_offering_id"),
        Index("materials_lesson_id_idx", "lesson_id"),
        Index("materials_file_asset_id_idx", "file_asset_id"),
        Index("materials_status_idx", "status"),
    )

    teaching_offering = relationship("TeachingOffering", back_populates="materials")
    lesson = relationship("Lesson", back_populates="materials")
    file_asset = relationship("FileAsset", back_populates="materials")
    moderations = relationship(
        "MaterialModeration",
        back_populates="material",
        cascade="all, delete-orphan",
        order_by="MaterialModeration.created_at.desc()",
    )
    # Personal per-student progress rows (never shared, never deleted with
    # a material archive -- history stays with the material id).
    progress_rows = relationship(
        "MaterialProgress",
        back_populates="material",
        cascade="all, delete-orphan",
    )
