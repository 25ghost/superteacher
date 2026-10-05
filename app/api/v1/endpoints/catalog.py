"""Shared catalog read APIs (SuperTeacher reference data).

These endpoints expose the whole-system education catalog — academic years,
pathways, education levels, subjects, programs, program versions, TVET
sectors/programs, schools, and school offerings. They are **shared
SuperTeacher reference-data APIs**, not Student-Registration-private
endpoints: Curriculum, Learning, Progress and future modules consume the
same data.

Conventions:

- Collection endpoints return ``200 OK`` with ``[]`` when no matching
  records exist; an empty catalog is a fact, not an error.
- ``404``/``400`` are reserved for invalid or unknown requested identities
  (e.g. ``pathway=NOPE``, unknown school code).
- Ordering is deterministic (see ``repositories/catalog_repository.py``).
- Reads only: nothing here creates, updates or deletes catalog records.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_CATALOG
from app.core.database import get_db
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
from app.services import catalog_service

router = APIRouter(prefix="/catalog", tags=[TAG_CATALOG])

_SHARED_REFERENCE_NOTE = (
    "SuperTeacher shared reference-data API: consumed by the Student "
    "Registration Portal first, and reused by Curriculum, Learning, Progress "
    "and future modules."
)


def _not_found(exc: CatalogNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# --- academic years -------------------------------------------------------------


@router.get(
    "/academic-years",
    response_model=list[AcademicYearRead],
    summary="List academic years",
    description=(
        "Academic years, newest first (start_date descending, then name). "
        "Filter by status when needed (planned | active | closed | archived). "
        + _SHARED_REFERENCE_NOTE
    ),
)
def read_academic_years(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    session: Session = Depends(get_db),
) -> list[AcademicYearRead]:
    return catalog_service.list_academic_years(session, status=status_filter)


# --- pathways ---------------------------------------------------------------------


@router.get(
    "/pathways",
    response_model=list[PathwayRead],
    summary="List learning pathways",
    description=(
        "Learning pathways ordered by code (e.g. O_LEVEL, A_LEVEL, TVET, "
        "TTC — whatever rows exist in PostgreSQL). " + _SHARED_REFERENCE_NOTE
    ),
)
def read_pathways(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    session: Session = Depends(get_db),
) -> list[PathwayRead]:
    return catalog_service.list_pathways(session, status=status_filter)


# --- education levels ---------------------------------------------------------------


@router.get(
    "/education-levels",
    response_model=list[EducationLevelRead],
    summary="List education levels",
    description=(
        "Education levels ordered by level_number. With "
        "?pathway={code}, returns only the levels actually mapped to that "
        "pathway through the pathway_levels relationship. An unknown "
        "pathway code is a 404 error. " + _SHARED_REFERENCE_NOTE
    ),
)
def read_education_levels(
    pathway: str | None = Query(default=None, alias="pathway", max_length=32),
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    session: Session = Depends(get_db),
) -> list[EducationLevelRead]:
    try:
        return catalog_service.list_education_levels(
            session, status=status_filter, pathway_code=pathway
        )
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc


# --- subjects -------------------------------------------------------------------------


@router.get(
    "/subjects",
    response_model=list[SubjectRead],
    summary="List subjects",
    description=(
        "The shared subject catalog ordered by name. " + _SHARED_REFERENCE_NOTE
    ),
)
def read_subjects(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    session: Session = Depends(get_db),
) -> list[SubjectRead]:
    return catalog_service.list_subjects(session, status=status_filter)


# --- programs / program versions --------------------------------------------------------


@router.get(
    "/programs",
    response_model=list[ProgramRead],
    summary="List programs",
    description=(
        "Programs of study (combinations, TVET programs, streams) ordered by "
        "code. Filter by offering context — ?pathway={code} and "
        "?level={code} resolve against actual program_versions rows. "
        "Unknown pathway/level codes are 404 errors. "
        + _SHARED_REFERENCE_NOTE
    ),
)
def read_programs(
    pathway: str | None = Query(default=None, alias="pathway", max_length=64),
    level: str | None = Query(default=None, alias="level", max_length=32),
    program_type: str | None = Query(default=None, max_length=32),
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    session: Session = Depends(get_db),
) -> list[ProgramRead]:
    try:
        return catalog_service.list_programs(
            session,
            status=status_filter,
            program_type=program_type,
            pathway_code=pathway,
            level_code=level,
        )
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get(
    "/program-versions",
    response_model=list[ProgramVersionRead],
    summary="List program versions",
    description=(
        "Program offerings per academic year / pathway / level. Identity is "
        "the offering tuple (program, academic year, pathway, education "
        "level) — the code field is a label, never identity. Unknown "
        "pathway/level codes are 404 errors. " + _SHARED_REFERENCE_NOTE
    ),
)
def read_program_versions(
    pathway: str | None = Query(default=None, alias="pathway", max_length=64),
    level: str | None = Query(default=None, alias="level", max_length=32),
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    session: Session = Depends(get_db),
) -> list[ProgramVersionRead]:
    try:
        return catalog_service.list_program_versions(
            session, status=status_filter, pathway_code=pathway, level_code=level
        )
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc


# --- TVET ---------------------------------------------------------------------------------


@router.get(
    "/tvet/sectors",
    response_model=list[TVETSectorRead],
    summary="List TVET sectors",
    description=(
        "TVET sectors ordered by code. Truthfully empty while the catalog "
        "contains no sectors. " + _SHARED_REFERENCE_NOTE
    ),
)
def read_tvet_sectors(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    session: Session = Depends(get_db),
) -> list[TVETSectorRead]:
    return catalog_service.list_tvet_sectors(session, status=status_filter)


@router.get(
    "/tvet/programs",
    response_model=list[TVETProgramRead],
    summary="List TVET programs",
    description=(
        "TVET programs (program + sector profiles) ordered by program code. "
        "Truthfully empty while the catalog contains none; an unknown "
        "?sector= code is a 404 error. " + _SHARED_REFERENCE_NOTE
    ),
)
def read_tvet_programs(
    sector: str | None = Query(default=None, alias="sector", max_length=64),
    session: Session = Depends(get_db),
) -> list[TVETProgramRead]:
    try:
        return catalog_service.list_tvet_programs(session, sector_code=sector)
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc


# --- schools / offerings ---------------------------------------------------------------------


@router.get(
    "/schools",
    response_model=list[SchoolRead],
    summary="List schools",
    description=(
        "Schools ordered by name. Optional province/district filters. "
        "Truthfully empty while the catalog contains no schools. "
        + _SHARED_REFERENCE_NOTE
    ),
)
def read_schools(
    province: str | None = Query(default=None, max_length=80),
    district: str | None = Query(default=None, max_length=80),
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    session: Session = Depends(get_db),
) -> list[SchoolRead]:
    return catalog_service.list_schools(
        session, status=status_filter, province=province, district=district
    )


@router.get(
    "/schools/{school_code}/programs",
    response_model=list[SchoolProgramRead],
    summary="List a school's program offerings",
    description=(
        "The program versions a school actually offers (real "
        "school_programs rows only — offerings are never inferred). An "
        "unknown school code is a 404 error. " + _SHARED_REFERENCE_NOTE
    ),
)
def read_school_programs(
    school_code: str,
    session: Session = Depends(get_db),
) -> list[SchoolProgramRead]:
    try:
        return catalog_service.list_school_programs_for_school(session, school_code)
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc
