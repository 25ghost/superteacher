"""Read-only data access for the shared education catalog.

Repositories own every SELECT for catalog resources: joins, filters and
deterministic ordering live here. They contain no HTTP concepts and no
business rules, and are deliberately named for the *catalog* — not for the
Student Portal — because Curriculum, Learning, Progress and Registration all
consume the same reference data.

Ordering contract (documented for API consumers):

- ``academic_years``  : ``start_date`` descending (newest first), then ``name``
- ``pathways``        : ``code`` ascending
- ``education_levels``: ``level_number`` ascending
- ``subjects``        : ``name`` ascending, then ``code``
- ``programs``        : ``code`` ascending
- ``program_versions``: program code, academic-year start date, level number
- ``tvet_sectors``    : ``code`` ascending
- ``tvet_programs``   : program code ascending
- ``schools``         : ``name`` ascending, then school_code
- ``school_programs`` : school name, school code, then program-version id

A repository function that resolves a catalog identity (e.g. one pathway by
code) raises :class:`CatalogNotFoundError`; the service layer translates that
into the API's HTTP convention.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from app.models.academic_year import AcademicYear
from app.models.education_level import EducationLevel
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_version import ProgramVersion
from app.models.school import School
from app.models.school_program import SchoolProgram
from app.models.subject import Subject
from app.models.tvet_program import TVETProgram
from app.models.tvet_sector import TVETSector


class CatalogNotFoundError(LookupError):
    """A requested catalog identity does not exist (e.g. unknown pathway code)."""


def _order_by(stmt: Select, *columns) -> Select:
    return stmt.order_by(*columns)


# --- academic years -----------------------------------------------------------


def list_academic_years(session: Session, status: str | None = None) -> list[AcademicYear]:
    stmt = _order_by(
        select(AcademicYear), AcademicYear.start_date.desc(), AcademicYear.name
    )
    if status is not None:
        stmt = stmt.where(AcademicYear.status == status)
    return list(session.scalars(stmt))


# --- pathways ------------------------------------------------------------------


def list_pathways(session: Session, status: str | None = None) -> list[Pathway]:
    stmt = _order_by(select(Pathway), Pathway.code)
    if status is not None:
        stmt = stmt.where(Pathway.status == status)
    return list(session.scalars(stmt))


def get_pathway_by_code(session: Session, code: str) -> Pathway:
    pathway = session.scalar(select(Pathway).where(Pathway.code == code))
    if pathway is None:
        raise CatalogNotFoundError(f"unknown pathway code: {code!r}")
    return pathway


# --- education levels ----------------------------------------------------------


def list_education_levels(
    session: Session,
    status: str | None = None,
    pathway_id: uuid.UUID | None = None,
) -> list[EducationLevel]:
    stmt = _order_by(select(EducationLevel), EducationLevel.level_number, EducationLevel.code)
    if status is not None:
        stmt = stmt.where(EducationLevel.status == status)
    if pathway_id is not None:
        # Filter through the actual pathway_levels relationship, not names.
        stmt = stmt.join(
            PathwayLevel, PathwayLevel.education_level_id == EducationLevel.id
        ).where(PathwayLevel.pathway_id == pathway_id)
    return list(session.scalars(stmt))


def get_education_level_by_code(session: Session, code: str) -> EducationLevel:
    level = session.scalar(select(EducationLevel).where(EducationLevel.code == code))
    if level is None:
        raise CatalogNotFoundError(f"unknown education level code: {code!r}")
    return level


# --- subjects -------------------------------------------------------------------


def list_subjects(session: Session, status: str | None = None) -> list[Subject]:
    stmt = _order_by(select(Subject), Subject.name, Subject.code)
    if status is not None:
        stmt = stmt.where(Subject.status == status)
    return list(session.scalars(stmt))


# --- programs / program versions -------------------------------------------------


def list_programs(
    session: Session,
    status: str | None = None,
    program_type: str | None = None,
    pathway_id: uuid.UUID | None = None,
    education_level_id: uuid.UUID | None = None,
) -> list[Program]:
    stmt = _order_by(select(Program), Program.code)
    if status is not None:
        stmt = stmt.where(Program.status == status)
    if program_type is not None:
        stmt = stmt.where(Program.program_type == program_type)
    if pathway_id is not None or education_level_id is not None:
        # Programs exist in a pathway/level context only through their
        # program_versions rows — filter through that relationship.
        stmt = stmt.join(
            ProgramVersion, ProgramVersion.program_id == Program.id
        ).distinct()
        if pathway_id is not None:
            stmt = stmt.where(ProgramVersion.pathway_id == pathway_id)
        if education_level_id is not None:
            stmt = stmt.where(ProgramVersion.education_level_id == education_level_id)
    return list(session.scalars(stmt))


def list_program_versions(
    session: Session,
    status: str | None = None,
    program_id: uuid.UUID | None = None,
    academic_year_id: uuid.UUID | None = None,
    pathway_id: uuid.UUID | None = None,
    education_level_id: uuid.UUID | None = None,
) -> list[ProgramVersion]:
    stmt = (
        select(ProgramVersion)
        .options(selectinload(ProgramVersion.program))
        .join(
            AcademicYear, AcademicYear.id == ProgramVersion.academic_year_id
        )
        .join(Pathway, Pathway.id == ProgramVersion.pathway_id)
        .join(EducationLevel, EducationLevel.id == ProgramVersion.education_level_id)
        .join(Program, Program.id == ProgramVersion.program_id)
    )
    if status is not None:
        stmt = stmt.where(ProgramVersion.status == status)
    if program_id is not None:
        stmt = stmt.where(ProgramVersion.program_id == program_id)
    if academic_year_id is not None:
        stmt = stmt.where(ProgramVersion.academic_year_id == academic_year_id)
    if pathway_id is not None:
        stmt = stmt.where(ProgramVersion.pathway_id == pathway_id)
    if education_level_id is not None:
        stmt = stmt.where(ProgramVersion.education_level_id == education_level_id)
    stmt = _order_by(
        stmt,
        Program.code,
        AcademicYear.start_date.desc(),
        EducationLevel.level_number,
    )
    return list(session.scalars(stmt))


# --- TVET -------------------------------------------------------------------------


def list_tvet_sectors(session: Session, status: str | None = None) -> list[TVETSector]:
    stmt = _order_by(select(TVETSector), TVETSector.code)
    if status is not None:
        stmt = stmt.where(TVETSector.status == status)
    return list(session.scalars(stmt))


def get_tvet_sector_by_code(session: Session, code: str) -> TVETSector | None:
    """Return one TVET sector by code, or None when it does not exist."""
    return session.scalar(select(TVETSector).where(TVETSector.code == code))


def list_tvet_programs(
    session: Session,
    sector_id: uuid.UUID | None = None,
) -> list[TVETProgram]:
    stmt = (
        select(TVETProgram)
        .options(selectinload(TVETProgram.program), selectinload(TVETProgram.sector))
        .join(Program, Program.id == TVETProgram.program_id)
        .join(TVETSector, TVETSector.id == TVETProgram.sector_id)
    )
    if sector_id is not None:
        stmt = stmt.where(TVETProgram.sector_id == sector_id)
    stmt = _order_by(stmt, Program.code)
    return list(session.scalars(stmt))


# --- schools / school programs ------------------------------------------------------


def list_schools(
    session: Session,
    status: str | None = None,
    province: str | None = None,
    district: str | None = None,
) -> list[School]:
    stmt = _order_by(select(School), School.name, School.school_code)
    if status is not None:
        stmt = stmt.where(School.status == status)
    if province is not None:
        stmt = stmt.where(School.province == province)
    if district is not None:
        stmt = stmt.where(School.district == district)
    return list(session.scalars(stmt))


def get_school_by_code(session: Session, school_code: str) -> School:
    school = session.scalar(select(School).where(School.school_code == school_code))
    if school is None:
        raise CatalogNotFoundError(f"unknown school code: {school_code!r}")
    return school


def get_school_by_id(session: Session, school_id: uuid.UUID) -> School | None:
    """The school with this primary key, or ``None`` when unknown.

    Unlike :func:`get_school_by_code` this returns ``None`` instead of
    raising: callers outside the catalog API (administrative school
    assignment) map the miss onto their own error family, and a
    repository must not choose the caller's HTTP contract for them.
    """
    return session.scalar(select(School).where(School.id == school_id))


def list_school_programs(
    session: Session,
    school_id: uuid.UUID | None = None,
) -> list[SchoolProgram]:
    stmt = (
        select(SchoolProgram)
        .options(
            selectinload(SchoolProgram.school),
            selectinload(SchoolProgram.program_version)
            .selectinload(ProgramVersion.program),
            selectinload(SchoolProgram.program_version)
            .selectinload(ProgramVersion.pathway),
            selectinload(SchoolProgram.program_version)
            .selectinload(ProgramVersion.education_level),
        )
        .join(School, School.id == SchoolProgram.school_id)
    )
    if school_id is not None:
        stmt = stmt.where(SchoolProgram.school_id == school_id)
    stmt = _order_by(stmt, School.name, School.school_code, SchoolProgram.program_version_id)
    return list(session.scalars(stmt))
