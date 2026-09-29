"""pathway_levels table — many-to-many mapping between pathways and levels."""
import uuid

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PathwayLevel(Base):
    """Associates a pathway with the education levels it spans.

    Seed relationships (data, not logic): O_LEVEL -> S1,S2,S3;
    A_LEVEL -> S4,S5,S6; TVET -> L3,L4,L5.
    """

    __tablename__ = "pathway_levels"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pathway_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pathways.id"), nullable=False
    )
    education_level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("education_levels.id"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "pathway_id", "education_level_id",
            name="uq_pathway_levels_pathway_id_education_level_id_key",
        ),
        # pathway_id is the leading column of the unique constraint above;
        # education_level_id is trailing and needs its own index for the
        # reverse lookup ("which pathways contain this level?").
        Index("pathway_levels_education_level_id_idx", "education_level_id"),
    )

    pathway = relationship("Pathway", back_populates="levels")
    education_level = relationship("EducationLevel", back_populates="pathways")
