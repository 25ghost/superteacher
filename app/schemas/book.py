"""Pydantic schemas for the administrator book library (book P1).

Boundary: administrator-only intake and inspection. No request field can
select an uploader, a file asset or a status — all come from the URL
path, the Bearer token or the upload pipeline. The upload request is
multipart (form fields + one PDF file), mirroring the teacher-material
convention. No response schema ever includes a ``storage_key`` or any
other filesystem path, and the original PDF is never served to a student
surface (P1 exposes none at all).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import BookRevisionStatus
from app.schemas.material import FileAssetRead


class BookRevisionRead(BaseModel):
    """One uploaded edition of a book (never a path/URL).

    P2 adds the extraction outcome as plain metadata: ``status`` already
    tells the administrator where the revision is in the pipeline, and
    these two fields tell them what happened — ``extraction_summary`` is
    the parsed extraction report (document title, page/unit counts, fixed
    warnings) once the sweeper finished, ``extraction_error`` the fixed
    ``code: message`` why it failed. NULL until extraction runs; never
    file bytes, never a storage path, never extracted document text.
    """

    revision_id: UUID
    book_id: UUID
    revision_number: int
    status: BookRevisionStatus
    uploaded_by_user_id: UUID
    file_asset: FileAssetRead
    extraction_summary: dict | None = None
    extraction_error: str | None = None
    created_at: datetime
    updated_at: datetime


class BookRead(BaseModel):
    """One book with its canonical contexts and latest revision."""

    book_id: UUID
    title: str
    description: str | None = None
    created_by_user_id: UUID
    learning_context_ids: list[UUID]
    revision: BookRevisionRead
    created_at: datetime
    updated_at: datetime
