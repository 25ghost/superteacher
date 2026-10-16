"""teacher_applications table — one verification review cycle for a teacher.

Registration and verification are separate operations: creating a teacher
account never creates an application, and an application never grants
teaching access until it is ``approved`` with an explicitly approved
context scope.

States (``TeacherApplicationStatus``): ``draft`` (editable), ``submitted``
(under review), ``needs_information`` (reviewer asked for specific
corrections — the applicant may edit and resubmit), ``approved``,
``rejected`` (both terminal) and ``withdrawn`` (applicant-initiated).
A re-application after a terminal state is a NEW row whose
``previous_application_id`` preserves the decision history.

``category`` is the CLAIMED verification category; it becomes the
teacher's approved ``teachers.verification_category`` only when the
application is approved. One non-terminal application per teacher is
enforced by the partial unique index
``uq_teacher_applications_active_key``.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import (
    TeacherApplicationStatus,
    TeacherVerificationCategory,
    sql_in_list,
)


class TeacherApplication(TimestampMixin, Base):
    """One teacher's verification application (one review cycle)."""

    __tablename__ = "teacher_applications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teachers.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TeacherApplicationStatus.DRAFT.value
    )
    # Claimed category (never inferred from email/subject/filename); the
    # approved one lives on ``teachers.verification_category``.
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Formal-teacher employment claim: when set, employment confirmation
    # evidence is mandatory. Not required from every formal teacher.
    claimed_school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schools.id"), nullable=True
    )
    # Independent-tutor declaration: will not misrepresent formal
    # qualifications, professional registration or school employment.
    misrepresentation_declaration: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Free-text profile hint only — never eligibility or approved scope.
    statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Rejection reason or the latest request-for-information message.
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("teacher_applications.id"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(TeacherApplicationStatus)})",
            name="teacher_applications_status_check",
        ),
        CheckConstraint(
            f"category IS NULL OR category IN "
            f"({sql_in_list(TeacherVerificationCategory)})",
            name="teacher_applications_category_check",
        ),
        Index(
            "uq_teacher_applications_active_key",
            "teacher_id",
            unique=True,
            postgresql_where=text(
                "status IN ('draft', 'submitted', 'needs_information')"
            ),
            sqlite_where=text(
                "status IN ('draft', 'submitted', 'needs_information')"
            ),
        ),
        Index("teacher_applications_teacher_id_idx", "teacher_id"),
        Index("teacher_applications_status_idx", "status"),
    )

    teacher = relationship("Teacher")
    previous_application = relationship(
        "TeacherApplication", remote_side=[id], uselist=False
    )
    contexts = relationship(
        "TeacherApplicationContext",
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="TeacherApplicationContext.created_at",
    )
    evidence = relationship(
        "TeacherEvidence",
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="TeacherEvidence.created_at",
    )
    checks = relationship(
        "TeacherVerificationCheck",
        back_populates="application",
        cascade="all, delete-orphan",
    )
    assessments = relationship(
        "TeacherAssessment",
        back_populates="application",
        cascade="all, delete-orphan",
    )
