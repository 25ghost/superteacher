"""book_questions table — one administrator-authored objective question (P4, ADR-006).

A question always belongs to ONE exercise-set unit of ONE revision. The
strongest practical FK pair — ``(book_unit_id, book_revision_id) ->
book_units(id, book_revision_id)`` — makes "a question grafted from one
edition onto another" impossible for every future writer, mirroring the
``book_units`` self-FK pattern; the shared ``book_units_id_revision_key``
constraint is its target.

Integrity mechanisms:

- the COMPOSITE FK above binds the question to its unit AND that unit's
  revision in one constraint;
- ``prompt`` and ``explanation`` are never blank (named CHECKs) — the
  explanation is the corrective feedback shown for a wrong answer, so it
  must exist from creation;
- answer-key verification is a durable two-column lockstep (reviewer id
  + timestamp, both NULL or both set, paired CHECKs exactly like
  ``book_units`` review state): correctness is never inferred, only an
  explicit administrator verification records it;
- sibling ``display_order`` is unique per unit via a plain unique index
  (``book_unit_id`` is NOT NULL, so no partial index is needed).

Questions and their options are copied (fresh ids, verification reset) by
``create_correction_revision``; source revision rows are never written.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import BookQuestionType, sql_in_list


class BookQuestion(TimestampMixin, Base):
    """One objective (multiple-choice / true-false) question."""

    __tablename__ = "book_questions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    book_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("book_revisions.id"), nullable=False
    )
    book_unit_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    question_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The question text, verbatim from the administrator — never
    #: generated or translated. Never blank.
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    #: Corrective feedback shown when a student answers wrongly. Never
    #: blank — it exists from creation so a graded attempt can always
    #: explain a miss.
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    #: Sibling order within the exercise set.
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    #: WHO confirmed the answer key (NULL while unverified); NO ACTION
    #: FK like the rest of the book family — history is never cascaded.
    answer_key_verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    #: WHEN the answer key was confirmed (NULL while unverified).
    answer_key_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "question_type IN (" + sql_in_list(BookQuestionType) + ")",
            name="book_questions_question_type_check",
        ),
        CheckConstraint("prompt <> ''", name="book_questions_prompt_check"),
        CheckConstraint(
            "explanation <> ''", name="book_questions_explanation_check"
        ),
        CheckConstraint(
            "display_order >= 1", name="book_questions_display_order_check"
        ),
        CheckConstraint(
            "answer_key_verified_by_user_id IS NULL "
            "OR answer_key_verified_at IS NOT NULL",
            name="book_questions_verified_check",
        ),
        CheckConstraint(
            "answer_key_verified_at IS NULL "
            "OR answer_key_verified_by_user_id IS NOT NULL",
            name="book_questions_unverified_check",
        ),
        # FK target for the composite FK below (same pattern as book_units).
        UniqueConstraint(
            "id", "book_revision_id", name="book_questions_id_revision_key"
        ),
        ForeignKeyConstraint(
            ["book_unit_id", "book_revision_id"],
            ["book_units.id", "book_units.book_revision_id"],
            name="book_questions_book_unit_id_fkey",
        ),
        # One order slot per exercise set.
        Index(
            "uq_book_questions_unit_order_key",
            "book_unit_id",
            "display_order",
            unique=True,
        ),
        Index("book_questions_book_revision_id_idx", "book_revision_id"),
        Index("book_questions_book_unit_id_idx", "book_unit_id"),
        # Reviewer FK column carries its own index (0016/0019 house rule).
        Index(
            "book_questions_answer_key_verified_by_user_id_idx",
            "answer_key_verified_by_user_id",
        ),
    )

    revision = relationship("BookRevision", viewonly=True)
    unit = relationship("BookUnit", viewonly=True, overlaps="revision")
    options = relationship(
        "BookQuestionOption",
        back_populates="question",
        order_by="BookQuestionOption.display_order.asc()",
        cascade="all, delete-orphan",
    )
