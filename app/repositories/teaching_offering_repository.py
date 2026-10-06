"""Data access for ``teaching_offerings``.

Reads are always eager-loaded (context, its catalog rows and the owning
teacher in one statement) so a page of offerings never costs one query per
row; writes are ``flush``ed, never committed.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.enums import TeacherVerificationStatus, TeachingOfferingStatus
from app.models.learning_context import LearningContext
from app.models.program_version import ProgramVersion
from app.models.teacher import Teacher
from app.models.teaching_offering import TeachingOffering

#: Everything a teaching-offering summary reads, as loader options: the
#: context and its five catalog relations (many-to-one, joined — no extra
#: round-trips) plus the owning teacher.
EAGER_OPTIONS = (
    joinedload(TeachingOffering.learning_context).joinedload(LearningContext.academic_year),
    joinedload(TeachingOffering.learning_context).joinedload(LearningContext.pathway),
    joinedload(TeachingOffering.learning_context).joinedload(LearningContext.education_level),
    joinedload(TeachingOffering.learning_context)
    .joinedload(LearningContext.program_version)
    .joinedload(ProgramVersion.program),
    joinedload(TeachingOffering.learning_context).joinedload(LearningContext.subject),
    joinedload(TeachingOffering.teacher),
)


def get_by_id(session: Session, offering_id: uuid.UUID) -> TeachingOffering | None:
    """One offering with its full context loaded (or None)."""
    stmt = (
        select(TeachingOffering)
        .options(*EAGER_OPTIONS)
        .where(TeachingOffering.id == offering_id)
    )
    return session.execute(stmt).unique().scalar_one_or_none()


def reload_with_relations(
    session: Session, offering_id: uuid.UUID
) -> TeachingOffering | None:
    """Re-read one offering through :data:`EAGER_OPTIONS` after a write.

    ``populate_existing`` refreshes already-bound attributes (including
    ``status``/``description``) from the eager query, so the summary built
    from the row never triggers a lazy load.
    """
    stmt = (
        select(TeachingOffering)
        .options(*EAGER_OPTIONS)
        .where(TeachingOffering.id == offering_id)
        .execution_options(populate_existing=True)
    )
    return session.execute(stmt).unique().scalar_one_or_none()


def get_by_id_for_update(
    session: Session, offering_id: uuid.UUID
) -> TeachingOffering | None:
    """One offering with its row locked (``SELECT ... FOR UPDATE``).

    A locking read must not take locks on the context or catalog tables
    (joinedload would), so this is a plain single-table statement; the
    relations come back through :func:`reload_with_relations`.
    """
    stmt = (
        select(TeachingOffering)
        .where(TeachingOffering.id == offering_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalar(stmt)


def find_active_for_teacher_context(
    session: Session, teacher_id: uuid.UUID, learning_context_id: uuid.UUID
) -> TeachingOffering | None:
    """The teacher's *active* offering of one context, if any.

    Mirrors the partial unique index
    ``uq_teaching_offerings_teacher_context_active_key`` so the service can
    answer 409 before the INSERT runs; the index stays the final
    protection under a race.
    """
    stmt = select(TeachingOffering).where(
        TeachingOffering.teacher_id == teacher_id,
        TeachingOffering.learning_context_id == learning_context_id,
        TeachingOffering.status == TeachingOfferingStatus.ACTIVE.value,
    )
    return session.scalar(stmt)


def list_for_teacher(
    session: Session, teacher_id: uuid.UUID
) -> list[TeachingOffering]:
    """Every offering the teacher owns, newest first (eager-loaded)."""
    stmt = (
        select(TeachingOffering)
        .options(*EAGER_OPTIONS)
        .where(TeachingOffering.teacher_id == teacher_id)
        .order_by(TeachingOffering.created_at.desc(), TeachingOffering.id)
    )
    return list(session.execute(stmt).unique().scalars())


def _marketplace_filters(
    *,
    academic_year_id: uuid.UUID | None,
    pathway_id: uuid.UUID | None,
    education_level_id: uuid.UUID | None,
    subject_id: uuid.UUID | None,
) -> list:
    """Optional catalog filters, applied through the context relation."""
    conditions = []
    if academic_year_id is not None:
        conditions.append(LearningContext.academic_year_id == academic_year_id)
    if pathway_id is not None:
        conditions.append(LearningContext.pathway_id == pathway_id)
    if education_level_id is not None:
        conditions.append(LearningContext.education_level_id == education_level_id)
    if subject_id is not None:
        conditions.append(LearningContext.subject_id == subject_id)
    return conditions


def list_active_for_marketplace(
    session: Session,
    *,
    academic_year_id: uuid.UUID | None = None,
    pathway_id: uuid.UUID | None = None,
    education_level_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[TeachingOffering]:
    """Discoverable offerings: active, taught by an *approved* teacher.

    The verification join is the marketplace's enforcement of the vetting
    decision — suspending a teacher hides their offerings immediately,
    without touching the rows. Ordering is deterministic (created_at
    descending with the unique id as tiebreaker) so offset paging is
    stable while a whole batch shares one timestamp.
    """
    stmt = (
        select(TeachingOffering)
        .join(Teacher, Teacher.id == TeachingOffering.teacher_id)
        .join(
            LearningContext,
            TeachingOffering.learning_context_id == LearningContext.id,
        )
        .options(*EAGER_OPTIONS)
        .where(
            TeachingOffering.status == TeachingOfferingStatus.ACTIVE.value,
            Teacher.verification_status == TeacherVerificationStatus.APPROVED.value,
            *_marketplace_filters(
                academic_year_id=academic_year_id,
                pathway_id=pathway_id,
                education_level_id=education_level_id,
                subject_id=subject_id,
            ),
        )
        .order_by(TeachingOffering.created_at.desc(), TeachingOffering.id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(stmt).unique().scalars())


def create(
    session: Session,
    *,
    teacher_id: uuid.UUID,
    learning_context_id: uuid.UUID,
    description: str | None = None,
) -> TeachingOffering:
    """Insert one offering row (flushed, not committed), status ``active``."""
    offering = TeachingOffering(
        teacher_id=teacher_id,
        learning_context_id=learning_context_id,
        description=description,
        status=TeachingOfferingStatus.ACTIVE.value,
    )
    session.add(offering)
    session.flush()  # assign the PK so the audit event can name it
    return offering
