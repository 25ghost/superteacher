"""Catalog services: read rules over the shared education reference data.

The service layer turns repository results into API-ready payloads and owns
the read-side business rules:

- resolving catalog identities (pathway code -> row, level code -> row,
  school code -> row) and translating "not found" into the API convention,
- pathway -> education-level lookups through the actual ``pathway_levels``
  relationship,
- offering-context filtering for programs and program versions,
- school-offering lookups (a school offers a program only when an actual
  ``school_programs`` row exists — never inferred).

Keep database querying in the repositories and HTTP concerns in the API
layer. These functions are reusable by future modules (Curriculum, Learning,
Progress, Registration), not just the Student Portal.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.repositories import catalog_repository as repo
from app.repositories.catalog_repository import CatalogNotFoundError
from app.schemas.catalog import (
    AcademicYearRead,
    EducationLevelRead,
    ProgramRead,
    ProgramVersionRead,
    PathwayRead,
    SchoolProgramRead,
    SchoolRead,
    SubjectRead,
    TVETProgramRead,
    TVETSectorRead,
)

__all__ = [
    "CatalogNotFoundError",
    "list_academic_years",
    "list_pathways",
    "list_education_levels",
    "list_subjects",
    "list_programs",
    "list_program_versions",
    "list_tvet_sectors",
    "list_tvet_programs",
    "list_schools",
    "list_school_programs_for_school",
]


def list_academic_years(session: Session, status: str | None = None) -> list[AcademicYearRead]:
    """Newest-first academic years, optionally filtered by status."""
    return [
        AcademicYearRead.model_validate(row)
        for row in repo.list_academic_years(session, status=status)
    ]


def list_pathways(session: Session, status: str | None = None) -> list[PathwayRead]:
    """All pathways ordered by code, optionally filtered by status."""
    return [
        PathwayRead.model_validate(row)
        for row in repo.list_pathways(session, status=status)
    ]


def list_education_levels(
    session: Session,
    status: str | None = None,
    pathway_code: str | None = None,
) -> list[EducationLevelRead]:
    """Education levels ordered by ``level_number``.

    ``pathway_code`` resolves against the database first; an unknown code is
    an error (HTTP 400 at the API layer), not an empty list — an empty list
    is reserved for a pathway that genuinely has no levels mapped.
    """
    pathway_id: uuid.UUID | None = None
    if pathway_code is not None:
        pathway_id = repo.get_pathway_by_code(session, pathway_code).id
    return [
        EducationLevelRead.model_validate(row)
        for row in repo.list_education_levels(session, status=status, pathway_id=pathway_id)
    ]


def list_subjects(session: Session, status: str | None = None) -> list[SubjectRead]:
    """Shared subject catalog ordered by name."""
    return [
        SubjectRead.model_validate(row)
        for row in repo.list_subjects(session, status=status)
    ]


def list_programs(
    session: Session,
    status: str | None = None,
    program_type: str | None = None,
    pathway_code: str | None = None,
    level_code: str | None = None,
) -> list[ProgramRead]:
    """Programs ordered by code, filtered through actual offering context."""
    pathway_id = (
        repo.get_pathway_by_code(session, pathway_code).id
        if pathway_code is not None
        else None
    )
    education_level_id = (
        repo.get_education_level_by_code(session, level_code).id
        if level_code is not None
        else None
    )
    return [
        ProgramRead.model_validate(row)
        for row in repo.list_programs(
            session,
            status=status,
            program_type=program_type,
            pathway_id=pathway_id,
            education_level_id=education_level_id,
        )
    ]


def list_program_versions(
    session: Session,
    status: str | None = None,
    pathway_code: str | None = None,
    level_code: str | None = None,
) -> list[ProgramVersionRead]:
    """Program versions with their full offering tuple resolved."""
    pathway_id = (
        repo.get_pathway_by_code(session, pathway_code).id
        if pathway_code is not None
        else None
    )
    education_level_id = (
        repo.get_education_level_by_code(session, level_code).id
        if level_code is not None
        else None
    )
    rows = repo.list_program_versions(
        session, status=status, pathway_id=pathway_id, education_level_id=education_level_id
    )
    return [_program_version_read(row) for row in rows]


def _program_version_read(row) -> ProgramVersionRead:
    return ProgramVersionRead(
        id=row.id,
        program_id=row.program_id,
        program_code=row.program.code,
        academic_year_name=row.academic_year.name,
        pathway_code=row.pathway.code,
        level_code=row.education_level.code,
        code=row.code,
        name=row.name,
        description=row.description,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        status=row.status,
    )


def list_tvet_sectors(session: Session, status: str | None = None) -> list[TVETSectorRead]:
    """TVET sectors ordered by code. Truthfully empty while no rows exist."""
    return [
        TVETSectorRead.model_validate(row)
        for row in repo.list_tvet_sectors(session, status=status)
    ]


def list_tvet_programs(
    session: Session,
    sector_code: str | None = None,
) -> list[TVETProgramRead]:
    """TVET programs ordered by program code, optionally by sector."""
    if sector_code is not None:
        sector = repo.get_tvet_sector_by_code(session, sector_code)
        if sector is None:
            raise CatalogNotFoundError(f"unknown TVET sector code: {sector_code!r}")
        rows = repo.list_tvet_programs(session, sector_id=sector.id)
    else:
        rows = repo.list_tvet_programs(session)
    return [
        TVETProgramRead(
            id=row.id,
            program_id=row.program_id,
            program_code=row.program.code,
            program_name=row.program.name,
            sector_code=row.sector.code,
            sector_name=row.sector.name,
        )
        for row in rows
    ]


def list_schools(
    session: Session,
    status: str | None = None,
    province: str | None = None,
    district: str | None = None,
) -> list[SchoolRead]:
    """Schools ordered by name. Truthfully empty while no rows exist."""
    return [
        SchoolRead.model_validate(row)
        for row in repo.list_schools(session, status=status, province=province, district=district)
    ]


def list_school_programs_for_school(
    session: Session, school_code: str
) -> list[SchoolProgramRead]:
    """Offerings of one school. Unknown school code is an error (404 upstream).

    Only actual ``school_programs`` rows are returned — a school's offering
    is never inferred from its type or level.

    Each row includes the program version's full offering context (program
    code/name, version code/name, pathway code, level code) so consumers
    can filter and display offerings without a separate lookup.
    """
    school = repo.get_school_by_code(session, school_code)
    rows = repo.list_school_programs(session, school_id=school.id)
    return [
        SchoolProgramRead(
            id=row.id,
            school_id=row.school_id,
            school_code=row.school.school_code or "",
            program_version_id=row.program_version_id,
            program_code=row.program_version.program.code,
            program_name=row.program_version.program.name,
            code=row.program_version.code,
            name=row.program_version.name,
            pathway_code=row.program_version.pathway.code,
            level_code=row.program_version.education_level.code,
            status=row.status,
        )
        for row in rows
    ]
