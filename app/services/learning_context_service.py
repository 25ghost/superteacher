"""Learning contexts: catalog validation and the shared Phase 1 error family.

A learning context is the teachable unit — ``(academic year, pathway,
education level, program version?, subject)`` — that a teaching offering
publishes and a learning enrollment joins. Teachers never POST a context
directly: creating an offering resolves the tuple here and inserts the row
the first time it is seen, so every teacher offering the same tuple and
every student enrolled in it share one row (and therefore one
"one active enrollment per context" rule).

This module also owns the Phase 1 error family
(:class:`LearningError` and subclasses), which the offering and enrollment
services import: one vocabulary, one mapping onto HTTP statuses for the
whole marketplace.

Validation mirrors ``registration_service`` deliberately — the same
coherence rules (pathway spans the level, program version belongs to the
tuple, the subject is actually taught by that program version) expressed
with ids instead of codes, so a context can never describe an impossible
teaching assignment.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.education_level import EducationLevel
from app.models.learning_context import LearningContext
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.subject import Subject
from app.repositories import learning_context_repository as context_repo
from app.schemas.learning import LearningContextRead, TeachingOfferingCreate


class LearningError(Exception):
    """Base class: ``.status_code`` tells the API layer which HTTP status to emit."""

    status_code = 400


class LearningValidationError(LearningError):
    """Incoherent context (pathway/level mismatch, foreign program, ...)."""

    status_code = 422


class LearningConflictError(LearningError):
    """The request is understood but collides with existing state."""

    status_code = 409


class LearningNotFoundError(LearningError):
    """A referenced row does not exist (404)."""

    status_code = 404


class LearningForbiddenError(LearningError):
    """Authenticated, but not allowed (unverified teacher, foreign row)."""

    status_code = 403


# --- catalog resolution -------------------------------------------------------------


def _resolve_year(session: Session, academic_year_id: uuid.UUID) -> AcademicYear:
    year = session.get(AcademicYear, academic_year_id)
    if year is None:
        raise LearningNotFoundError(f"no academic year with id {academic_year_id}")
    if year.status == "archived":
        raise LearningValidationError(
            f"academic year {year.name!r} is archived and cannot host new offerings"
        )
    return year


def _resolve_pathway(session: Session, pathway_id: uuid.UUID) -> Pathway:
    pathway = session.get(Pathway, pathway_id)
    if pathway is None:
        raise LearningNotFoundError(f"no pathway with id {pathway_id}")
    return pathway


def _resolve_level(session: Session, education_level_id: uuid.UUID) -> EducationLevel:
    level = session.get(EducationLevel, education_level_id)
    if level is None:
        raise LearningNotFoundError(f"no education level with id {education_level_id}")
    return level


def _validate_pathway_level(session: Session, pathway: Pathway, level: EducationLevel) -> None:
    """The pathway must actually span the level (a real pathway_levels row)."""
    link = session.scalars(
        select(PathwayLevel).where(
            PathwayLevel.pathway_id == pathway.id,
            PathwayLevel.education_level_id == level.id,
        )
    ).first()
    if link is None:
        raise LearningValidationError(
            f"pathway {pathway.code!r} does not include education level {level.code!r}"
        )


def _resolve_subject(session: Session, subject_id: uuid.UUID) -> Subject:
    subject = session.get(Subject, subject_id)
    if subject is None:
        raise LearningNotFoundError(f"no subject with id {subject_id}")
    return subject


def _resolve_program_version(
    session: Session,
    program_version_id: uuid.UUID,
    year: AcademicYear,
    pathway: Pathway,
    level: EducationLevel,
) -> ProgramVersion:
    """Load the program version and prove it belongs to the offering context."""
    program_version = session.get(ProgramVersion, program_version_id)
    if program_version is None:
        raise LearningNotFoundError(
            f"no program version with id {program_version_id}"
        )
    if (
        program_version.academic_year_id != year.id
        or program_version.pathway_id != pathway.id
        or program_version.education_level_id != level.id
    ):
        raise LearningValidationError(
            "program version does not belong to the selected academic "
            "year / pathway / education level"
        )
    if program_version.status != "active":
        raise LearningValidationError(
            f"program version {program_version.code!r} is "
            f"{program_version.status!r} and cannot be taught"
        )
    return program_version


def _validate_subject_taught(
    session: Session, program_version: ProgramVersion, subject: Subject
) -> None:
    """A subject belongs to a program version only via program_subjects."""
    row = session.scalars(
        select(ProgramSubject).where(
            ProgramSubject.program_version_id == program_version.id,
            ProgramSubject.subject_id == subject.id,
        )
    ).first()
    if row is None:
        raise LearningValidationError(
            f"subject {subject.code!r} is not part of program version "
            f"{program_version.code!r}"
        )


# --- read projection ----------------------------------------------------------------


def read_context(context: LearningContext) -> LearningContextRead:
    """The flattened catalog summary of one context (relations must be eager)."""
    program_version = context.program_version
    program = program_version.program if program_version else None
    return LearningContextRead(
        learning_context_id=context.id,
        academic_year_id=context.academic_year_id,
        academic_year=context.academic_year.name,
        pathway_id=context.pathway_id,
        pathway_code=context.pathway.code,
        pathway_name=context.pathway.name,
        education_level_id=context.education_level_id,
        level_code=context.education_level.code,
        level_name=context.education_level.name,
        program_version_id=context.program_version_id,
        program_code=program.code if program else None,
        program_name=program.name if program else None,
        subject_id=context.subject_id,
        subject_code=context.subject.code,
        subject_name=context.subject.name,
    )


# --- get-or-create ------------------------------------------------------------------


def resolve_context(
    session: Session, payload: TeachingOfferingCreate
) -> LearningContext:
    """Validate the requested tuple, then return its context row (creating it).

    Steps, each of which may abort the whole operation:

    1. academic year (404, archived → 422), pathway (404), level (404),
    2. pathway ↔ level mapping (422),
    3. subject (404),
    4. when a program version is supplied: resolve it (404), prove it
       belongs to the tuple (422) and is active (422), then prove the
       subject is taught by it (422),
    5. look the natural key up; insert only when it is genuinely new.

    The insert races with another teacher publishing the same tuple: the
    savepoint keeps the outer transaction usable, so the loser re-reads the
    winner's row instead of failing the request.
    """
    year = _resolve_year(session, payload.academic_year_id)
    pathway = _resolve_pathway(session, payload.pathway_id)
    level = _resolve_level(session, payload.education_level_id)
    _validate_pathway_level(session, pathway, level)
    subject = _resolve_subject(session, payload.subject_id)

    program_version: ProgramVersion | None = None
    if payload.program_version_id is not None:
        program_version = _resolve_program_version(
            session, payload.program_version_id, year, pathway, level
        )
        _validate_subject_taught(session, program_version, subject)

    existing = context_repo.get_by_natural_key(
        session,
        academic_year_id=year.id,
        pathway_id=pathway.id,
        education_level_id=level.id,
        program_version_id=program_version.id if program_version else None,
        subject_id=subject.id,
    )
    if existing is not None:
        return existing

    try:
        with session.begin_nested():
            return context_repo.create(
                session,
                academic_year_id=year.id,
                pathway_id=pathway.id,
                education_level_id=level.id,
                program_version_id=program_version.id if program_version else None,
                subject_id=subject.id,
            )
    except IntegrityError:
        winner = context_repo.get_by_natural_key(
            session,
            academic_year_id=year.id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            program_version_id=program_version.id if program_version else None,
            subject_id=subject.id,
        )
        if winner is None:
            raise  # an unrelated integrity problem must stay visible (500)
        return winner
