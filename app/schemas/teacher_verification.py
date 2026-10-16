"""Pydantic schemas for the teacher verification application workflow.

Boundary: the applicant self-service (start/edit/submit an application,
attach evidence, read the approved scope) and the administrator review
surface (list/detail, evidence file download, recorded checks and
assessments, per-context decisions).

Security rules baked into these schemas:

- no request field selects a *teacher*: the applicant identity comes from
  the Bearer token; an application row always belongs to the caller;
- ``extra="forbid"`` rejects unknown keys with a 422;
- no response schema exposes ``credential_reference`` (sensitive,
  server-side only) or a storage key / file path — evidence bytes leave
  only through the dedicated private download endpoint;
- ``category`` is a *claim* until approval; the approved value lives on
  ``teachers.verification_category`` and is never written from a request.
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    ApplicationContextStatus,
    AssessmentOutcome,
    TeacherApplicationStatus,
    TeacherEvidenceStatus,
    TeacherEvidenceType,
    TeacherVerificationCategory,
    VerificationCheckOutcome,
    VerificationMethod,
)


class ApplicationContextInput(BaseModel):
    """One requested canonical learning context (resolved server-side)."""

    model_config = ConfigDict(extra="forbid")

    academic_year_id: UUID
    pathway_id: UUID
    education_level_id: UUID
    program_version_id: UUID | None = None
    subject_id: UUID


class ApplicationStart(BaseModel):
    """Start (or re-start after a terminal state) an application."""

    model_config = ConfigDict(extra="forbid")

    category: TeacherVerificationCategory
    claimed_school_id: UUID | None = None
    statement: str | None = Field(default=None, max_length=4000)


class ApplicationUpdate(BaseModel):
    """Amend the claimed profile of a draft / needs-information application."""

    model_config = ConfigDict(extra="forbid")

    category: TeacherVerificationCategory | None = None
    claimed_school_id: UUID | None = None
    statement: str | None = Field(default=None, max_length=4000)
    misrepresentation_declaration: bool | None = None


class ContextsReplace(BaseModel):
    """Replace the full set of requested contexts (canonical tuples)."""

    model_config = ConfigDict(extra="forbid")

    contexts: list[ApplicationContextInput] = Field(min_length=1, max_length=20)


class EvidenceUpload(BaseModel):
    """Metadata accompanying one evidence file upload."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: TeacherEvidenceType
    title: str = Field(min_length=1, max_length=200)
    issuing_organization: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=80)
    #: Server-side only — never echoed in ordinary responses.
    credential_reference: str | None = Field(default=None, max_length=200)
    #: Requested contexts this document substantiates (must be a subset
    #: of the application's requested contexts).
    learning_context_ids: list[UUID] = Field(min_length=1, max_length=20)


class ApplicationContextRead(BaseModel):
    """One requested context with its decision."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    learning_context_id: UUID
    status: ApplicationContextStatus
    decision_reason: str | None = None
    decided_at: datetime | None = None


class EvidenceRead(BaseModel):
    """One evidence document as the applicant or reviewer sees it.

    ``credential_reference`` is deliberately absent — it never leaves the
    server outside an authorized reviewer's direct database access.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    evidence_type: TeacherEvidenceType
    status: TeacherEvidenceStatus
    title: str
    issuing_organization: str | None = None
    country: str | None = None
    duplicate_indicator: bool
    learning_context_ids: list[UUID]
    created_at: datetime


class VerificationCheckRead(BaseModel):
    """One recorded reviewer check (administrative read)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    evidence_id: UUID
    reviewer_user_id: UUID
    seq: int
    method: VerificationMethod
    outcome: VerificationCheckOutcome
    source_institution: str
    notes: str | None = None
    created_at: datetime


class AssessmentRead(BaseModel):
    """One recorded subject assessment outcome (administrative read)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    subject_id: UUID
    assessment_date: date
    assessor: str
    outcome: AssessmentOutcome
    notes: str | None = None
    created_at: datetime


class ApplicationRead(BaseModel):
    """One application as its applicant sees it."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    teacher_id: UUID
    status: TeacherApplicationStatus
    category: TeacherVerificationCategory | None = None
    claimed_school_id: UUID | None = None
    misrepresentation_declaration: bool
    statement: str | None = None
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    previous_application_id: UUID | None = None
    contexts: list[ApplicationContextRead]
    evidence: list[EvidenceRead]
    created_at: datetime
    updated_at: datetime


class ApplicationAdminRead(ApplicationRead):
    """Administrative detail: adds the full review trail."""

    model_config = ConfigDict(extra="forbid")

    checks: list[VerificationCheckRead]
    assessments: list[AssessmentRead]


class ApplicationSummaryRead(BaseModel):
    """One row of the administrative queue."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    teacher_id: UUID
    teacher_name: str
    status: TeacherApplicationStatus
    category: TeacherVerificationCategory | None = None
    submitted_at: datetime | None = None
    created_at: datetime


class ScopeContextRead(BaseModel):
    """One entry of the canonical approved teaching scope."""

    model_config = ConfigDict(extra="forbid")

    learning_context_id: UUID
    application_id: UUID
    approved_at: datetime


class VerificationCheckRecord(BaseModel):
    """Record one reviewer check on one evidence document."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID
    method: VerificationMethod
    outcome: VerificationCheckOutcome
    source_institution: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)


class AssessmentRecord(BaseModel):
    """Record the outcome of a subject-specific assessment."""

    model_config = ConfigDict(extra="forbid")

    subject_id: UUID
    assessment_date: date
    assessor: str = Field(min_length=1, max_length=200)
    outcome: AssessmentOutcome
    notes: str | None = Field(default=None, max_length=4000)


class InformationRequest(BaseModel):
    """Ask the applicant for specific corrections."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)


class ContextDecision(BaseModel):
    """One requested context's explicit review outcome."""

    model_config = ConfigDict(extra="forbid")

    learning_context_id: UUID
    status: ApplicationContextStatus
    reason: str | None = Field(default=None, max_length=4000)


class ApplicationDecision(BaseModel):
    """Approve (fully or a reviewed subset) or reject an application.

    For approval every requested context must appear here with an explicit
    outcome; contexts approved here become the teacher's approved scope.
    For rejection a single ``reason`` is required and no context outcomes
    are supplied.
    """

    model_config = ConfigDict(extra="forbid")

    approve: bool
    reason: str | None = Field(default=None, max_length=4000)
    contexts: list[ContextDecision] = Field(default_factory=list)
