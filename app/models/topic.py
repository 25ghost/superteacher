"""topics table — one curriculum unit beneath a teaching offering.

A topic is the teacher's own outline section inside ONE offering: it does
not duplicate the Admin-owned catalog (``learning_contexts`` answers
*what* is taught; a topic answers *how this teacher structures it*). Every
topic therefore hangs off a ``teaching_offerings`` row and stays inside
that offering's educational context.

Ordering is a domain invariant, not decoration: ``display_order`` is unique
within the offering (``uq_topics_teaching_offering_display_order_key``) and
must be >= 1, so the sequence a student will eventually read is stored,
not recomputed. Creating a topic assigns the next free position; a PATCH
may move it, but only onto a position nobody else holds.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin


class Topic(TimestampMixin, Base):
    """One teacher-authored topic inside one of their teaching offerings."""

    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    teaching_offering_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teaching_offerings.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default=text("1")
    )

    __table_args__ = (
        # Unique ordering is a *partial-free* full index, declared the same
        # way Phase 1 declares its offering uniqueness (Index, not
        # UniqueConstraint) so the migration and the ORM agree on shape.
        Index(
            "uq_topics_teaching_offering_display_order_key",
            "teaching_offering_id",
            "display_order",
            unique=True,
        ),
        CheckConstraint(
            "display_order >= 1",
            name="topics_display_order_check",
        ),
        Index("topics_teaching_offering_id_idx", "teaching_offering_id"),
    )

    teaching_offering = relationship(
        "TeachingOffering", back_populates="topics"
    )
    lessons = relationship(
        "Lesson",
        back_populates="topic",
        cascade="all, delete-orphan",
    )
