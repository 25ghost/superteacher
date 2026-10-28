"""Pydantic schemas for the administrator book library (book P1-P3).

Boundary: administrator-only intake, inspection and review. No request
field can select an uploader, a file asset or a status — all come from
the URL path, the Bearer token or the upload pipeline. The upload request
is multipart (form fields + one PDF file), mirroring the teacher-material
convention. No response schema ever includes a ``storage_key`` or any
other filesystem path, and the original PDF is never served to a student
surface (no phase exposes one).

P3 adds the review surface: ``BookUnitRead`` (one extracted unit with its
kind, ordered content, source-page range, extraction warnings, assessment
designation and durable review state), ``BookRevisionDetailRead`` (the
revision plus its ordered unit tree), and the two action payloads —
``BookUnitCorrectRequest`` (partial, every field optional) and
``BookRevisionRejectRequest`` (mandatory reason, mirroring
``MaterialRejectRequest``).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    AssessmentRequirement,
    BookRevisionStatus,
    BookUnitKind,
    BookUnitReviewStatus,
)
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

    P3 adds ``is_current_publication``: TRUE only for the book's current
    published revision, so a historical published edition (status still
    ``published``, flag false) stays distinguishable from the live one.
    """

    revision_id: UUID
    book_id: UUID
    revision_number: int
    status: BookRevisionStatus
    uploaded_by_user_id: UUID
    file_asset: FileAssetRead
    extraction_summary: dict | None = None
    extraction_error: str | None = None
    is_current_publication: bool = False
    created_at: datetime
    updated_at: datetime


class BookUnitRead(BaseModel):
    """One extracted unit with its review state (metadata + its own text).

    ``content`` is the unit's OWN extracted text (that is the point of a
    correction surface); ``storage_key`` and every other filesystem path
    are structurally absent. ``extraction_warnings`` is a list of the
    fixed codes (empty when clean) and survives approval — approving a
    unit never erases where the parser struggled. The flat
    ``parent_unit_id`` reference lets a client rebuild the tree; the list
    on the detail read is ordered parents-before-children.
    """

    unit_id: UUID
    parent_unit_id: UUID | None
    kind: BookUnitKind
    title: str
    content: str
    display_order: int
    source_page_start: int
    source_page_end: int
    extraction_warnings: list[str]
    assessment_requirement: AssessmentRequirement
    review_status: BookUnitReviewStatus
    reviewed_by_user_id: UUID | None = None
    reviewed_at: datetime | None = None
    #: P4: the exercise set's passing threshold (percent). NULL means
    #: the default 70; only ever set on ``exercise_set`` units (the
    #: database CHECK proves it).
    passing_threshold_percent: int | None = None
    created_at: datetime
    updated_at: datetime


class BookRevisionDetailRead(BookRevisionRead):
    """One revision with its complete ordered unit tree (admin inspection).

    ``units`` is depth-first (every parent precedes its children) with
    sibling ``display_order`` preserved, so the hierarchy, warnings,
    page ranges, assessment designations and review states are all
    visible in one read. ``rejection_reason`` is the recorded reason
    when the revision was rejected; NULL otherwise.
    """

    rejection_reason: str | None = None
    units: list[BookUnitRead]


class BookUnitCorrectRequest(BaseModel):
    """Partial correction of ONE unit of a ``pending_review`` revision.

    Every field is optional and means "leave unchanged" when absent —
    an empty body is a 422 (there is nothing to correct). An explicit
    ``null`` is only meaningful for ``parent_unit_id`` (move the unit to
    the root); for every other field it is a 422 rather than a silent
    no-op. Unknown keys are refused (422), mirroring every other write
    schema in this API.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = None
    kind: BookUnitKind | None = None
    display_order: int | None = Field(default=None, ge=1)
    parent_unit_id: UUID | None = None
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)
    assessment_requirement: AssessmentRequirement | None = None


class BookRevisionRejectRequest(BaseModel):
    """Administrator rejection of a pending revision (reason is mandatory).

    Mirrors ``MaterialRejectRequest``: the reason is recorded on the
    revision and in the audit trail; blank/whitespace-only text is
    refused by the service before anything is written.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


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
