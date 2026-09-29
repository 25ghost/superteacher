"""Data access for ``student_enrollments`` and ``student_subjects``.

Insert/lookup helpers only — no HTTP, no business rules. *Whether* a
pathway fits a level, whether a school offers a program version, or
whether registration is open are **service** decisions (Phase 3C
Option-A architecture); this module only decides *how* rows are written
and found.

All inserts are ``flush``ed, never committed: the request-scoped session
(the one ``get_db`` yields) remains the single transaction owner, and the
API layer commits on success or rolls back on failure. Nothing here may
open a second session or transaction.

Ordering contract: ``list_for_student`` returns the student's enrollment
history newest academic year first (year ``start_date`` descending), then
creation time descending — deterministic for API consumers.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.academic_year import AcademicYear
from app.models.enrollment import StudentEnrollment
from app.models.student_subject import StudentSubject
from app.models.subject import Subject


def get_by_id(session: Session, enrollment_id: uuid.UUID) -> StudentEnrollment | None:
    """One enrollment with its full catalog context loaded (or None).

    The five catalog relations are many-to-one, so ``joinedload`` fetches
    them in the same query (single row, no extra round-trips); the one-to-many
    ``subjects`` (plus each subject) uses ``selectinload``.
    """
    stmt = (
        select(StudentEnrollment)
        .options(
            joinedload(StudentEnrollment.academic_year),
            joinedload(StudentEnrollment.pathway),
            joinedload(StudentEnrollment.education_level),
            joinedload(StudentEnrollment.program_version),
            joinedload(StudentEnrollment.school),
            selectinload(StudentEnrollment.subjects).selectinload(StudentSubject.subject),
        )
        .where(StudentEnrollment.id == enrollment_id)
    )
    return session.execute(stmt).unique().scalar_one_or_none()


def find_existing(
    session: Session,
    student_id: uuid.UUID,
    academic_year_id: uuid.UUID,
) -> StudentEnrollment | None:
    """The student's enrollment for one academic year, if any.

    Mirrors the database uniqueness constraint
    ``uq_student_enrollments_student_id_academic_year_id_key`` so the
    service can return a useful 409 before the INSERT even runs; the
    constraint itself stays the final integrity protection.
    """
    stmt = select(StudentEnrollment).where(
        StudentEnrollment.student_id == student_id,
        StudentEnrollment.academic_year_id == academic_year_id,
    )
    return session.scalar(stmt)


def list_for_student(session: Session, student_id: uuid.UUID) -> list[StudentEnrollment]:
    """Enrollment history of one student, newest academic year first.

    Same load strategy as :func:`get_by_id`: joined scalar relations,
    selectin collections.
    """
    stmt = (
        select(StudentEnrollment)
        .options(
            joinedload(StudentEnrollment.academic_year),
            joinedload(StudentEnrollment.pathway),
            joinedload(StudentEnrollment.education_level),
            joinedload(StudentEnrollment.program_version),
            joinedload(StudentEnrollment.school),
            selectinload(StudentEnrollment.subjects).selectinload(StudentSubject.subject),
        )
        .join(AcademicYear, AcademicYear.id == StudentEnrollment.academic_year_id)
        .where(StudentEnrollment.student_id == student_id)
        .order_by(AcademicYear.start_date.desc(), StudentEnrollment.created_at.desc())
    )
    return list(session.execute(stmt).unique().scalars())


def create(
    session: Session,
    *,
    student_id: uuid.UUID,
    academic_year_id: uuid.UUID,
    school_id: uuid.UUID,
    pathway_id: uuid.UUID,
    education_level_id: uuid.UUID,
    program_version_id: uuid.UUID | None,
    status: str,
) -> StudentEnrollment:
    """Insert one enrollment row (flushed, not committed)."""
    enrollment = StudentEnrollment(
        student_id=student_id,
        academic_year_id=academic_year_id,
        school_id=school_id,
        pathway_id=pathway_id,
        education_level_id=education_level_id,
        program_version_id=program_version_id,
        status=status,
    )
    session.add(enrollment)
    session.flush()  # assign the PK so student_subject rows can link to it
    return enrollment


def create_student_subjects(
    session: Session,
    *,
    enrollment_id: uuid.UUID,
    subject_ids: list[uuid.UUID],
    status: str,
) -> list[StudentSubject]:
    """Insert ``student_subjects`` rows for one enrollment (flushed, not committed)."""
    rows = [
        StudentSubject(
            enrollment_id=enrollment_id,
            subject_id=subject_id,
            status=status,
        )
        for subject_id in subject_ids
    ]
    session.add_all(rows)
    session.flush()
    return rows


def subject_ids_for_program_version(
    session: Session,
    program_version_id: uuid.UUID,
) -> list[tuple[uuid.UUID, str, str]]:
    """Subjects of one program version as ``(subject_id, code, name)``.

    The authoritative mapping is ``program_version → program_subjects →
    subjects``; the generic ``subjects`` table alone is never used to
    derive a student's subjects.
    """
    from app.models.program_subject import ProgramSubject

    stmt = (
        select(Subject.id, Subject.code, Subject.name)
        .join(ProgramSubject, ProgramSubject.subject_id == Subject.id)
        .where(ProgramSubject.program_version_id == program_version_id)
        .order_by(ProgramSubject.display_order, Subject.code)
    )
    return [(row.id, row.code, row.name) for row in session.execute(stmt)]
