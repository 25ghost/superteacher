"""teachers table — the teacher profile, one row per teacher account.

Created by migration ``0006`` together with the ``pending`` account status:

- an administrator creates the account (``POST /admin/teachers``) with a
  status of ``pending``; the row below stores the profile fields the invite
  email carries;
- accepting the invite (``POST /auth/accept-invite``) sets the password and
  flips the account to ``active``.

Deletion policy mirrors ``students``: the ``user_id`` FK has no ``ondelete``,
so deleting a user that still owns a profile is refused by the database.

Migration ``0010`` adds ``verification_status`` (Phase 1): the vetting axis
that gates publishing teaching offerings, deliberately separate from the
account status above.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import TeacherVerificationStatus, sql_in_list


class Teacher(TimestampMixin, Base):
    """A teacher's profile, linked one-to-one to a user account."""

    __tablename__ = "teachers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schools.id"), nullable=True
    )
    # Optional teaching subject/assignment hint (free text, not a catalog FK).
    subject: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Phase 1: vetting state, separate from the account status — the
    # service refuses teaching offerings unless this is 'approved'.
    verification_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TeacherVerificationStatus.PENDING.value,
    )

    __table_args__ = (
        CheckConstraint(
            f"verification_status IN ({sql_in_list(TeacherVerificationStatus)})",
            name="teachers_verification_status_check",
        ),
    )

    user = relationship("User", uselist=False)
