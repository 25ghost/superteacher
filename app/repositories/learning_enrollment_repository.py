"""Data access for ``learning_enrollments``.

The one rule this module mirrors from the schema is the partial unique
index ``uq_learning_enrollments_student_context_active_key``: at most one
*active* enrollment per (student, learning context). The pre-check below
lets the service answer 409 with a readable message; the index stays the
final protection. Writes are ``flush``ed, never committed.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.enums import LearningEnrollmentStatus
from app.models.learning_context import LearningContext
from app.models.learning_enrollment import LearningEnrollment
from app.models.program_version import ProgramVersion
from app.models.teaching_offering import TeachingOffering

#: Everything an enrollment summary reads: the context and its catalog rows
#: (via the enrollment's own context FK) plus the offering and its teacher.
EAGER_OPTIONS = (
    joinedload(LearningEnrollment.learning_context).joinedload(LearningContext.academic_year),
    joinedload(LearningEnrollment.learning_context).joinedload(LearningContext.pathway),
    joinedload(LearningEnrollment.learning_context).joinedload(LearningContext.education_level),
    joinedload(LearningEnrollment.learning_context)
    .joinedload(LearningContext.program_version)
    .joinedload(ProgramVersion.program),
    joinedload(LearningEnrollment.learning_context).joinedload(LearningContext.subject),
    joinedload(LearningEnrollment.teaching_offering).joinedload(TeachingOffering.teacher),
)


def get_by_id(
    session: Session, enrollment_id: uuid.UUID
) -> LearningEnrollment | None:
    """One enrollment with its full context and offering loaded (or None)."""
    stmt = (
        select(LearningEnrollment)
        .options(*EAGER_OPTIONS)
        .where(LearningEnrollment.id == enrollment_id)
    )
    return session.execute(stmt).unique().scalar_one_or_none()


def get_by_id_for_update(
    session: Session, enrollment_id: uuid.UUID
) -> LearningEnrollment | None:
    """One enrollment with its row locked (``SELECT ... FOR UPDATE``).

    A locking read must not take row locks on the offering/context/catalog
    tables (joinedload would), and ``populate_existing`` makes the lock
    read authoritative — the status the leave check compares is the one the
    lock protects, not an earlier snapshot in the identity map.
    """
    stmt = (
        select(LearningEnrollment)
        .where(LearningEnrollment.id == enrollment_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalar(stmt)


def reload_with_relations(
    session: Session, enrollment_id: uuid.UUID
) -> LearningEnrollment | None:
    """Re-read one enrollment through :data:`EAGER_OPTIONS` after a write."""
    stmt = (
        select(LearningEnrollment)
        .options(*EAGER_OPTIONS)
        .where(LearningEnrollment.id == enrollment_id)
        .execution_options(populate_existing=True)
    )
    return session.execute(stmt).unique().scalar_one_or_none()


def find_active_for_student_context(
    session: Session, student_id: uuid.UUID, learning_context_id: uuid.UUID
) -> LearningEnrollment | None:
    """The student's *active* enrollment in one context, if any.

    Keys on the context rather than the offering: this is the "one active
    enrollment per context" rule that forces an explicit leave before
    switching teachers.
    """
    stmt = select(LearningEnrollment).where(
        LearningEnrollment.student_id == student_id,
        LearningEnrollment.learning_context_id == learning_context_id,
        LearningEnrollment.status == LearningEnrollmentStatus.ACTIVE.value,
    )
    return session.scalar(stmt)


def find_active_for_student_offering(
    session: Session, student_id: uuid.UUID, teaching_offering_id: uuid.UUID
) -> LearningEnrollment | None:
    """The student's *active* enrollment in one teaching offering, if any.

    Slice 2C authorization keys on the *offering* (not the context): a
    student may only read content belonging to the offering they are
    actively enrolled in — another teacher's offering of the same context
    answers exactly like an unknown material (L6 existence leak).
    """
    stmt = select(LearningEnrollment).where(
        LearningEnrollment.student_id == student_id,
        LearningEnrollment.teaching_offering_id == teaching_offering_id,
        LearningEnrollment.status == LearningEnrollmentStatus.ACTIVE.value,
    )
    return session.scalar(stmt)


def list_for_student(
    session: Session, student_id: uuid.UUID
) -> list[LearningEnrollment]:
    """The student's learning history, newest first (eager-loaded)."""
    stmt = (
        select(LearningEnrollment)
        .options(*EAGER_OPTIONS)
        .where(LearningEnrollment.student_id == student_id)
        .order_by(LearningEnrollment.created_at.desc(), LearningEnrollment.id)
    )
    return list(session.execute(stmt).unique().scalars())


def create(
    session: Session,
    *,
    student_id: uuid.UUID,
    teaching_offering_id: uuid.UUID,
    learning_context_id: uuid.UUID,
) -> LearningEnrollment:
    """Insert one active enrollment (flushed, not committed)."""
    enrollment = LearningEnrollment(
        student_id=student_id,
        teaching_offering_id=teaching_offering_id,
        learning_context_id=learning_context_id,
        status=LearningEnrollmentStatus.ACTIVE.value,
    )
    session.add(enrollment)
    session.flush()  # assign the PK so the audit event can name it
    return enrollment
