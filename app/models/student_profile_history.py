"""student_profile_history table — audit trail for profile changes.

Records profile *creation* (``change_type="create"``, ``field_name="profile"``
— attributed to the acting administrator or the new account itself) and
every update to a mutable profile field (full_name, gender, country —
including clears of nullable columns) with old/new values, who made the
change, and when. email/phone/date_of_birth are immutable identity anchors
and are never written here.

Deletion policy: audit rows are attached to the *student* (``ON DELETE
CASCADE`` — a deleted profile takes its history with it) but only
attributed to the actor (``ON DELETE SET NULL`` — deleting a user keeps
the audit trail, nulling ``changed_by``). There is no ORM relationship
from ``Student`` to this table, so cleanup runs purely at the database
level whether the delete came from the ORM or raw SQL.
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class StudentProfileHistory(TimestampMixin, Base):
    """One profile change event for one student."""

    __tablename__ = "student_profile_history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_student_profile_history_student_id", "student_id"),
        Index("ix_student_profile_history_changed_by", "changed_by"),
        # Vocabulary checks (migration 0004) mirror the repository's
        # CHANGE_TYPES / FIELD_NAMES constants.
        CheckConstraint(
            "change_type IN ('create', 'update')",
            name="student_profile_history_change_type_check",
        ),
        CheckConstraint(
            "field_name IN ('profile', 'full_name', 'gender', 'country')",
            name="student_profile_history_field_name_check",
        ),
    )
