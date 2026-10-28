"""book_attempt_answers table — one submitted answer within one attempt (P4, ADR-006).

One row per answered question of one attempt. Integrity mechanisms:

- ``UNIQUE (book_attempt_id, book_question_id)`` — a question can be
  answered at most once per attempt (the service additionally refuses
  duplicates, unknowns and omissions BEFORE anything is written);
- the composite FK ``(selected_option_id, book_question_id) ->
  book_question_options(id, book_question_id)`` proves in the database
  that the selected option is an option OF the question being answered —
  an answer can never pair a question with a foreign option;
- ``is_correct`` is the grading verdict frozen at submission (the
  option's own correctness may never change afterwards because the
  revision is published and immutable, but the attempt keeps its own
  verdict regardless).

Source FKs stay NO ACTION: attempts and their answers are history.
"""
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class BookAttemptAnswer(TimestampMixin, Base):
    """One submitted answer: the option a student selected for one question."""

    __tablename__ = "book_attempt_answers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    book_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("book_attempts.id"), nullable=False
    )
    book_question_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: The option the student selected (composite FK below binds it to
    #: the same question).
    selected_option_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: The grading verdict recorded at submission.
    is_correct: Mapped[bool] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "book_attempt_id",
            "book_question_id",
            name="uq_book_attempt_answers_attempt_question_key",
        ),
        ForeignKeyConstraint(
            ["selected_option_id", "book_question_id"],
            ["book_question_options.id", "book_question_options.book_question_id"],
            name="book_attempt_answers_selected_option_id_fkey",
        ),
        Index("book_attempt_answers_book_attempt_id_idx", "book_attempt_id"),
        Index("book_attempt_answers_book_question_id_idx", "book_question_id"),
        # The composite FK's leading column carries its own index (0016
        # house rule: PostgreSQL does not index foreign keys automatically).
        Index(
            "book_attempt_answers_selected_option_id_idx", "selected_option_id"
        ),
    )

    attempt = relationship("BookAttempt", back_populates="answers")
    # book_question_id is part of the composite FK to book_question_options
    # (not a standalone FK to book_questions), so the join is explicit and
    # read-only — the authoritative link is the composite pair below.
    question = relationship(
        "BookQuestion",
        primaryjoin="foreign(BookAttemptAnswer.book_question_id) == BookQuestion.id",
        viewonly=True,
    )
    selected_option = relationship("BookQuestionOption")
