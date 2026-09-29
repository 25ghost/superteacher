"""Pydantic response schemas for the shared catalog read APIs.

These schemas are the API contract for SuperTeacher shared reference data —
academic years, pathways, education levels, subjects, programs, program
versions, TVET sectors/programs, schools, and school offerings. Student
Registration is their first consumer; Curriculum, Learning, Progress and the
other future modules read the same contract.

Conventions (Pydantic v2):

- ``model_config = ConfigDict(from_attributes=True)`` so the ORM rows can be
  validated directly.
- Internal UUID primary keys are exposed as ``id`` — consumers need stable
  references — but ORM implementation details (timestamps, FK columns) are
  not.
- ``code``/``name`` pairs are the human-facing identity of every catalog row;
  program-version identity is additionally the offering tuple, mirrored by
  the ``program_version_code``/``pathway_code``/``level_code``/
  ``academic_year_name`` convenience fields on ``ProgramVersionRead``
  (never ``program_versions.code`` alone — that column is a label, not
  identity).
"""
from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CatalogBase(BaseModel):
    """Fields shared by every catalog read model."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    description: str | None = None
    status: str


class AcademicYearRead(BaseModel):
    """One academic year."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    start_date: date
    end_date: date
    status: str


class PathwayRead(CatalogBase):
    """One learning pathway (O-Level, A-Level, TVET, ...)."""


class EducationLevelRead(BaseModel):
    """One education level (S1..S6, L3..L5)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    level_number: int
    description: str | None = None
    status: str


class SubjectRead(CatalogBase):
    """One subject in the shared subject catalog."""


class ProgramRead(CatalogBase):
    """One program of study (combination, TVET program, stream, other)."""

    program_type: str


class ProgramVersionRead(BaseModel):
    """A program's offering in one academic year / pathway / level.

    Identity is the offering tuple
    ``(program, academic_year, pathway, education_level)`` — mirrored here by
    the ``*_code``/``*_name`` fields. The ``code`` column itself is a
    human-facing label only.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    program_id: UUID
    program_code: str
    academic_year_name: str
    pathway_code: str
    level_code: str
    code: str
    name: str
    description: str | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    status: str


class TVETSectorRead(CatalogBase):
    """One TVET sector."""


class TVETProgramRead(BaseModel):
    """One TVET program (a program profile attached to a sector)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    program_id: UUID
    program_code: str
    program_name: str
    sector_code: str
    sector_name: str


class SchoolRead(BaseModel):
    """One school in the shared school catalog.

    Deliberately does NOT inherit :class:`CatalogBase`: a school's code
    identity is ``school_code`` (the seeder's natural key), not the generic
    ``code`` field — and the ``schools`` table has no ``code`` column, so
    inheriting it made every validation of a real row fail (contract bug
    found in Phase 5E while wiring the Student Portal, invisible until then
    because the catalog was empty).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    school_code: str
    name: str
    status: str
    school_type: str | None = None
    province: str | None = None
    district: str | None = None
    sector: str | None = None


class SchoolProgramRead(BaseModel):
    """A school's actual offering of one program version.

    Includes the program version's full offering context so consumers can
    filter and display offerings without a separate program-version lookup.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    school_id: UUID
    school_code: str
    program_version_id: UUID
    program_code: str
    program_name: str
    code: str
    name: str
    pathway_code: str
    level_code: str
    status: str
