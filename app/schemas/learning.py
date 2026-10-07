"""Pydantic schemas for the Phase 1 learning marketplace.

Boundary: publishing and maintaining teaching offerings (teacher
self-service), discovering them (marketplace) and enrolling into / leaving
them (student self-service).

Security rules baked into these schemas:

- no request field can select a *teacher* or *student*: both identities
  come from the Bearer token, so a body can only ever describe the thing
  being taught (context) or the caller's own enrollment;
- ``extra="forbid"`` rejects unknown keys with a 422 — including
  ``teacher_id``, ``student_id`` and anything role-shaped;
- no response schema includes credential material.

The context fields (``academic_year`` … ``subject``) are flattened onto
every read like ``RegistrationRead`` does, so one response never needs a
second request to the catalog.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import TeachingOfferingStatus


class TeachingOfferingCreate(BaseModel):
    """Publish an offering (``POST /me/teacher/offerings``).

    The context is expressed with ids because the *teacher* picks it: an
    academic year, a pathway, an education level, an optional program
    version and a subject. The service proves the tuple is coherent
    (pathway spans the level, program version belongs to the tuple, the
    subject is taught by that program version) and creates the shared
    ``learning_contexts`` row the first time it is seen.
    """

    model_config = ConfigDict(extra="forbid")

    academic_year_id: UUID
    pathway_id: UUID
    education_level_id: UUID
    program_version_id: UUID | None = None
    subject_id: UUID
    description: str | None = Field(default=None, max_length=2000)


class TeachingOfferingUpdate(BaseModel):
    """Amend one of the caller's offerings (``PATCH .../offerings/{id}``).

    At least one field must be supplied. ``description`` changes the text
    (an explicit ``null`` clears it); ``status`` drives the
    active → paused → archived lifecycle. Both may be sent together, but
    the status — when present — must name a *different* value, so a
    no-op PATCH is a 409 instead of a silent 200.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2000)
    status: TeachingOfferingStatus | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "TeachingOfferingUpdate":
        supplied = self.model_fields_set
        if not supplied:
            raise ValueError("supply at least one of: description, status")
        if "status" in supplied and self.status is None:
            raise ValueError("status must not be null")
        return self


class LearningContextRead(BaseModel):
    """The context a marketplace/consumer row is about (flattened catalog)."""

    learning_context_id: UUID
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
    subject_id: UUID
    subject_code: str
    subject_name: str


class TeachingOfferingRead(LearningContextRead):
    """One teaching offering — the shape both the teacher's own list and the
    marketplace return, so a student and a teacher read the same object."""

    offering_id: UUID
    teacher_id: UUID
    teacher_name: str
    description: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class LearningEnrollmentRead(LearningContextRead):
    """One of the caller's learning enrollments, with its offering attached."""

    enrollment_id: UUID
    student_id: UUID
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    created_at: datetime
    offering_id: UUID
    offering_status: str
    offering_description: str | None = None
    teacher_name: str


class OfferingStudentRead(BaseModel):
    """One student enrolled in ONE of the caller's own offerings.

    The whole of a teacher's student visibility: the row exists only
    because this student joined this teaching offering. It carries no
    contact/PII beyond the name and login email, no search/filter fields,
    and it can never describe a student of another offering — the offering
    id comes from the path and is ownership-checked by the service. Reads
    never expose another student's data: progress, registrations and
    material access stay on the student's own surface.
    """

    enrollment_id: UUID
    student_id: UUID
    full_name: str
    #: The account email of the enrolled student (``users.email`` is
    #: nullable in the schema, so this may be null).
    email: str | None = None
    enrollment_status: str
    started_at: datetime
    ended_at: datetime | None = None
