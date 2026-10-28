"""book_question_options table — one answer option of one question (P4, ADR-006).

Options are administrator-authored rows; exactly one of them is ``correct``
once the owning question's answer key is verified. Integrity mechanisms:

- ``option_text`` is never blank (named CHECK) and the composite
  ``(id, book_question_id)`` unique constraint is the target of the
  attempt-answer FK pair, so a submitted answer can only ever reference
  an option of the question it answers;
- sibling ``display_order`` is unique per question;
- AT MOST ONE correct option per question is a database rule via the
  PARTIAL unique index below — "at least one" is the service's
  verification action (a CHECK cannot count sibling rows). The partial
  index deliberately does not require correctness: an unverified draft
  question may have zero correct options, but never two.

Source FKs stay NO ACTION like the rest of the book family.
"""
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class BookQuestionOption(TimestampMixin, Base):
    """One answer option of one objective question."""

    __tablename__ = "book_question_options"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    book_question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("book_questions.id"), nullable=False
    )
    #: The option text, verbatim from the administrator. Never blank.
    option_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Sibling order within the question.
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The answer key: FALSE until (and unless) this option is the
    #: verified correct one. Never inferred from text order.
    is_correct: Mapped[bool] = mapped_column(nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("option_text <> ''", name="book_question_options_text_check"),
        CheckConstraint(
            "display_order >= 1", name="book_question_options_display_order_check"
        ),
        # FK target for book_attempt_answers' composite (option, question)
        # pair — an answer can only reference an option of its own question.
        UniqueConstraint(
            "id", "book_question_id", name="book_question_options_id_question_key"
        ),
        Index(
            "uq_book_question_options_question_order_key",
            "book_question_id",
            "display_order",
            unique=True,
        ),
        # At most ONE correct option per question (at least one is the
        # verification action in the service).
        Index(
            "uq_book_question_options_question_correct_key",
            "book_question_id",
            unique=True,
            postgresql_where=text("is_correct"),
            sqlite_where=text("is_correct"),
        ),
        # The FK column carries its own index (0016 house rule).
        Index("book_question_options_book_question_id_idx", "book_question_id"),
    )

    question = relationship("BookQuestion", back_populates="options")
