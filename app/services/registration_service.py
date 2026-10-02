"""Registration service: the authoritative business layer for enrollments.

This is the Phase 3C **Option-A** architecture: cross-entity consistency
rules that foreign keys cannot express live here, not in the schema:

- pathway ↔ education level (through the real ``pathway_levels`` rows),
- program version ↔ offering context ``(academic_year, pathway, level)``
  (the four-column natural key ``uq_program_versions_offering_key``),
- school ↔ program offering (an actual ``school_programs`` row — never
  inferred from "school exists" + "program exists"),
- one enrollment per (student, academic year) — service pre-check for a
  useful 409, with the database UNIQUE constraint as the final guard,
- academic-year status: registration is only allowed into years whose
  status permits it (using the model's own vocabulary, never a new one).

Catalog truth is never hard-coded: pathway/level codes are resolved
against the database (any rows the seeders loaded), program/TVET/school
catalogs may be genuinely empty and the errors say so honestly.

Errors follow the shared convention: ``RegistrationError`` subclasses with
a ``status_code`` the API layer maps straight onto the HTTP response.
``RegistrationValidationError`` → 422, ``RegistrationConflictError`` →
409, ``RegistrationNotFoundError`` → 404, and
``RegistrationUnavailableError`` → 503 for "the catalog context exists but
is not configured yet" (e.g. a required program/TVET/school catalog that
is empty, or a closed academic year).

Transaction behavior: every write goes through the caller's request-scoped
session. The service only ``flush``es; the API layer commits on success.
Any raise — including one raised by the database itself — is rolled back
by the API layer, so a half-created registration can never survive.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.education_level import EducationLevel
from app.models.enums import AcademicYearStatus, EnrollmentStatus, StudentSubjectStatus
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_version import ProgramVersion
from app.models.school import School
from app.models.school_program import SchoolProgram
from app.models.student import Student
from app.models.student_subject import StudentSubject
from app.models.enrollment import StudentEnrollment
from app.models.tvet_program import TVETProgram
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import enrollment_repository as enrollment_repo
from app.schemas.pagination import Page
from app.schemas.registration import (
    RegistrationCreate,
    RegistrationRead,
    RegistrationSubjectRead,
)

logger = logging.getLogger(__name__)

# The database's final duplicate protection (mirrored by the service-level
# pre-check). When a concurrent INSERT wins the race, this constraint — not
# the pre-check — is what fires, and its name is how the resulting
# IntegrityError is recognised and translated into the 409 contract.
DUPLICATE_ENROLLMENT_CONSTRAINT = (
    "uq_student_enrollments_student_id_academic_year_id_key"
)

__all__ = [
    "RegistrationError",
    "RegistrationValidationError",
    "RegistrationConflictError",
    "RegistrationNotFoundError",
    "RegistrationUnavailableError",
    "ENROLLMENT_STATUS_VALUES",
    "ALLOWED_STATUS_TRANSITIONS",
    "TERMINAL_ENROLLMENT_STATUSES",
    "register_student",
    "get_registration",
    "list_student_registrations",
    "list_admin_registrations",
    "update_registration_status",
    "registration_readiness",
    "commit",
]

# Academic-year statuses that permit new registrations. Derived from the
# model vocabulary (AcademicYearStatus) — no invented state. ``closed`` and
# ``archived`` years honestly refuse registration; ``planned`` and
# ``active`` accept it.
REGISTRABLE_YEAR_STATUSES: frozenset[str] = frozenset(
    {AcademicYearStatus.PLANNED.value, AcademicYearStatus.ACTIVE.value}
)

#: The status vocabulary of ``student_enrollments`` (the model's CHECK
#: constraint), exposed for filter validation — no invented values.
ENROLLMENT_STATUS_VALUES: frozenset[str] = frozenset(
    status.value for status in EnrollmentStatus
)

#: Allowed status transitions of the administrative PATCH workflow, in
#: terms of the existing ``EnrollmentStatus`` vocabulary only — no new
#: state is invented: ``pending`` may become ``active`` or ``cancelled``;
#: an ``active`` registration may finish (``completed``) or end early
#: (``transferred``, ``withdrawn``, ``cancelled``). Same status is not a
#: transition (409), and any pair not listed here — including everything
#: out of a terminal state — is refused with 409.
ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    EnrollmentStatus.PENDING.value: frozenset(
        {EnrollmentStatus.ACTIVE.value, EnrollmentStatus.CANCELLED.value}
    ),
    EnrollmentStatus.ACTIVE.value: frozenset(
        {
            EnrollmentStatus.COMPLETED.value,
            EnrollmentStatus.TRANSFERRED.value,
            EnrollmentStatus.WITHDRAWN.value,
            EnrollmentStatus.CANCELLED.value,
        }
    ),
    EnrollmentStatus.COMPLETED.value: frozenset(),
    EnrollmentStatus.TRANSFERRED.value: frozenset(),
    EnrollmentStatus.WITHDRAWN.value: frozenset(),
    EnrollmentStatus.CANCELLED.value: frozenset(),
}

#: Statuses with no outgoing transitions (final). Entering one closes the
#: registration and stamps ``ended_at``.
TERMINAL_ENROLLMENT_STATUSES: frozenset[str] = frozenset(
    status
    for status, targets in ALLOWED_STATUS_TRANSITIONS.items()
    if not targets
)


class RegistrationError(Exception):
    """Base class: ``.status_code`` tells the API layer which HTTP status to emit."""

    status_code = 400


class RegistrationValidationError(RegistrationError):
    """A business rule failed (invalid pathway-level pair, wrong program
    context, school not offering the program, closed year)."""

    status_code = 422


class RegistrationConflictError(RegistrationError):
    status_code = 409


class RegistrationNotFoundError(RegistrationError):
    status_code = 404


class RegistrationUnavailableError(RegistrationError):
    """The request is understood, but the catalog context required to honor
    it is not configured (empty program/TVET/school catalog, closed year
    handled as policy). A retry may succeed once data is loaded."""

    status_code = 503


def _load_registration_read(
    session: Session,
    enrollment: StudentEnrollment,
) -> RegistrationRead:
    """Build the registration summary from the enrollment's catalog context."""
    program_version = enrollment.program_version
    program: Program | None = program_version.program if program_version else None
    return RegistrationRead(
        enrollment_id=enrollment.id,
        student_id=enrollment.student_id,
        academic_year_id=enrollment.academic_year_id,
        academic_year=enrollment.academic_year.name,
        pathway_id=enrollment.pathway_id,
        pathway_code=enrollment.pathway.code,
        pathway_name=enrollment.pathway.name,
        education_level_id=enrollment.education_level_id,
        level_code=enrollment.education_level.code,
        level_name=enrollment.education_level.name,
        program_version_id=program_version.id if program_version else None,
        program_code=program.code if program else None,
        program_name=program.name if program else None,
        school_id=enrollment.school_id,
        school_name=enrollment.school.name,
        school_code=enrollment.school.school_code,
        status=enrollment.status,
        started_at=enrollment.started_at,
        created_at=enrollment.created_at,
        subjects=[
            RegistrationSubjectRead(
                subject_id=link.subject_id,
                code=link.subject.code,
                name=link.subject.name,
                status=link.status,
            )
            for link in enrollment.subjects
        ],
    )


def _resolve_student(session: Session, student_id: uuid.UUID) -> Student:
    student = session.get(Student, student_id)
    if student is None:
        raise RegistrationNotFoundError(f"no student with id {student_id}")
    return student


def _resolve_academic_year(session: Session, academic_year_id: uuid.UUID) -> AcademicYear:
    year = session.get(AcademicYear, academic_year_id)
    if year is None:
        raise RegistrationNotFoundError(f"no academic year with id {academic_year_id}")
    return year


def _resolve_pathway(session: Session, pathway_code: str) -> Pathway:
    pathway = session.scalars(
        select(Pathway).where(Pathway.code == pathway_code)
    ).first()
    if pathway is None:
        raise RegistrationNotFoundError(f"no pathway with code {pathway_code!r}")
    return pathway


def _resolve_education_level(session: Session, level_code: str) -> EducationLevel:
    level = session.scalars(
        select(EducationLevel).where(EducationLevel.code == level_code)
    ).first()
    if level is None:
        raise RegistrationNotFoundError(f"no education level with code {level_code!r}")
    return level


def _resolve_school(session: Session, school_id: uuid.UUID) -> School:
    school = session.get(School, school_id)
    if school is None:
        raise RegistrationNotFoundError(f"no school with id {school_id}")
    return school


def _validate_pathway_level(
    session: Session,
    pathway: Pathway,
    level: EducationLevel,
) -> None:
    """The pathway must actually span the level (a real pathway_levels row)."""
    link = session.scalars(
        select(PathwayLevel).where(
            PathwayLevel.pathway_id == pathway.id,
            PathwayLevel.education_level_id == level.id,
        )
    ).first()
    if link is None:
        raise RegistrationValidationError(
            f"pathway {pathway.code!r} does not include education level "
            f"{level.code!r}"
        )


def _validate_year_registrable(year: AcademicYear) -> None:
    """Registration is only permitted for years whose status allows it.

    Uses the actual AcademicYearStatus vocabulary; a closed or archived
    year refuses honestly instead of pretending to accept registration.
    """
    if year.status not in REGISTRABLE_YEAR_STATUSES:
        raise RegistrationUnavailableError(
            f"academic year {year.name!r} is {year.status!r} and does not "
            "accept new registrations"
        )


def _resolve_program_version(
    session: Session,
    program_version_id: uuid.UUID,
    year: AcademicYear,
    pathway: Pathway,
    level: EducationLevel,
) -> ProgramVersion:
    """Load the program version and prove it belongs to the offering context.

    Identity is the four-column tuple
    ``(program_id, academic_year_id, pathway_id, education_level_id)`` —
    the program_versions.code label is never used as identity.
    """
    program_version = session.get(ProgramVersion, program_version_id)
    if program_version is None:
        raise RegistrationNotFoundError(
            f"no program version with id {program_version_id}"
        )
    if (
        program_version.academic_year_id != year.id
        or program_version.pathway_id != pathway.id
        or program_version.education_level_id != level.id
    ):
        raise RegistrationValidationError(
            "program version does not belong to the selected academic "
            "year / pathway / education level"
        )
    return program_version


def _validate_program_status(program_version: ProgramVersion) -> None:
    """An inactive program version cannot back a new registration."""
    if program_version.status != "active":
        raise RegistrationValidationError(
            f"program version {program_version.code!r} is "
            f"{program_version.status!r} and cannot be registered into"
        )


def _validate_tvet_profile(session: Session, program_version: ProgramVersion) -> None:
    """A TVET program must carry a real TVET profile (program → sector).

    When the TVET catalog is not configured for the selected program the
    error says so instead of fabricating a sector.
    """
    program = program_version.program
    if program is None:
        raise RegistrationNotFoundError(
            f"program {program_version.program_id} backing the selected "
            "program version no longer exists"
        )
    if program.program_type == "tvet_program":
        from app.models.tvet_program import TVETProgram

        profile = session.scalars(
            select(TVETProgram).where(TVETProgram.program_id == program.id)
        ).first()
        if profile is None:
            raise RegistrationUnavailableError(
                f"program {program.code!r} is a TVET program but has no "
                "TVET sector profile configured"
            )


def _validate_school_offers(
    session: Session,
    school: School,
    program_version: ProgramVersion,
) -> None:
    """A school offers a program version only via a real school_programs row."""
    offering = session.scalars(
        select(SchoolProgram).where(
            SchoolProgram.school_id == school.id,
            SchoolProgram.program_version_id == program_version.id,
        )
    ).first()
    if offering is None:
        raise RegistrationValidationError(
            f"school {school.name!r} does not offer program version "
            f"{program_version.code!r}"
        )


def register_student(session: Session, payload: RegistrationCreate) -> RegistrationRead:
    """Validate the full registration context, then create it atomically.

    Order of operations (each step may abort the whole operation):

    1. resolve student (404), academic year (404) and check its status,
    2. resolve pathway (404) and education level (404) by code,
    3. validate the pathway ↔ level mapping (422),
    4. duplicate-enrollment pre-check (409),
    5. when a program version is supplied: resolve it (404), prove it
       matches the offering context (422), prove TVET completeness (503
       when the TVET profile is missing) and school offering (422),
    6. insert the enrollment with the model's initial status (``pending``),
    7. derive ``student_subjects`` from the program version's
       ``program_subjects`` (only real mappings, never the bare subjects
       table),
    8. return the summary — the caller commits.

    No commit happens here: the API layer owns the request transaction, so
    a failure at any point rolls back everything (including the enrollment
    and any student-subject rows already flushed).
    """
    # 1. Student + academic year ------------------------------------------------
    _resolve_student(session, payload.student_id)
    year = _resolve_academic_year(session, payload.academic_year_id)
    _validate_year_registrable(year)

    # 2-3. Pathway + level + their mapping --------------------------------------
    pathway = _resolve_pathway(session, payload.pathway)
    level = _resolve_education_level(session, payload.education_level)
    _validate_pathway_level(session, pathway, level)

    # The schema requires a school (school_id NOT NULL) — always resolve it.
    school = _resolve_school(session, payload.school_id)

    # 4. One enrollment per student per academic year ---------------------------
    existing = enrollment_repo.find_existing(
        session, payload.student_id, payload.academic_year_id
    )
    if existing is not None:
        raise RegistrationConflictError(
            f"student {payload.student_id} is already enrolled in academic "
            f"year {year.name!r} (enrollment {existing.id})"
        )

    # 5. Program version context -------------------------------------------------
    program_version: ProgramVersion | None = None
    if payload.program_version_id is not None:
        program_version = _resolve_program_version(
            session, payload.program_version_id, year, pathway, level
        )
        _validate_program_status(program_version)
        _validate_tvet_profile(session, program_version)
        _validate_school_offers(session, school, program_version)

    # 6. Enrollment ---------------------------------------------------------------
    # Race safety: between the service pre-check (step 4) and this INSERT,
    # a concurrent request may commit its own enrollment for the same
    # (student, academic year). The database UNIQUE constraint is the final
    # protection — a violation here is translated into the same 409 contract
    # instead of leaking as a 500. The caller (API layer) rolls the failed
    # transaction back; nothing partial survives.
    try:
        enrollment = enrollment_repo.create(
            session,
            student_id=payload.student_id,
            academic_year_id=payload.academic_year_id,
            school_id=payload.school_id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            program_version_id=program_version.id if program_version else None,
            status=EnrollmentStatus.PENDING.value,  # the model's initial state
        )
    except IntegrityError as exc:
        if DUPLICATE_ENROLLMENT_CONSTRAINT in str(exc.orig):
            logger.warning(
                "duplicate registration race lost: student=%s year=%s",
                payload.student_id,
                payload.academic_year_id,
            )
            raise RegistrationConflictError(
                f"student {payload.student_id} is already enrolled in academic "
                f"year {year.name!r}"
            ) from exc
        raise  # a real database problem must stay visible (500)

    # 7. Student subjects (only from real program_subjects mappings) -------------
    if program_version is not None:
        subject_rows = enrollment_repo.subject_ids_for_program_version(
            session, program_version.id
        )
        if subject_rows:
            enrollment_repo.create_student_subjects(
                session,
                enrollment_id=enrollment.id,
                subject_ids=[row[0] for row in subject_rows],
                status=StudentSubjectStatus.ACTIVE.value,
            )

    # 8. Summary — re-read through the eager path so subjects and program
    #    context arrive via loader options, never lazy loads; caller commits.
    reloaded = enrollment_repo.reload_with_relations(session, enrollment.id)
    if reloaded is None:  # pragma: no cover - the row was just flushed
        raise RegistrationNotFoundError(f"no enrollment with id {enrollment.id}")
    return _load_registration_read(session, reloaded)


def get_registration(
    session: Session,
    enrollment_id: uuid.UUID,
) -> RegistrationRead:
    """One registration by enrollment id (404 when unknown)."""
    enrollment = enrollment_repo.get_by_id(session, enrollment_id)
    if enrollment is None:
        raise RegistrationNotFoundError(f"no enrollment with id {enrollment_id}")
    return _load_registration_read(session, enrollment)


def list_student_registrations(
    session: Session,
    student_id: uuid.UUID,
) -> list[RegistrationRead]:
    """All registrations of one student, newest academic year first (404 when
    the student does not exist)."""
    _resolve_student(session, student_id)
    return [
        _load_registration_read(session, enrollment)
        for enrollment in enrollment_repo.list_for_student(session, student_id)
    ]


def list_admin_registrations(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    academic_year_id: uuid.UUID | None = None,
    status: str | None = None,
    school_id: uuid.UUID | None = None,
    school_code: str | None = None,
    student_id: uuid.UUID | None = None,
) -> Page[RegistrationRead]:
    """One page of registrations for the administrative list.

    Every filter is optional and validated against the model's own
    vocabulary (``status``) or resolved as a plain equality/EXISTS
    predicate in the repository — exactly one count query plus one page
    query run, and the page query eager-loads program version → program,
    school and subjects so the summaries cost no per-row statements.

    Read-only: the caller's session is used as-is, nothing is committed.
    """
    if status is not None and status not in ENROLLMENT_STATUS_VALUES:
        raise RegistrationValidationError(
            f"unknown registration status {status!r} "
            f"(expected one of {', '.join(sorted(ENROLLMENT_STATUS_VALUES))})"
        )
    filters = dict(
        academic_year_id=academic_year_id,
        status=status,
        school_id=school_id,
        school_code=school_code,
        student_id=student_id,
    )
    total = enrollment_repo.count_page(session, **filters)
    rows = enrollment_repo.list_page(session, limit=limit, offset=offset, **filters)
    return Page[RegistrationRead](
        items=[_load_registration_read(session, enrollment) for enrollment in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def update_registration_status(
    session: Session,
    enrollment_id: uuid.UUID,
    new_status: str,
    *,
    actor_id: uuid.UUID,
) -> RegistrationRead:
    """Move one registration to another status (administrative, audited).

    Order of operations:

    1. validate the requested value against the model vocabulary (422 —
       over HTTP unreachable, the request schema already enforces it),
    2. lock the row with ``SELECT ... FOR UPDATE`` *before* reading its
       status, so two concurrent transitions serialize: the second one
       re-reads the committed row and honestly reports 409 instead of
       both overwriting each other,
    3. unknown id → 404; same status → 409; a pair outside
       :data:`ALLOWED_STATUS_TRANSITIONS` → 409 naming the current and
       the requested status,
    4. apply the change; entering a terminal status stamps ``ended_at``
       from the database clock (the model's other server timestamps use
       the same authority), so the date-order CHECK
       ``ended_at >= started_at`` can never be violated,
    5. record exactly one ``auth_events`` audit row — subject is the
       student's account, actor the administrator, payload
       ``{"from": ..., "to": ...}`` in the style of ``user_role_changed``,
    6. return the summary re-read through the eager path; the caller
       commits, and a 404/409 rolls the whole transaction back (the
       audit row included).

    No commit happens here — the API layer owns the request transaction.
    """
    if new_status not in ENROLLMENT_STATUS_VALUES:
        raise RegistrationValidationError(
            f"unknown registration status {new_status!r} "
            f"(expected one of {', '.join(sorted(ENROLLMENT_STATUS_VALUES))})"
        )
    enrollment = enrollment_repo.get_by_id_for_update(session, enrollment_id)
    if enrollment is None:
        raise RegistrationNotFoundError(f"no enrollment with id {enrollment_id}")

    current = enrollment.status
    if new_status == current:
        raise RegistrationConflictError(
            f"enrollment {enrollment_id} is already {current!r}"
        )
    if new_status not in ALLOWED_STATUS_TRANSITIONS.get(current, frozenset()):
        raise RegistrationConflictError(
            f"enrollment {enrollment_id} cannot move from status "
            f"{current!r} to {new_status!r}"
        )

    enrollment.status = new_status
    if new_status in TERMINAL_ENROLLMENT_STATUSES:
        enrollment.ended_at = func.now()
    session.flush()

    student = session.get(Student, enrollment.student_id)
    if student is None:  # pragma: no cover - FK guarantees the row
        raise RegistrationNotFoundError(f"no student with id {enrollment.student_id}")
    auth_event_repo.log_event(
        session,
        user_id=student.user_id,
        event_type="registration_status_changed",
        actor_user_id=actor_id,
        metadata_json=json.dumps({"from": current, "to": new_status}),
    )

    reloaded = enrollment_repo.reload_with_relations(session, enrollment.id)
    if reloaded is None:  # pragma: no cover - the row is locked in this transaction
        raise RegistrationNotFoundError(f"no enrollment with id {enrollment_id}")
    logger.info(
        "registration status changed",
        extra={
            "enrollment_id": str(enrollment.id),
            "actor_id": str(actor_id),
            "from_status": current,
            "to_status": new_status,
        },
    )
    return _load_registration_read(session, reloaded)


def registration_readiness(session: Session) -> dict:
    """Read-only readiness report for the future Student Portal.

    Truthfully reports which catalog sections are currently populated and
    which block registration. It never creates records and never claims
    registration is available when required data is missing.

    Shape (deliberately plain JSON, no ORM internals)::

        {
          "ready": bool,                # every mandatory section available
          "open_academic_year": {...} | None,
          "pathways_available": bool,
          "education_levels_available": bool,
          "pathway_level_mappings_available": bool,
          "program_catalog_available": bool,   # optional for registration
          "school_catalog_available": bool,    # mandatory for registration
          "tvet_catalog_available": bool,      # relevant only to TVET pathways
        }

    ``ready`` is False while the school catalog is empty, no registrable
    (planned/active) academic year exists, or no pathway-level mappings are
    loaded — those are mandatory. The program and TVET catalogs are optional
    today (program_version_id is nullable) and are reported for
    transparency, not gate-keeping.
    """
    registrable = (
        select(AcademicYear)
        .where(AcademicYear.status.in_(sorted(REGISTRABLE_YEAR_STATUSES)))
        .order_by(AcademicYear.start_date.desc())
        .limit(1)
    )
    open_year = session.scalars(registrable).first()

    pathways = session.scalar(select(func.count()).select_from(Pathway)) or 0
    levels = session.scalar(select(func.count()).select_from(EducationLevel)) or 0
    pathway_level_mappings = (
        session.scalar(select(func.count()).select_from(PathwayLevel)) or 0
    )
    program_versions = (
        session.scalar(select(func.count()).select_from(ProgramVersion)) or 0
    )
    schools = session.scalar(select(func.count()).select_from(School)) or 0
    tvet_programs = (
        session.scalar(select(func.count()).select_from(TVETProgram)) or 0
    )

    school_catalog_available = schools > 0
    ready = (
        open_year is not None
        and pathways > 0
        and levels > 0
        and pathway_level_mappings > 0
        and school_catalog_available
    )

    return {
        "ready": ready,
        "open_academic_year": (
            {
                "id": str(open_year.id),
                "name": open_year.name,
                "status": open_year.status,
                "start_date": open_year.start_date.isoformat(),
                "end_date": open_year.end_date.isoformat(),
            }
            if open_year is not None
            else None
        ),
        "pathways_available": pathways > 0,
        "education_levels_available": levels > 0,
        "pathway_level_mappings_available": pathway_level_mappings > 0,
        "program_catalog_available": program_versions > 0,
        "school_catalog_available": school_catalog_available,
        "tvet_catalog_available": tvet_programs > 0,
    }


def commit(session: Session) -> None:
    """Commit the request transaction (called by the API layer on success)."""
    session.commit()
