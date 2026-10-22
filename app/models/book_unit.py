"""book_units table — the extracted hierarchy of one book revision (P2, ADR-004).

Each row is ONE structural unit — chapter, topic, explanation, example or
exercise set — parsed from the revision's source PDF, carrying its
source-derived title, extracted text, sibling ``display_order``, the
1-based source page range it came from, any fixed-code extraction
warnings and its (currently always ``unresolved``) assessment
requirement. The tree is a staging area for administrative review (P3):
no row is ever student-visible in P2.

Two integrity mechanisms carry most of the weight:

- a COMPOSITE self-FK ``(parent_unit_id, book_revision_id) -> (id,
  book_revision_id)`` — a child can only ever reference a parent of the
  SAME revision, enforced by the database against every future writer
  (a bug can never graft a chapter of one edition under a topic of
  another);
- two PARTIAL unique indexes on sibling order — one for children, one
  for roots — so (revision, parent, display_order) can never collide
  even for root units where ``parent_unit_id`` is NULL (a plain UNIQUE
  constraint ignores NULLs and would leave roots unprotected).

The FK target needs ``UNIQUE (id, book_revision_id)`` to exist; the
shared ``book_units_id_revision_key`` constraint is exactly that. Source
FKs stay NO ACTION like the rest of the book family: historical
revisions are never cascade-deleted.
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
from app.models.enums import (
    AssessmentRequirement,
    BookUnitKind,
    BookUnitReviewStatus,
    sql_in_list,
)


class BookUnit(TimestampMixin, Base):
    """One extracted structural unit of one book revision."""

    __tablename__ = "book_units"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    book_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("book_revisions.id"), nullable=False
    )
    parent_unit_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The heading line this unit was recognized by (verbatim, trimmed) —
    #: never a generated or translated title.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: Extracted text; '' for a pure container that owns no text of its
    #: own. Concatenating every unit's content in document order must
    #: reproduce the source text (bar recognized heading lines), so no
    #: input line can vanish silently.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    source_page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Fixed warning codes (see ``app.services.book_extraction``),
    #: newline-separated; NULL when the unit parsed cleanly. Approval
    #: never clears these: P3 keeps them visible on approved units too.
    extraction_warnings: Mapped[str | None] = mapped_column(Text, nullable=True)
    assessment_requirement: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AssessmentRequirement.UNRESOLVED.value,
    )
    #: P3 durable per-unit review state: starts ``pending``, becomes
    #: ``approved`` only through an explicit administrator approval, and
    #: any correction (of this unit or a structural ancestor) resets it —
    #: the paired CHECKs below keep the reviewer identity columns in
    #: lockstep so a reset can never leave stale identity behind.
    review_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=BookUnitReviewStatus.PENDING.value,
    )
    #: WHO approved this unit (NULL while pending); FK uses NO ACTION
    #: like the rest of the book family — review history is never
    #: cascade-deleted.
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    #: WHEN the approval happened (NULL while pending).
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("kind IN (" + sql_in_list(BookUnitKind) + ")", name="book_units_kind_check"),
        CheckConstraint("title <> ''", name="book_units_title_check"),
        CheckConstraint("display_order >= 1", name="book_units_display_order_check"),
        CheckConstraint("source_page_start >= 1", name="book_units_page_start_check"),
        CheckConstraint(
            "source_page_end >= source_page_start",
            name="book_units_page_range_check",
        ),
        CheckConstraint(
            "assessment_requirement IN (" + sql_in_list(AssessmentRequirement) + ")",
            name="book_units_assessment_requirement_check",
        ),
        CheckConstraint(
            "review_status IN (" + sql_in_list(BookUnitReviewStatus) + ")",
            name="book_units_review_status_check",
        ),
        CheckConstraint(
            "review_status <> 'approved' OR (reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="book_units_review_approved_check",
        ),
        CheckConstraint(
            "review_status <> 'pending' OR (reviewed_by_user_id IS NULL AND reviewed_at IS NULL)",
            name="book_units_review_pending_check",
        ),
        # FK target for the composite self-FK below.
        UniqueConstraint("id", "book_revision_id", name="book_units_id_revision_key"),
        ForeignKeyConstraint(
            ["parent_unit_id", "book_revision_id"],
            ["book_units.id", "book_units.book_revision_id"],
            name="book_units_parent_unit_id_fkey",
        ),
        Index("book_units_book_revision_id_idx", "book_revision_id"),
        Index("book_units_parent_unit_id_idx", "parent_unit_id"),
        # The reviewer FK column carries its own index (0016/0019 house
        # rule: PostgreSQL does not index foreign keys automatically).
        Index("book_units_reviewed_by_user_id_idx", "reviewed_by_user_id"),
        # Sibling order is a durable invariant: children and roots get one
        # PARTIAL unique index each (NULL parent is excluded from a plain
        # UNIQUE), with sqlite_where kept alongside postgresql_where so the
        # scratch-SQLite metadata checks and PostgreSQL see the same DDL.
        Index(
            "uq_book_units_revision_parent_order_key",
            "book_revision_id",
            "parent_unit_id",
            "display_order",
            unique=True,
            postgresql_where=text("parent_unit_id IS NOT NULL"),
            sqlite_where=text("parent_unit_id IS NOT NULL"),
        ),
        Index(
            "uq_book_units_revision_root_order_key",
            "book_revision_id",
            "display_order",
            unique=True,
            postgresql_where=text("parent_unit_id IS NULL"),
            sqlite_where=text("parent_unit_id IS NULL"),
        ),
    )

    revision = relationship("BookRevision", back_populates="units")
