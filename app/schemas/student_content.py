"""Pydantic schemas for Phase 2 slice 2C — student content access + progress.

Boundary: the *student's own* view of an offering they hold an ACTIVE
learning enrollment for. Drafts, rejected materials, moderation metadata
and storage paths are never part of these shapes.

Security rules baked into these schemas:

- no request field can select a *student*, *offering* or *enrollment*:
  identity comes from the Bearer token and the path; a body can only ever
  describe progress being recorded;
- ``extra="forbid"`` rejects unknown keys with a 422;
- file metadata is intentionally thin — original filename, content type
  and size only; ``storage_key`` and validation internals stay server-side;
- ``content_url`` is a *relative application path*, never a permanent
  storage URL — authorization is re-checked on every content fetch.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import MaterialProgressStatus, MaterialType


class StudentTopicRead(BaseModel):
    """One topic under the student's enrolled offering (structure only)."""

    topic_id: UUID
    offering_id: UUID
    title: str
    description: str | None = None
    display_order: int
    created_at: datetime
    updated_at: datetime


class StudentLessonRead(BaseModel):
    """One lesson under the student's enrolled offering (structure only)."""

    lesson_id: UUID
    topic_id: UUID
    offering_id: UUID
    title: str
    description: str | None = None
    display_order: int
    created_at: datetime
    updated_at: datetime


class MaterialProgressRead(BaseModel):
    """One student's progress on one material -- never another student's.

    When no progress row exists yet, the service still answers 200 with
    ``status=not_started`` and ``progress_id=None`` so clients always have
    a status to render without writing a row on a read.
    """

    progress_id: UUID | None = None
    material_id: UUID
    student_id: UUID
    status: MaterialProgressStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MaterialProgressUpdate(BaseModel):
    """Record progress on a published material you may access.

    ``not_started`` is implied by absence; the update body names the state
    the student is moving to. Moving backwards is a 409; re-sending the
    current status is idempotent (200).
    """

    model_config = ConfigDict(extra="forbid")

    status: MaterialProgressStatus


class StudentMaterialFileRead(BaseModel):
    """Thin file metadata for an authorized material (no storage path)."""

    file_asset_id: UUID
    original_filename: str
    content_type: str
    size_bytes: int


class StudentMaterialRead(BaseModel):
    """One published material the student may access (progress attached)."""

    material_id: UUID
    offering_id: UUID
    lesson_id: UUID | None = None
    title: str
    description: str | None = None
    material_type: MaterialType
    status: str  # always "published" on this surface
    file: StudentMaterialFileRead
    #: Relative application path — authorization is re-checked on fetch.
    content_url: str
    progress: MaterialProgressRead | None = None
    created_at: datetime
    updated_at: datetime


class LearningContentOverviewRead(BaseModel):
    """The student's entry point for one ACTIVE learning enrollment."""

    enrollment_id: UUID
    offering_id: UUID
    offering_status: str
    offering_description: str | None = None
    teacher_name: str
    learning_context_id: UUID
    academic_year: str
    pathway_code: str
    pathway_name: str
    level_code: str
    level_name: str
    subject_code: str
    subject_name: str
    topic_count: int
    lesson_count: int
    published_material_count: int
