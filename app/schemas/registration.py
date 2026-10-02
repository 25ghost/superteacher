"""Pydantic schemas for the student registration API (Phase 5D).

Boundary: a *registration* binds an already-created student profile to one
academic year / pathway / education level / (optional) program version /
school, producing a ``student_enrollments`` row (plus ``student_subjects``
rows derived from the program version when one is selected). Personal
profile data has no place here (Phase 5C owns it), and learning progress
belongs to future modules.

Request conventions:

- Stable identifiers only, no nested objects: UUID ids for student,
  academic year, program version and school (the same ids the catalog
  APIs expose), and the catalog's natural string codes for pathway and
  education level (resolved exactly like the Phase 5B catalog query
  parameters ``?pathway={code}`` / ``?level={code}``).
- ``program_version_id`` is optional: the model allows an enrollment
  without a program version, and the current program catalog may be
  genuinely empty. When supplied, it is validated against the full
  offering context by the registration service — never trusted as-is.

Response conventions:

- ``RegistrationRead`` carries both stable ids and the human-readable
  catalog names/codes so the Student Portal can render a registration
  summary without extra round-trips. Internal columns that are not part
  of the registration's meaning (e.g. ``updated_at``, raw FK columns the
  summary already expresses by name) are not exposed.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import EnrollmentStatus


class RegistrationCreate(BaseModel):
    """Request body for registering a student into an academic year.

    ``pathway`` and ``education_level`` are the catalog codes (e.g.
    ``O_LEVEL``, ``S1``); everything else is a UUID id. The service
    validates every cross-entity rule before any row is written.
    """

    student_id: UUID
    academic_year_id: UUID
    pathway: str = Field(min_length=1, max_length=32)
    education_level: str = Field(min_length=1, max_length=32)
    program_version_id: UUID | None = None
    school_id: UUID

    @field_validator("pathway", "education_level")
    @classmethod
    def _code_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("catalog code must not be empty")
        return stripped


class RegistrationCreateSelf(BaseModel):
    """Student self-service registration request (``POST /me/registrations``).

    The identity comes from the Bearer token — there is deliberately no
    ``student_id`` field, so registering on behalf of someone else is not
    even expressible; ``extra="forbid"`` turns a spoofing attempt (or a
    typo) into a 422 instead of a silent no-op. Administrators register
    on behalf of a student through ``POST /admin/registrations`` with
    :class:`RegistrationCreateAdmin`.
    """

    model_config = ConfigDict(extra="forbid")

    academic_year_id: UUID
    pathway: str = Field(min_length=1, max_length=32)
    education_level: str = Field(min_length=1, max_length=32)
    program_version_id: UUID | None = None
    school_id: UUID

    @field_validator("pathway", "education_level")
    @classmethod
    def _code_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("catalog code must not be empty")
        return stripped


class RegistrationCreateAdmin(BaseModel):
    """Administrative registration request (``POST /admin/registrations``).

    Trusted workflow (admin role): ``student_id`` is
    REQUIRED — it names the student being registered on behalf of.
    ``extra="forbid"`` rejects unknown keys with a 422.
    """

    model_config = ConfigDict(extra="forbid")

    student_id: UUID
    academic_year_id: UUID
    pathway: str = Field(min_length=1, max_length=32)
    education_level: str = Field(min_length=1, max_length=32)
    program_version_id: UUID | None = None
    school_id: UUID

    @field_validator("pathway", "education_level")
    @classmethod
    def _code_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("catalog code must not be empty")
        return stripped


class RegistrationSubjectRead(BaseModel):
    """One subject derived from the selected program version."""

    model_config = ConfigDict(from_attributes=True)

    subject_id: UUID
    code: str
    name: str
    status: str


class OpenAcademicYearSummary(BaseModel):
    """The newest registrable (planned/active) academic year, if any."""

    id: UUID
    name: str
    status: str
    start_date: str
    end_date: str


class RegistrationReadiness(BaseModel):
    """Read-only report of whether registration can currently proceed.

    Truthfully reflects catalog state: ``ready`` is false while a required
    section (open academic year, pathways, levels, schools) is missing. The
    program and TVET catalogs are optional today and reported for
    transparency.
    """

    ready: bool
    open_academic_year: OpenAcademicYearSummary | None
    pathways_available: bool
    education_levels_available: bool
    pathway_level_mappings_available: bool
    program_catalog_available: bool
    school_catalog_available: bool
    tvet_catalog_available: bool


class RegistrationRead(BaseModel):
    """Registration summary: ids + human-readable catalog context.

    ``program_*`` fields are ``None`` when the enrollment was created
    without a program version (allowed by the schema while the program
    catalog is being populated). ``subjects`` lists the student subjects
    derived from the selected program version, if any.
    """

    enrollment_id: UUID
    student_id: UUID

    academic_year_id: UUID
    academic_year: str

    pathway_id: UUID
    pathway_code: str
    pathway_name: str

    education_level_id: UUID
    level_code: str
    level_name: str

    program_version_id: UUID | None = None
    program_code: str | None = None
    program_name: str | None = None

    school_id: UUID
    school_name: str
    school_code: str | None = None

    status: str
    started_at: datetime
    created_at: datetime
    subjects: list[RegistrationSubjectRead] = Field(default_factory=list)


class RegistrationStatusUpdate(BaseModel):
    """Request body for an administrative status transition.

    ``extra="forbid"`` rejects unknown keys with a 422, and ``status``
    must be one of the model's own ``EnrollmentStatus`` states (anything
    else is a 422 before the handler runs). *Whether* the transition from
    the current status to the requested one is allowed is a service rule:
    a refused move is a 409 naming both statuses, never a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    status: EnrollmentStatus
