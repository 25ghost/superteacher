"""book_attempts table — one submitted attempt at one exercise set (P4, ADR-006).

An attempt is a durable, immutable record: once accepted it is never
updated or deleted (there is no service path that writes it again), so
history survives every later republication of the book. Identity and
scope:

- the STUDENT profile (``students.id``, not the user id) owns the
  attempt — the same convention as ``material_progress``;
- the revision AND the exercise-set unit are both recorded, with the
  composite FK ``(book_unit_id, book_revision_id) ->
  book_units(id, book_revision_id)`` binding the attempt to one unit of
  one specific edition — an attempt can never reference a unit of a
  different revision;
- totals and outcome are stored, not recomputed later: the pass decision
  (exact correct-count / total-count against the exercise set's
  threshold at submission time) is frozen with the attempt, so a later
  threshold change never rewrites history.

Equal weighting is a domain rule, not a column: one point per question,
``score_percent`` derived from the exact fraction (never rounded before
the pass comparison). Source FKs stay NO ACTION — attempts outlive
every referenced row's lifecycle.
"""
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class BookAttempt(TimestampMixin, Base):
    """One submitted attempt at one exercise set by one student."""

    __tablename__ = "book_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    #: The student PROFILE (not the user account) — material_progress convention.
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False
    )
    book_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("book_revisions.id"), nullable=False
    )
    #: The exercise-set unit that was attempted (composite FK below).
    book_unit_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    total_questions: Mapped[int] = mapped_column(Integer, nullable=False)
    correct_answers: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Exact fraction of correct answers, 0-100, unrounded — the pass
    #: comparison uses the exact fraction, this is the stored display.
    score_percent: Mapped[float] = mapped_column(Float, nullable=False)
    #: The frozen pass outcome (threshold at submission time).
    passed: Mapped[bool] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "total_questions >= 1", name="book_attempts_total_questions_check"
        ),
        CheckConstraint(
            "correct_answers >= 0 AND correct_answers <= total_questions",
            name="book_attempts_correct_answers_check",
        ),
        CheckConstraint(
            "score_percent >= 0 AND score_percent <= 100",
            name="book_attempts_score_percent_check",
        ),
        ForeignKeyConstraint(
            ["book_unit_id", "book_revision_id"],
            ["book_units.id", "book_units.book_revision_id"],
            name="book_attempts_book_unit_id_fkey",
        ),
        Index("book_attempts_student_id_idx", "student_id"),
        Index("book_attempts_book_revision_id_idx", "book_revision_id"),
        Index("book_attempts_book_unit_id_idx", "book_unit_id"),
        # Attempt history for one student on one exercise set (P5 listing).
        Index(
            "book_attempts_student_unit_idx", "student_id", "book_unit_id"
        ),
    )

    student = relationship("Student")
    # Both revision and unit are read-only views over the same stored
    # columns (the composite FK and the plain FK share
    # book_revision_id); the service writes the columns directly, so
    # neither relationship may cascade writes.
    revision = relationship("BookRevision", viewonly=True)
    unit = relationship("BookUnit", viewonly=True, overlaps="revision")
    answers = relationship(
        "BookAttemptAnswer",
        back_populates="attempt",
        order_by="BookAttemptAnswer.created_at.asc()",
    )
