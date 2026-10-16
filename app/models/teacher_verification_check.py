"""teacher_verification_checks — one reviewer check of one evidence row.

Verification is a deterministic, recorded, human decision — never an
automated/AI judgement. Each row records WHO checked (the reviewing
administrator), WHAT was checked (the evidence and the source institution
or register), HOW (registry lookup, issuer confirmation, documented
manual contact, other), WHEN (``created_at``) and the OUTCOME with
notes explaining it.

Approval accounting uses the LATEST check per evidence (highest ``seq``):
a latest outcome of ``failed`` or ``inconclusive`` blocks approval until
it is resolved by a later recorded check or the evidence is replaced. A
failed check cannot be bypassed by resubmitting the same document as a
new evidence row — the duplicate indicators surface that attempt.
"""
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import VerificationCheckOutcome, VerificationMethod, sql_in_list


class TeacherVerificationCheck(TimestampMixin, Base):
    """One recorded verification check on one evidence document."""

    __tablename__ = "teacher_verification_checks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teacher_applications.id"), nullable=False
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teacher_evidence.id"), nullable=False
    )
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    #: Monotonic per evidence: latest = MAX(seq) (timestamps may tie
    #: inside one transaction, so ordering never depends on them).
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    source_institution: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"method IN ({sql_in_list(VerificationMethod)})",
            name="teacher_verification_checks_method_check",
        ),
        CheckConstraint(
            f"outcome IN ({sql_in_list(VerificationCheckOutcome)})",
            name="teacher_verification_checks_outcome_check",
        ),
        Index(
            "uq_teacher_verification_checks_evidence_seq_key",
            "evidence_id",
            "seq",
            unique=True,
        ),
        Index(
            "teacher_verification_checks_application_id_idx", "application_id"
        ),
    )

    application = relationship("TeacherApplication", back_populates="checks")
    evidence = relationship("TeacherEvidence", back_populates="checks")
    reviewer = relationship("User")
