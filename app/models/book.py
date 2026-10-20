"""books table — stable identity and administrative metadata for one book.

A book is an administrator-managed entry in the platform book library
(ADR-001): it is NOT a teacher material and never hangs off a teaching
offering. What a book teaches is pinned by its revisions' canonical
``learning_contexts`` associations, never by free-text pathway/level/
subject fields.

Each uploaded or corrected edition is a ``book_revisions`` row; the book
row itself carries only identity and administrative metadata. Books are
never hard-deleted: historical revisions, source assets and audit rows
must survive for learning-record investigations (ADR-005), so the FK to
the creating user uses the database's default NO ACTION behaviour rather
than a cascade.
"""
import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class Book(TimestampMixin, Base):
    """One title in the administrator-managed book library."""

    __tablename__ = "books"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )

    __table_args__ = (
        Index("books_created_by_user_id_idx", "created_by_user_id"),
    )

    created_by = relationship("User")
    revisions = relationship(
        "BookRevision",
        back_populates="book",
        order_by="BookRevision.revision_number.desc()",
    )
