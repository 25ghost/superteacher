"""teacher_evidence — one submitted identity/credential document.

Evidence is sensitive PRIVATE material (government IDs, qualification
certificates) — never a student learning material, never served from a
public URL. The bytes live behind the private storage backend via the
``file_assets`` row; this table carries the structured claim: what kind of
evidence it is, who issued it, the title, the country where relevant, and
the credential reference when one exists (server-side only — never
returned in ordinary API responses).

``duplicate_indicator`` is set at insert when the same file checksum or
credential reference already appears on another evidence row: a review
signal for an authorized human, never an automatic fraud finding.
``removed`` is a soft delete so the submitted history stays auditable.
"""
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import TeacherEvidenceStatus, TeacherEvidenceType, sql_in_list


class TeacherEvidence(TimestampMixin, Base):
    """One evidence document submitted on one application."""

    __tablename__ = "teacher_evidence"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teacher_applications.id"), nullable=False
    )
    file_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("file_assets.id"), nullable=False
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TeacherEvidenceStatus.SUBMITTED.value,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    issuing_organization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Sensitive: never exposed in ordinary API responses or logs.
    credential_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duplicate_indicator: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"evidence_type IN ({sql_in_list(TeacherEvidenceType)})",
            name="teacher_evidence_type_check",
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(TeacherEvidenceStatus)})",
            name="teacher_evidence_status_check",
        ),
        Index("teacher_evidence_application_id_idx", "application_id"),
        Index("teacher_evidence_file_asset_id_idx", "file_asset_id"),
    )

    application = relationship("TeacherApplication", back_populates="evidence")
    file_asset = relationship("FileAsset")
    contexts = relationship(
        "TeacherEvidenceContext",
        back_populates="evidence",
        cascade="all, delete-orphan",
    )
    checks = relationship(
        "TeacherVerificationCheck",
        back_populates="evidence",
        cascade="all, delete-orphan",
        order_by="TeacherVerificationCheck.created_at",
    )
