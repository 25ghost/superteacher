"""book_revisions table — one uploaded edition of one book.

A revision owns the uploaded source PDF (via a ``file_assets`` row), its
sequential edition number within the book, the intake/processing status
and the administrator who uploaded it. P1 only ever creates revision 1 in
``uploaded`` state; the status vocabulary and the unique edition numbering
exist now so extraction, review, publication and republication extend
this table instead of reshaping it (ADR-004/ADR-005).

Source assets are history: the FKs to ``file_assets`` and ``users`` use
the default NO ACTION behaviour so a published or historical revision can
never be silently deleted by a cascade.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import BookRevisionStatus, sql_in_list


class BookRevision(TimestampMixin, Base):
    """One edition of one book, with its source asset and lifecycle state."""

    __tablename__ = "book_revisions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    book_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("books.id"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    file_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("file_assets.id"), nullable=False
    )
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BookRevisionStatus.UPLOADED.value
    )

    __table_args__ = (
        CheckConstraint(
            "revision_number >= 1",
            name="book_revisions_revision_number_check",
        ),
        CheckConstraint(
            f"status IN ({sql_in_list(BookRevisionStatus)})",
            name="book_revisions_status_check",
        ),
        # One edition number per book: a republication can never collide
        # with (or silently overwrite) an earlier edition.
        Index(
            "uq_book_revisions_book_revision_number_key",
            "book_id",
            "revision_number",
            unique=True,
        ),
        Index("book_revisions_book_id_idx", "book_id"),
        Index("book_revisions_file_asset_id_idx", "file_asset_id"),
        Index("book_revisions_uploaded_by_user_id_idx", "uploaded_by_user_id"),
        Index("book_revisions_status_idx", "status"),
    )

    book = relationship("Book", back_populates="revisions")
    file_asset = relationship("FileAsset")
    uploaded_by = relationship("User")
    contexts = relationship(
        "BookRevisionContext",
        back_populates="revision",
        order_by="BookRevisionContext.created_at.asc()",
    )
