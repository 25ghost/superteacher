"""student_book_progress table — one student's durable position in ONE
published book revision (P5).

Progress is personal and revision-scoped: the approved key
``(student_id, book_revision_id, book_unit_id)`` (``uq_student_book_
progress_key``) identifies a row, so progress never transfers between
students and never leaks across editions — when an administrator
publishes a new revision, the new edition's units have no rows yet and
every student's guided walk starts fresh from its first unit. Historical
progress stays with the revision it was earned on, exactly like
``material_progress`` stays with its material.

A row IS a completion: the student finished the unit's content, or a
passing attempt completed a required exercise-set checkpoint (written in
the same transaction as the attempt — see ``book_assessment_service``).
``completed_at`` is stamped once and never rewritten; revisiting a
completed unit only reads it.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class StudentBookProgress(TimestampMixin, Base):
    """One completed unit of one published revision for one student."""

    __tablename__ = "student_book_progress"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    #: The student PROFILE (not the user account) — material_progress convention.
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False
    )
    book_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("book_revisions.id"), nullable=False
    )
    #: The completed unit (composite FK below) — always a unit of the
    #: SAME revision the progress was earned on.
    book_unit_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: When the unit was completed (content step finished or checkpoint
    #: passed). Historical completions are never rewritten.
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # One progress record per (student, revision, unit) — enforced by
        # the database, mirrored by the service for a readable 409 on a
        # lost race.
        Index(
            "uq_student_book_progress_key",
            "student_id",
            "book_revision_id",
            "book_unit_id",
            unique=True,
        ),
        CheckConstraint(
            "completed_at IS NOT NULL",
            name="student_book_progress_completed_at_check",
        ),
        ForeignKeyConstraint(
            ["book_unit_id", "book_revision_id"],
            ["book_units.id", "book_units.book_revision_id"],
            name="student_book_progress_book_unit_id_fkey",
        ),
        Index("student_book_progress_student_id_idx", "student_id"),
        Index("student_book_progress_book_revision_id_idx", "book_revision_id"),
        Index("student_book_progress_book_unit_id_idx", "book_unit_id"),
    )

    student = relationship("Student", back_populates="book_progress_rows")
    # Both revision and unit are read-only views over the same stored
    # columns (the composite FK and the plain FK share book_revision_id);
    # the service writes the columns directly, so neither relationship
    # may cascade writes.
    revision = relationship("BookRevision", viewonly=True)
    unit = relationship("BookUnit", viewonly=True, overlaps="revision")
