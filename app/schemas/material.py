"""Pydantic schemas for Phase 2 slice 2B — teaching materials + moderation.

Boundary: the teacher's own materials under a teaching offering, and the
administrator's moderation of those materials. Materials do not re-declare
the Admin-owned catalog — teacher/academic-year/pathway/level/subject are
derivable through ``teaching_offerings`` → ``learning_contexts``.

Security rules baked into these schemas:

- no request field can select a *teacher*, *offering* or *file asset*: all
  come from the URL path, the Bearer token or the upload pipeline, so a
  JSON body can only ever describe the thing being created/amended;
- ``extra="forbid"`` rejects unknown keys with a 422;
- the create/upload request is multipart (Form fields + one file), not a
  JSON body — ``MaterialCreateForm`` documents the accepted fields;
- no response schema includes credential material or storage paths —
  ``storage_key`` is never returned to a client.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import MaterialStatus, MaterialType


class FileAssetRead(BaseModel):
    """Storage-adjacent metadata for one uploaded file (never a path/URL)."""

    file_asset_id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    validation_status: str
    validation_error: str | None = None
    created_at: datetime
    updated_at: datetime


class MaterialRead(BaseModel):
    """One material — the shape both the teacher's own list and admin read."""

    material_id: UUID
    offering_id: UUID
    lesson_id: UUID | None = None
    title: str
    description: str | None = None
    material_type: MaterialType
    status: MaterialStatus
    file_asset: FileAssetRead
    created_at: datetime
    updated_at: datetime


class MaterialUpdate(BaseModel):
    """Amend one of the caller's draft or rejected materials.

    Published materials are effectively immutable in the MVP: the service
    answers 409 rather than silently changing a live artifact. At least
    one field must be supplied.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    material_type: MaterialType | None = None
    lesson_id: UUID | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "MaterialUpdate":
        supplied = self.model_fields_set
        if not supplied:
            raise ValueError(
                "supply at least one of: title, description, material_type, lesson_id"
            )
        if "title" in supplied and self.title is None:
            raise ValueError("title must not be null")
        if "material_type" in supplied and self.material_type is None:
            raise ValueError("material_type must not be null")
        return self


class MaterialRejectRequest(BaseModel):
    """Administrator rejection of a pending material (reason is mandatory)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class MaterialModerationRead(BaseModel):
    """One administrator decision recorded against a material."""

    moderation_id: UUID
    material_id: UUID
    reviewer_user_id: UUID
    decision: str
    reason: str | None = None
    created_at: datetime


class MaterialDetailRead(MaterialRead):
    """Teacher/admin detail: the material plus its full moderation trail."""

    moderations: list[MaterialModerationRead] = Field(default_factory=list)
