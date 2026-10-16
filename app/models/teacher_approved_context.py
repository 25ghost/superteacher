"""teacher_approved_contexts — the authoritative approved teaching scope.

One row per (teacher, canonical learning context) explicitly approved by
a completed application review. This table — together with
``teachers.verification_status`` and ``teachers.verification_category`` —
is the DURABLE database state every teaching authorization decision reads.
An approved status alone grants nothing: no row, no teaching that
context. Rows are written only by the application approval workflow (and
the test fixtures that establish valid state), never from a request
payload.

Scope-extension applications add rows here only after review; previously
approved contexts stay authorized while a new review is outstanding.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class TeacherApprovedContext(TimestampMixin, Base):
    """One approved (teacher, learning context) scope grant."""

    __tablename__ = "teacher_approved_contexts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teachers.id"), nullable=False
    )
    learning_context_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_contexts.id"), nullable=False
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teacher_applications.id"), nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "teacher_id",
            "learning_context_id",
            name="uq_teacher_approved_contexts_teacher_context_key",
        ),
        Index("teacher_approved_contexts_context_id_idx", "learning_context_id"),
        Index("teacher_approved_contexts_application_id_idx", "application_id"),
    )

    teacher = relationship("Teacher")
    learning_context = relationship("LearningContext")
    application = relationship("TeacherApplication")
