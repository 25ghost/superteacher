"""teacher_evidence_contexts — which requested contexts one evidence covers.

Relevance is explicit, never inferred from a filename or label: the
applicant links each qualification/experience evidence to the specific
canonical learning contexts it is offered to substantiate. Approval of a
context requires verified evidence linked to that exact context.
"""
import uuid

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class TeacherEvidenceContext(TimestampMixin, Base):
    """Join row: one evidence document supports one learning context."""

    __tablename__ = "teacher_evidence_contexts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teacher_evidence.id"), nullable=False
    )
    learning_context_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_contexts.id"), nullable=False
    )

    __table_args__ = (
        Index(
            "uq_teacher_evidence_contexts_key",
            "evidence_id",
            "learning_context_id",
            unique=True,
        ),
        Index("teacher_evidence_contexts_context_id_idx", "learning_context_id"),
    )

    evidence = relationship("TeacherEvidence", back_populates="contexts")
