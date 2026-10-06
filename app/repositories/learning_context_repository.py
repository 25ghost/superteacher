"""Data access for ``learning_contexts``.

Insert/lookup helpers only — whether a pathway spans a level, whether a
program version belongs to the tuple or whether a subject is taught by
that program version are **service** decisions (the same Option-A split as
registration); this module only decides *how* rows are written and found.

All inserts are ``flush``ed, never committed: the request-scoped session
remains the single transaction owner.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.learning_context import LearningContext
from app.models.program_version import ProgramVersion

#: Catalog relations every context read needs, loaded in one statement.
EAGER_OPTIONS = (
    joinedload(LearningContext.academic_year),
    joinedload(LearningContext.pathway),
    joinedload(LearningContext.education_level),
    joinedload(LearningContext.program_version).joinedload(ProgramVersion.program),
    joinedload(LearningContext.subject),
)


def get_by_id(
    session: Session, learning_context_id: uuid.UUID
) -> LearningContext | None:
    """One context with its catalog relations loaded (or None)."""
    stmt = (
        select(LearningContext)
        .options(*EAGER_OPTIONS)
        .where(LearningContext.id == learning_context_id)
    )
    return session.execute(stmt).unique().scalar_one_or_none()


def get_by_natural_key(
    session: Session,
    *,
    academic_year_id: uuid.UUID,
    pathway_id: uuid.UUID,
    education_level_id: uuid.UUID,
    program_version_id: uuid.UUID | None,
    subject_id: uuid.UUID,
) -> LearningContext | None:
    """The context for one (year, pathway, level, program?, subject) tuple.

    NULL program versions are compared with ``IS NULL`` rather than ``==``
    (SQL's three-valued logic would never match) — the same two cases the
    partial unique indexes split the key into.
    """
    conditions = [
        LearningContext.academic_year_id == academic_year_id,
        LearningContext.pathway_id == pathway_id,
        LearningContext.education_level_id == education_level_id,
        LearningContext.subject_id == subject_id,
    ]
    if program_version_id is None:
        conditions.append(LearningContext.program_version_id.is_(None))
    else:
        conditions.append(LearningContext.program_version_id == program_version_id)
    stmt = select(LearningContext).where(*conditions)
    return session.scalar(stmt)


def create(
    session: Session,
    *,
    academic_year_id: uuid.UUID,
    pathway_id: uuid.UUID,
    education_level_id: uuid.UUID,
    program_version_id: uuid.UUID | None,
    subject_id: uuid.UUID,
) -> LearningContext:
    """Insert one context row (flushed, not committed).

    The caller must have resolved the natural key first; a concurrent
    duplicate loses the race on the partial unique index and is turned
    into a re-read by the service.
    """
    context = LearningContext(
        academic_year_id=academic_year_id,
        pathway_id=pathway_id,
        education_level_id=education_level_id,
        program_version_id=program_version_id,
        subject_id=subject_id,
    )
    session.add(context)
    session.flush()  # assign the PK so the offering can link to it
    return context
