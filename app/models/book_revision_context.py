"""book_revision_contexts — ONE canonical learning context per row.

Book eligibility is exact canonical context matching (ADR-002): a
revision is valid for the explicit ``learning_contexts`` rows an
administrator associated at intake, never for a context inferred from a
title or subject-text resemblance. Each association is its own row so the
later student access chain (P5) can match a requested context against the
revision's context set with a plain indexed join.

The default NO ACTION FK behaviour applies: a revision or context that is
referenced here cannot be hard-deleted out from under the association.
"""
import uuid

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class BookRevisionContext(TimestampMixin, Base):
    """One revision × one canonical learning context it is valid for."""

    __tablename__ = "book_revision_contexts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    book_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("book_revisions.id"), nullable=False
    )
    learning_context_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_contexts.id"), nullable=False
    )

    __table_args__ = (
        # The same context may never be attached to the same revision
        # twice, whatever path created the rows.
        Index(
            "uq_book_revision_contexts_revision_context_key",
            "book_revision_id",
            "learning_context_id",
            unique=True,
        ),
        # The revision id leads the unique index above; the reverse
        # lookup ("which revisions are valid for this context?") needs
        # its own index.
        Index(
            "book_revision_contexts_context_id_idx", "learning_context_id"
        ),
    )

    revision = relationship("BookRevision", back_populates="contexts")
    learning_context = relationship("LearningContext")
