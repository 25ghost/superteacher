"""material_moderations table — one administrator's decision on one material.

Every approve/reject writes exactly one row: who decided, what they decided,
why, and when. The trail answers "who published/rejected this material and
with what reason" without reading the live ``materials.status`` (which a
later revision may have moved on from).

``user_id`` on the sibling ``auth_events`` audit trail names the *subject*
(the teacher whose material was decided); this table names the *reviewer*
directly on the decision row, so moderation history never depends on
correlating audit rows.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class MaterialModeration(TimestampMixin, Base):
    """One administrator decision (approve/reject) on one material."""

    __tablename__ = "material_moderations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    material_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="material_moderations_decision_check",
        ),
        Index("material_moderations_material_id_idx", "material_id"),
        Index("material_moderations_reviewer_user_id_idx", "reviewer_user_id"),
    )

    material = relationship("Material", back_populates="moderations")
    reviewer = relationship("User")
