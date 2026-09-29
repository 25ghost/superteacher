"""students table — one student profile per user account.

Deletion policy: the ``user_id`` FK deliberately has NO ``ondelete`` —
deleting a user that still owns a profile is refused by the database
(explicit profile deletion must come first). Cascade of dependent rows is
asymmetric: ORM-level only for ``enrollments`` (``cascade="all,
delete-orphan"`` above — ``session.delete(student)`` works), but a raw
``DELETE FROM students`` is refused while enrollments exist, because
``student_enrollments.student_id`` also has no ``ondelete``.
"""
import uuid
from datetime import date

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class Student(TimestampMixin, Base):
    """A student's profile, linked one-to-one to a user account."""

    __tablename__ = "students"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(nullable=True)
    gender: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(80))

    __table_args__ = (
        # user_id is covered by the unique constraint above (uq_students_user_id_key).
        Index("students_full_name_idx", "full_name"),
        # DOB: not in the future AND within a sane historical floor — the
        # service additionally enforces the configurable age bounds
        # (STUDENT_MIN_AGE_YEARS / STUDENT_MAX_AGE_YEARS) for a readable 422.
        # (ISO string literal — portable across PostgreSQL and the SQLite
        # unit-test databases; both cast/compare date columns lexicographically
        # for ISO-8601.)
        CheckConstraint(
            "date_of_birth IS NULL OR "
            "(date_of_birth <= CURRENT_DATE AND date_of_birth >= '1900-01-01')",
            name="students_date_of_birth_range_check",
        ),
        # Gender vocabulary (hardening, migration 0004): mirrors the schema
        # GENDER_VALUES so direct database writes cannot store garbage.
        CheckConstraint(
            "gender IS NULL OR gender IN ('female', 'male', 'other', 'undisclosed')",
            name="students_gender_check",
        ),
    )

    user = relationship("User", back_populates="student", uselist=False)
    enrollments = relationship(
        "StudentEnrollment",
        back_populates="student",
        cascade="all, delete-orphan",
    )
