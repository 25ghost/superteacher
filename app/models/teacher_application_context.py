"""teacher_application_contexts — ONE requested learning context per row.

Each application explicitly names the canonical learning contexts the
applicant requests; nothing is inferred from a free-text subject field.
Approval applies per context: every requested context must receive an
explicit ``approved`` or ``rejected`` outcome (with a reason when
rejected) before the application itself may be approved — an
administrator may approve a subset only by rejecting the rest with
reasons.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import ApplicationContextStatus, sql_in_list


class TeacherApplicationContext(TimestampMixin, Base):
    """One requested learning context on one application, with its decision."""

    __tablename__ = "teacher_application_contexts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teacher_applications.id"), nullable=False
    )
    learning_context_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_contexts.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ApplicationContextStatus.REQUESTED.value,
    )
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(ApplicationContextStatus)})",
            name="teacher_application_contexts_status_check",
        ),
        CheckConstraint(
            "status <> 'rejected' OR decision_reason IS NOT NULL",
            name="teacher_application_contexts_reason_check",
        ),
        Index(
            "uq_teacher_application_contexts_key",
            "application_id",
            "learning_context_id",
            unique=True,
        ),
        Index(
            "teacher_application_contexts_context_id_idx", "learning_context_id"
        ),
    )

    application = relationship("TeacherApplication", back_populates="contexts")
    learning_context = relationship("LearningContext")
