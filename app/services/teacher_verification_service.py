"""Teacher verification application workflow (policy enforcement core).

Registration and verification stay separate: creating a teacher account
never creates an application, and an application grants nothing until it
is ``approved`` with an explicitly approved per-context scope. This module
owns the whole deterministic workflow — no AI ever decides an outcome;
every decision is a recorded human reviewer action, and every teaching
authorization later reads only durable database state (teachers row +
teacher_approved_contexts).

Applicant flow (self-service, Bearer identity):

    start → (edit profile / replace contexts / attach evidence)*
         → submit → (needs_information → edit → resubmit)*
         → approved | rejected, or withdrawn at any non-terminal state.

Re-application after a terminal state is a NEW row whose
``previous_application_id`` preserves the decision chain; at most one
non-terminal application per teacher (partial unique index).

Reviewer flow (administrative):

    record checks on each evidence → record assessments (experience
    route) → request information or decide. Approval requires every
    non-removed evidence's LATEST check to be ``verified``, the
    category's evidence rules, and an explicit outcome per requested
    context (reason required when rejected). Approved contexts become
    ``teacher_approved_contexts`` rows — the canonical approved scope.

Nothing commits here — the API layer owns the transaction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models.enums import (
    ApplicationContextStatus,
    FileAssetStatus,
    ProgramType,
    TeacherApplicationStatus,
    TeacherEvidenceStatus,
    TeacherEvidenceType,
    TeacherVerificationCategory,
    TeacherVerificationStatus,
    UserRole,
    VerificationCheckOutcome,
)
from app.models.file_asset import FileAsset
from app.models.learning_context import LearningContext
from app.models.program import Program
from app.models.program_version import ProgramVersion
from app.models.school import School
from app.models.teacher import Teacher
from app.models.teacher_application import TeacherApplication
from app.models.teacher_application_context import TeacherApplicationContext
from app.models.teacher_approved_context import TeacherApprovedContext
from app.models.teacher_assessment import TeacherAssessment
from app.models.teacher_evidence import TeacherEvidence
from app.models.teacher_evidence_context import TeacherEvidenceContext
from app.models.teacher_verification_check import TeacherVerificationCheck
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import file_asset_repository as file_asset_repo
from app.schemas.teacher_verification import (
    ApplicationAdminRead,
    ApplicationDecision,
    ApplicationRead,
    ApplicationStart,
    ApplicationSummaryRead,
    ApplicationUpdate,
    ApplicationContextRead,
    AssessmentRead,
    AssessmentRecord,
    ContextsReplace,
    EvidenceRead,
    EvidenceUpload,
    InformationRequest,
    ScopeContextRead,
    VerificationCheckRead,
    VerificationCheckRecord,
)
from app.services import evidence_validation, learning_context_service
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)
from app.services.storage import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

#: Non-terminal application states (the partial unique index mirrors this).
_ACTIVE_STATUSES = (
    TeacherApplicationStatus.DRAFT.value,
    TeacherApplicationStatus.SUBMITTED.value,
    TeacherApplicationStatus.NEEDS_INFORMATION.value,
)

#: Allowed status moves, as ``current -> frozenset(next ...)``.
_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    TeacherApplicationStatus.DRAFT.value: frozenset(
        {
            TeacherApplicationStatus.SUBMITTED.value,
            TeacherApplicationStatus.WITHDRAWN.value,
        }
    ),
    TeacherApplicationStatus.SUBMITTED.value: frozenset(
        {
            TeacherApplicationStatus.NEEDS_INFORMATION.value,
            TeacherApplicationStatus.APPROVED.value,
            TeacherApplicationStatus.REJECTED.value,
            TeacherApplicationStatus.WITHDRAWN.value,
        }
    ),
    TeacherApplicationStatus.NEEDS_INFORMATION.value: frozenset(
        {
            TeacherApplicationStatus.SUBMITTED.value,
            TeacherApplicationStatus.WITHDRAWN.value,
        }
    ),
    TeacherApplicationStatus.APPROVED.value: frozenset(),
    TeacherApplicationStatus.REJECTED.value: frozenset(),
    TeacherApplicationStatus.WITHDRAWN.value: frozenset(),
}

#: Evidence that substantiates a context (tutor coverage / TVET rules).
_COVERING_EVIDENCE_TYPES = frozenset(
    {
        TeacherEvidenceType.ACADEMIC_QUALIFICATION.value,
        TeacherEvidenceType.VOCATIONAL_QUALIFICATION.value,
        TeacherEvidenceType.PROFESSIONAL_EXPERIENCE.value,
    }
)

#: Evidence types that can cover a TVET (vocational) context.
_TVET_EVIDENCE_TYPES = frozenset(
    {
        TeacherEvidenceType.VOCATIONAL_QUALIFICATION.value,
        TeacherEvidenceType.PROFESSIONAL_EXPERIENCE.value,
    }
)

_EVIDENCE_EAGER = (
    joinedload(TeacherEvidence.contexts),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(
    session: Session,
    *,
    user_id: uuid.UUID,
    event_type: str,
    payload: dict,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    auth_event_repo.log_event(
        session,
        user_id=user_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        metadata_json=json.dumps(payload),
    )


def _teacher_subject_id(session: Session, teacher_id: uuid.UUID) -> uuid.UUID:
    """The user-account id behind a teacher profile (the audit subject).

    ``auth_events.user_id`` references ``users.id`` — workflow events are
    attributed to the teacher's *account*, never the profile row id.
    """
    user_id = session.scalar(
        select(Teacher.user_id).where(Teacher.id == teacher_id)
    )
    if user_id is None:
        raise LearningNotFoundError("no teacher profile for this application")
    return user_id


# --------------------------------------------------------------------------
# loading helpers
# --------------------------------------------------------------------------


def _load_teacher_profile(session: Session, user: User) -> Teacher:
    """The caller's teacher profile (404 when the account has none)."""
    if user.role != UserRole.TEACHER.value:
        raise LearningForbiddenError("teacher role required for this operation")
    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    if profile is None:
        raise LearningNotFoundError("no teacher profile for this account")
    return profile


def _load_owned_application(
    session: Session, user: User, application_id: uuid.UUID
) -> TeacherApplication:
    """One application owned by the caller (404 unknown-or-foreign)."""
    profile = _load_teacher_profile(session, user)
    application = session.get(TeacherApplication, application_id)
    if application is None or application.teacher_id != profile.id:
        raise LearningNotFoundError("no teacher application with this id")
    return application


def _load_application_detail(
    session: Session, application_id: uuid.UUID
) -> TeacherApplication:
    """One application with every review relation loaded (404 unknown).

    ``populate_existing`` is required: callers such as ``replace_contexts``
    mutate the context/evidence collections through the same session and
    then re-read through this loader; without it the identity map would
    return the stale collections already loaded on the cached instance
    and the response would silently omit the freshly written rows.
    """
    stmt = (
        select(TeacherApplication)
        .options(
            joinedload(TeacherApplication.contexts),
            joinedload(TeacherApplication.evidence).options(
                joinedload(TeacherEvidence.contexts)
            ),
            joinedload(TeacherApplication.checks),
            joinedload(TeacherApplication.assessments),
        )
        .where(TeacherApplication.id == application_id)
        .execution_options(populate_existing=True)
    )
    application = session.execute(stmt).unique().scalar_one_or_none()
    if application is None:
        raise LearningNotFoundError("no teacher application with this id")
    return application


def _active_application(
    session: Session, teacher_id: uuid.UUID
) -> TeacherApplication | None:
    """The teacher's non-terminal application, if any."""
    stmt = (
        select(TeacherApplication)
        .options(
            joinedload(TeacherApplication.contexts),
            joinedload(TeacherApplication.evidence).options(
                joinedload(TeacherEvidence.contexts)
            ),
        )
        .where(
            TeacherApplication.teacher_id == teacher_id,
            TeacherApplication.status.in_(_ACTIVE_STATUSES),
        )
    )
    return session.execute(stmt).unique().scalar_one_or_none()


def _latest_application(
    session: Session, teacher_id: uuid.UUID
) -> TeacherApplication | None:
    """The teacher's most recent application of any state, if any."""
    stmt = (
        select(TeacherApplication)
        .options(
            joinedload(TeacherApplication.contexts),
            joinedload(TeacherApplication.evidence).options(
                joinedload(TeacherEvidence.contexts)
            ),
        )
        .where(TeacherApplication.teacher_id == teacher_id)
        .order_by(TeacherApplication.created_at.desc(), TeacherApplication.id)
    )
    return session.execute(stmt).unique().scalars().first()


def _require_transition(application: TeacherApplication, target: str) -> None:
    allowed = _STATUS_TRANSITIONS.get(application.status, frozenset())
    if target not in allowed:
        raise LearningConflictError(
            f"application is {application.status!r}; it cannot move to {target!r}"
        )


def _live_evidence(application: TeacherApplication) -> list[TeacherEvidence]:
    return [
        e
        for e in application.evidence
        if e.status != TeacherEvidenceStatus.REMOVED.value
    ]


def _latest_check_outcome(
    session: Session, evidence_id: uuid.UUID
) -> str | None:
    """Outcome of the highest-``seq`` check on one evidence (or None)."""
    stmt = select(TeacherVerificationCheck.outcome).where(
        TeacherVerificationCheck.evidence_id == evidence_id
    )
    outcomes = list(session.scalars(stmt))
    if not outcomes:
        return None
    # seq is monotonic per evidence; re-read seqs to order without
    # trusting timestamps (they may tie inside one transaction).
    stmt = select(TeacherVerificationCheck.seq, TeacherVerificationCheck.outcome).where(
        TeacherVerificationCheck.evidence_id == evidence_id
    )
    pairs = list(session.execute(stmt))
    if not pairs:
        return None
    return max(pairs, key=lambda p: p[0])[1]


def _evidence_types_for_context(
    session: Session, application_id: uuid.UUID, learning_context_id: uuid.UUID
) -> set[str]:
    """Non-removed evidence types explicitly linked to one context."""
    stmt = (
        select(TeacherEvidence.evidence_type)
        .join(TeacherEvidenceContext, TeacherEvidenceContext.evidence_id == TeacherEvidence.id)
        .where(
            TeacherEvidence.application_id == application_id,
            TeacherEvidence.status != TeacherEvidenceStatus.REMOVED.value,
            TeacherEvidenceContext.learning_context_id == learning_context_id,
        )
    )
    return set(session.scalars(stmt))


def _is_tvet_context(session: Session, learning_context_id: uuid.UUID) -> bool:
    """True when the canonical context sits under a TVET program."""
    stmt = (
        select(Program.program_type)
        .join(ProgramVersion, ProgramVersion.program_id == Program.id)
        .join(
            LearningContext,
            LearningContext.program_version_id == ProgramVersion.id,
        )
        .where(LearningContext.id == learning_context_id)
    )
    return session.scalar(stmt) == ProgramType.TVET_PROGRAM.value


def _context_subject_id(
    session: Session, learning_context_id: uuid.UUID
) -> uuid.UUID | None:
    context = session.get(LearningContext, learning_context_id)
    return context.subject_id if context is not None else None


def _has_passed_assessment(
    session: Session, application_id: uuid.UUID, subject_id: uuid.UUID
) -> bool:
    stmt = select(TeacherAssessment.id).where(
        TeacherAssessment.application_id == application_id,
        TeacherAssessment.subject_id == subject_id,
        TeacherAssessment.outcome == "passed",
    )
    return session.scalar(stmt) is not None


# --------------------------------------------------------------------------
# read builders
# --------------------------------------------------------------------------


def _read_context(row: TeacherApplicationContext) -> ApplicationContextRead:
    return ApplicationContextRead(
        id=row.id,
        learning_context_id=row.learning_context_id,
        status=row.status,
        decision_reason=row.decision_reason,
        decided_at=row.decided_at,
    )


def _read_evidence(e: TeacherEvidence) -> EvidenceRead:
    return EvidenceRead(
        id=e.id,
        evidence_type=e.evidence_type,
        status=e.status,
        title=e.title,
        issuing_organization=e.issuing_organization,
        country=e.country,
        duplicate_indicator=e.duplicate_indicator,
        learning_context_ids=[c.learning_context_id for c in e.contexts],
        created_at=e.created_at,
    )


def _read_application(application: TeacherApplication) -> ApplicationRead:
    return ApplicationRead(
        id=application.id,
        teacher_id=application.teacher_id,
        status=application.status,
        category=application.category,
        claimed_school_id=application.claimed_school_id,
        misrepresentation_declaration=application.misrepresentation_declaration,
        statement=application.statement,
        submitted_at=application.submitted_at,
        decided_at=application.decided_at,
        decision_reason=application.decision_reason,
        previous_application_id=application.previous_application_id,
        contexts=[_read_context(c) for c in application.contexts],
        evidence=[_read_evidence(e) for e in application.evidence],
        created_at=application.created_at,
        updated_at=application.updated_at,
    )


def _read_check(c: TeacherVerificationCheck) -> VerificationCheckRead:
    return VerificationCheckRead(
        id=c.id,
        evidence_id=c.evidence_id,
        reviewer_user_id=c.reviewer_user_id,
        seq=c.seq,
        method=c.method,
        outcome=c.outcome,
        source_institution=c.source_institution,
        notes=c.notes,
        created_at=c.created_at,
    )


def _read_assessment(a: TeacherAssessment) -> AssessmentRead:
    return AssessmentRead(
        id=a.id,
        subject_id=a.subject_id,
        assessment_date=a.assessment_date,
        assessor=a.assessor,
        outcome=a.outcome,
        notes=a.notes,
        created_at=a.created_at,
    )


def _read_admin(application: TeacherApplication) -> ApplicationAdminRead:
    base = _read_application(application)
    return ApplicationAdminRead(
        **base.model_dump(),
        checks=[_read_check(c) for c in application.checks],
        assessments=[_read_assessment(a) for a in application.assessments],
    )


# --------------------------------------------------------------------------
# applicant self-service
# --------------------------------------------------------------------------


def start_application(
    session: Session, user: User, payload: ApplicationStart
) -> ApplicationRead:
    """Open a draft application (or 409 when one is already non-terminal)."""
    profile = _load_teacher_profile(session, user)

    existing = _active_application(session, profile.id)
    if existing is not None:
        raise LearningConflictError(
            "you already have an open teacher application; edit it instead"
        )

    if payload.claimed_school_id is not None:
        school = session.get(School, payload.claimed_school_id)
        if school is None:
            raise LearningNotFoundError("no school with this id")

    previous = session.scalar(
        select(TeacherApplication)
        .where(TeacherApplication.teacher_id == profile.id)
        .order_by(TeacherApplication.created_at.desc(), TeacherApplication.id)
        .limit(1)
    )

    application = TeacherApplication(
        teacher_id=profile.id,
        status=TeacherApplicationStatus.DRAFT.value,
        category=payload.category.value,
        claimed_school_id=payload.claimed_school_id,
        statement=payload.statement,
        previous_application_id=previous.id if previous is not None else None,
        created_by=user.id,
    )
    session.add(application)
    try:
        session.flush()
    except IntegrityError as exc:
        raise LearningConflictError(
            "you already have an open teacher application; edit it instead"
        ) from exc

    _log(
        session,
        user_id=user.id,
        event_type="application_created",
        payload={
            "application_id": str(application.id),
            "category": application.category,
            "previous_application_id": (
                str(application.previous_application_id)
                if application.previous_application_id
                else None
            ),
        },
    )
    session.flush()
    return _read_application(_load_application_detail(session, application.id))


def get_my_application(session: Session, user: User) -> ApplicationRead | None:
    """The caller's active application, else their latest one (or None)."""
    profile = _load_teacher_profile(session, user)
    application = _active_application(session, profile.id)
    if application is None:
        application = _latest_application(session, profile.id)
    if application is None:
        return None
    return _read_application(_load_application_detail(session, application.id))


def update_application(
    session: Session,
    user: User,
    application_id: uuid.UUID,
    payload: ApplicationUpdate,
) -> ApplicationRead:
    """Amend the claimed profile of a draft / needs-information application."""
    application = _load_owned_application(session, user, application_id)
    if application.status not in (
        TeacherApplicationStatus.DRAFT.value,
        TeacherApplicationStatus.NEEDS_INFORMATION.value,
    ):
        raise LearningConflictError(
            f"application is {application.status!r}; only a draft or "
            "needs-information application may be edited"
        )

    if (
        payload.category is not None
        and payload.category.value != application.category
    ):
        _log(
            session,
            user_id=user.id,
            event_type="application_category_changed",
            payload={
                "application_id": str(application.id),
                "from": application.category,
                "to": payload.category.value,
            },
        )
        application.category = payload.category.value

    if (
        payload.claimed_school_id is not None
        and payload.claimed_school_id != application.claimed_school_id
    ):
        school = session.get(School, payload.claimed_school_id)
        if school is None:
            raise LearningNotFoundError("no school with this id")
        application.claimed_school_id = payload.claimed_school_id

    if payload.statement is not None:
        application.statement = payload.statement

    if payload.misrepresentation_declaration is not None:
        application.misrepresentation_declaration = (
            payload.misrepresentation_declaration
        )

    session.flush()
    return _read_application(_load_application_detail(session, application.id))


def replace_contexts(
    session: Session,
    user: User,
    application_id: uuid.UUID,
    payload: ContextsReplace,
) -> ApplicationRead:
    """Replace the full requested-context set (canonical tuples only)."""
    application = _load_owned_application(session, user, application_id)
    if application.status not in (
        TeacherApplicationStatus.DRAFT.value,
        TeacherApplicationStatus.NEEDS_INFORMATION.value,
    ):
        raise LearningConflictError(
            f"application is {application.status!r}; only a draft or "
            "needs-information application may change its contexts"
        )

    resolved_ids: list[uuid.UUID] = []
    for item in payload.contexts:
        context = learning_context_service.resolve_context(session, item)
        if context.id in resolved_ids:
            raise LearningConflictError(
                "the same learning context appears more than once"
            )
        resolved_ids.append(context.id)

    for row in list(application.contexts):
        session.delete(row)
    session.flush()

    for context_id in resolved_ids:
        session.add(
            TeacherApplicationContext(
                application_id=application.id,
                learning_context_id=context_id,
                status=ApplicationContextStatus.REQUESTED.value,
            )
        )
    session.flush()

    _log(
        session,
        user_id=user.id,
        event_type="application_scope_changed",
        payload={
            "application_id": str(application.id),
            "learning_context_ids": [str(i) for i in resolved_ids],
        },
    )
    session.flush()
    return _read_application(_load_application_detail(session, application.id))


def add_evidence(
    session: Session,
    user: User,
    application_id: uuid.UUID,
    payload: EvidenceUpload,
    *,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    storage: StorageBackend | None = None,
) -> EvidenceRead:
    """Validate, privately store and attach one evidence document."""
    application = _load_owned_application(session, user, application_id)
    if application.status not in (
        TeacherApplicationStatus.DRAFT.value,
        TeacherApplicationStatus.NEEDS_INFORMATION.value,
    ):
        raise LearningConflictError(
            f"application is {application.status!r}; only a draft or "
            "needs-information application may attach evidence"
        )

    requested_ids = {c.learning_context_id for c in application.contexts}
    if not requested_ids:
        raise LearningValidationError(
            "request at least one learning context before attaching evidence"
        )
    unknown = [i for i in payload.learning_context_ids if i not in requested_ids]
    if unknown:
        raise LearningValidationError(
            "every evidence link must name one of the application's "
            "requested learning contexts"
        )
    if len(set(payload.learning_context_ids)) != len(payload.learning_context_ids):
        raise LearningValidationError(
            "the same learning context appears more than once"
        )

    try:
        evidence_validation.validate_evidence_upload(
            data=file_bytes,
            content_type=content_type,
            original_filename=original_filename,
        )
    except evidence_validation.ValidationError as exc:
        raise LearningValidationError(str(exc)) from exc

    normalized_type = evidence_validation.normalize_content_type(content_type)
    checksum = hashlib.sha256(file_bytes).hexdigest()

    duplicate = False
    # The new evidence row does not exist yet, so ANY row sharing this
    # checksum or credential reference marks a duplicate submission attempt.
    if session.scalar(
        select(TeacherEvidence.id)
        .join(FileAsset, FileAsset.id == TeacherEvidence.file_asset_id)
        .where(FileAsset.checksum_sha256 == checksum)
        .limit(1)
    ):
        duplicate = True
    if payload.credential_reference:
        if session.scalar(
            select(TeacherEvidence.id)
            .where(
                TeacherEvidence.credential_reference
                == payload.credential_reference
            )
            .limit(1)
        ):
            duplicate = True

    backend = storage or get_storage_backend()
    asset = file_asset_repo.create(
        session,
        uploaded_by_user_id=user.id,
        original_filename=original_filename,
        content_type=normalized_type,
        size_bytes=len(file_bytes),
        checksum_sha256=checksum,
        storage_key="pending",
        validation_status=FileAssetStatus.VALIDATING.value,
    )
    storage_key = f"file_assets/{asset.id}"
    asset.storage_key = storage_key
    session.flush()

    try:
        backend.save(storage_key, file_bytes, content_type=normalized_type)
    except Exception as exc:  # storage backend failure
        asset.validation_status = FileAssetStatus.INVALID.value
        asset.validation_error = "storage backend rejected the upload"
        session.flush()
        raise LearningValidationError(
            "the file could not be stored; try again"
        ) from exc

    asset.validation_status = FileAssetStatus.STORED.value
    session.flush()

    evidence = TeacherEvidence(
        application_id=application.id,
        file_asset_id=asset.id,
        evidence_type=payload.evidence_type.value,
        status=TeacherEvidenceStatus.SUBMITTED.value,
        title=payload.title,
        issuing_organization=payload.issuing_organization,
        country=payload.country,
        credential_reference=payload.credential_reference,
        duplicate_indicator=duplicate,
        created_by=user.id,
    )
    session.add(evidence)
    session.flush()

    for context_id in payload.learning_context_ids:
        session.add(
            TeacherEvidenceContext(
                evidence_id=evidence.id,
                learning_context_id=context_id,
            )
        )
    session.flush()

    _log(
        session,
        user_id=user.id,
        event_type="evidence_uploaded",
        payload={
            "application_id": str(application.id),
            "evidence_id": str(evidence.id),
            "evidence_type": evidence.evidence_type,
            "duplicate_indicator": duplicate,
        },
    )
    session.flush()

    stored = session.scalar(
        select(TeacherEvidence)
        .options(*_EVIDENCE_EAGER)
        .where(TeacherEvidence.id == evidence.id)
        .execution_options(populate_existing=True)
    )
    return _read_evidence(stored)


def remove_evidence(
    session: Session, user: User, application_id: uuid.UUID, evidence_id: uuid.UUID
) -> None:
    """Soft-delete one evidence document (history stays auditable)."""
    application = _load_owned_application(session, user, application_id)
    if application.status not in (
        TeacherApplicationStatus.DRAFT.value,
        TeacherApplicationStatus.NEEDS_INFORMATION.value,
    ):
        raise LearningConflictError(
            f"application is {application.status!r}; only a draft or "
            "needs-information application may remove evidence"
        )
    evidence = session.get(TeacherEvidence, evidence_id)
    if evidence is None or evidence.application_id != application.id:
        raise LearningNotFoundError("no evidence with this id on this application")
    if evidence.status == TeacherEvidenceStatus.REMOVED.value:
        raise LearningConflictError("this evidence is already removed")

    evidence.status = TeacherEvidenceStatus.REMOVED.value
    session.flush()
    _log(
        session,
        user_id=user.id,
        event_type="evidence_removed",
        payload={
            "application_id": str(application.id),
            "evidence_id": str(evidence.id),
        },
    )
    session.flush()


def _submit_blockers(
    session: Session, application: TeacherApplication
) -> list[str]:
    """Deterministic completeness rules the applicant must satisfy."""
    blockers: list[str] = []
    if application.category is None:
        return ["claim a verification category before submitting"]

    if not application.contexts:
        blockers.append("request at least one learning context")

    evidence = _live_evidence(application)
    if not evidence:
        blockers.append("attach at least one evidence document")

    types = {e.evidence_type for e in evidence}
    if TeacherEvidenceType.IDENTITY_DOCUMENT.value not in types:
        blockers.append("attach identity evidence")

    if application.category == TeacherVerificationCategory.FORMAL_TEACHER.value:
        if TeacherEvidenceType.ACADEMIC_QUALIFICATION.value not in types:
            blockers.append("attach academic qualification evidence")
        if not (
            {TeacherEvidenceType.TEACHER_TRAINING.value,
             TeacherEvidenceType.PROFESSIONAL_REGISTRATION.value}
            & types
        ):
            blockers.append(
                "attach teacher-training or professional-registration evidence"
            )
        if (
            application.claimed_school_id is not None
            and TeacherEvidenceType.EMPLOYMENT_CONFIRMATION.value not in types
        ):
            blockers.append(
                "attach employment confirmation for the claimed school"
            )
    elif application.category == TeacherVerificationCategory.INDEPENDENT_TUTOR.value:
        if not application.misrepresentation_declaration:
            blockers.append("sign the misrepresentation declaration")

    # Every requested context needs linked covering evidence.
    for row in application.contexts:
        linked = _evidence_types_for_context(
            session, application.id, row.learning_context_id
        )
        if _is_tvet_context(session, row.learning_context_id):
            if not (_TVET_EVIDENCE_TYPES & linked):
                blockers.append(
                    "a requested vocational (TVET) context needs linked "
                    "vocational-qualification or professional-experience evidence"
                )
        elif application.category == (
            TeacherVerificationCategory.INDEPENDENT_TUTOR.value
        ):
            if not (_COVERING_EVIDENCE_TYPES & linked):
                blockers.append(
                    "every requested context needs linked academic/vocational "
                    "qualification or professional-experience evidence"
                )
    return blockers


def submit_application(
    session: Session, user: User, application_id: uuid.UUID
) -> ApplicationRead:
    """Move draft/needs_information → submitted after completeness checks."""
    application = _load_owned_application(session, user, application_id)
    _require_transition(application, TeacherApplicationStatus.SUBMITTED.value)

    blockers = _submit_blockers(session, application)
    if blockers:
        raise LearningValidationError(
            "the application is not ready to submit: " + "; ".join(blockers)
        )

    application.status = TeacherApplicationStatus.SUBMITTED.value
    application.submitted_at = _now()
    application.decision_reason = None
    session.flush()

    _log(
        session,
        user_id=user.id,
        event_type="application_submitted",
        payload={"application_id": str(application.id)},
    )
    session.flush()
    return _read_application(_load_application_detail(session, application.id))


def withdraw_application(
    session: Session, user: User, application_id: uuid.UUID
) -> ApplicationRead:
    """Applicant-initiated withdrawal from any non-terminal state."""
    application = _load_owned_application(session, user, application_id)
    _require_transition(application, TeacherApplicationStatus.WITHDRAWN.value)

    application.status = TeacherApplicationStatus.WITHDRAWN.value
    session.flush()
    _log(
        session,
        user_id=user.id,
        event_type="application_withdrawn",
        payload={"application_id": str(application.id)},
    )
    session.flush()
    return _read_application(_load_application_detail(session, application.id))


def get_my_scope(session: Session, user: User) -> list[ScopeContextRead]:
    """The caller's canonical approved teaching scope."""
    profile = _load_teacher_profile(session, user)
    rows = session.scalars(
        select(TeacherApprovedContext)
        .where(TeacherApprovedContext.teacher_id == profile.id)
        .order_by(TeacherApprovedContext.approved_at, TeacherApprovedContext.id)
    )
    return [
        ScopeContextRead(
            learning_context_id=r.learning_context_id,
            application_id=r.application_id,
            approved_at=r.approved_at,
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# administrative review
# --------------------------------------------------------------------------


def list_applications(
    session: Session,
    *,
    status: TeacherApplicationStatus | None = None,
    teacher_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[ApplicationSummaryRead]:
    """The review queue, newest first (optional status/teacher filter)."""
    stmt = (
        select(TeacherApplication)
        .options(joinedload(TeacherApplication.teacher))
    )
    if status is not None:
        stmt = stmt.where(TeacherApplication.status == status.value)
    if teacher_id is not None:
        stmt = stmt.where(TeacherApplication.teacher_id == teacher_id)
    stmt = stmt.order_by(
        TeacherApplication.created_at.desc(), TeacherApplication.id
    ).limit(limit).offset(offset)
    rows = session.execute(stmt).unique().scalars().all()
    return [
        ApplicationSummaryRead(
            id=a.id,
            teacher_id=a.teacher_id,
            teacher_name=a.teacher.full_name,
            status=a.status,
            category=a.category,
            submitted_at=a.submitted_at,
            created_at=a.created_at,
        )
        for a in rows
    ]


def get_application_detail(
    session: Session, application_id: uuid.UUID
) -> ApplicationAdminRead:
    """Full administrative detail including the review trail."""
    return _read_admin(_load_application_detail(session, application_id))


def download_evidence_file(
    session: Session,
    application_id: uuid.UUID,
    evidence_id: uuid.UUID,
    *,
    storage: StorageBackend | None = None,
) -> tuple[bytes, str, str]:
    """Authorized private read of one evidence file's bytes."""
    application = _load_application_detail(session, application_id)
    evidence = next(
        (e for e in application.evidence if e.id == evidence_id), None
    )
    if evidence is None:
        raise LearningNotFoundError("no evidence with this id on this application")
    asset = session.get(FileAsset, evidence.file_asset_id)
    if asset is None:
        raise LearningNotFoundError("the evidence file asset is missing")
    backend = storage or get_storage_backend()
    try:
        data = backend.read(asset.storage_key)
    except Exception as exc:
        raise LearningNotFoundError("the evidence file is unavailable") from exc
    return data, asset.content_type, asset.original_filename


def record_check(
    session: Session,
    admin_user: User,
    application_id: uuid.UUID,
    payload: VerificationCheckRecord,
) -> VerificationCheckRead:
    """Record one reviewer check; ``seq`` is monotonic per evidence."""
    application = _load_application_detail(session, application_id)
    if application.status not in (
        TeacherApplicationStatus.SUBMITTED.value,
        TeacherApplicationStatus.NEEDS_INFORMATION.value,
    ):
        raise LearningConflictError(
            f"application is {application.status!r}; checks may only be "
            "recorded while it is under review"
        )
    evidence = next(
        (e for e in application.evidence if e.id == payload.evidence_id), None
    )
    if evidence is None:
        raise LearningNotFoundError("no evidence with this id on this application")
    if evidence.status == TeacherEvidenceStatus.REMOVED.value:
        raise LearningConflictError("this evidence has been removed")

    max_seq = session.scalar(
        select(func.max(TeacherVerificationCheck.seq)).where(
            TeacherVerificationCheck.evidence_id == evidence.id
        )
    )
    check = TeacherVerificationCheck(
        application_id=application.id,
        evidence_id=evidence.id,
        reviewer_user_id=admin_user.id,
        seq=(max_seq or 0) + 1,
        method=payload.method.value,
        outcome=payload.outcome.value,
        source_institution=payload.source_institution,
        notes=payload.notes,
    )
    session.add(check)
    session.flush()

    _log(
        session,
        user_id=_teacher_subject_id(session, application.teacher_id),
        event_type="verification_check_recorded",
        actor_user_id=admin_user.id,
        payload={
            "application_id": str(application.id),
            "evidence_id": str(evidence.id),
            "seq": check.seq,
            "outcome": check.outcome,
            "method": check.method,
        },
    )
    session.flush()
    return _read_check(check)


def record_assessment(
    session: Session,
    admin_user: User,
    application_id: uuid.UUID,
    payload: AssessmentRecord,
) -> AssessmentRead:
    """Record the outcome of a subject-specific assessment (experience route)."""
    application = _load_application_detail(session, application_id)
    if application.status not in (
        TeacherApplicationStatus.SUBMITTED.value,
        TeacherApplicationStatus.NEEDS_INFORMATION.value,
    ):
        raise LearningConflictError(
            f"application is {application.status!r}; assessments may only be "
            "recorded while it is under review"
        )

    assessment = TeacherAssessment(
        application_id=application.id,
        subject_id=payload.subject_id,
        assessment_date=payload.assessment_date,
        assessor=payload.assessor,
        outcome=payload.outcome.value,
        notes=payload.notes,
        recorded_by=admin_user.id,
    )
    session.add(assessment)
    session.flush()

    _log(
        session,
        user_id=_teacher_subject_id(session, application.teacher_id),
        event_type="assessment_recorded",
        actor_user_id=admin_user.id,
        payload={
            "application_id": str(application.id),
            "subject_id": str(payload.subject_id),
            "outcome": assessment.outcome,
        },
    )
    session.flush()
    return _read_assessment(assessment)


def request_information(
    session: Session,
    admin_user: User,
    application_id: uuid.UUID,
    payload: InformationRequest,
) -> ApplicationRead:
    """submitted → needs_information with the reviewer's message."""
    application = _load_application_detail(session, application_id)
    _require_transition(
        application, TeacherApplicationStatus.NEEDS_INFORMATION.value
    )

    application.status = TeacherApplicationStatus.NEEDS_INFORMATION.value
    application.decision_reason = payload.message
    session.flush()

    _log(
        session,
        user_id=_teacher_subject_id(session, application.teacher_id),
        event_type="information_requested",
        actor_user_id=admin_user.id,
        payload={
            "application_id": str(application.id),
            "message": payload.message,
        },
    )
    session.flush()
    return _read_application(application)


def _approval_blockers(
    session: Session,
    application: TeacherApplication,
    approved_rows: list[TeacherApplicationContext],
) -> list[str]:
    """Reviewer-side rules: verified evidence + per-context coverage."""
    blockers: list[str] = []

    for e in _live_evidence(application):
        outcome = _latest_check_outcome(session, e.id)
        if outcome != VerificationCheckOutcome.VERIFIED.value:
            blockers.append(
                f"evidence {e.title!r} has no latest check with outcome "
                "'verified'"
            )

    if not approved_rows:
        blockers.append("approve at least one learning context")

    for row in approved_rows:
        linked = _evidence_types_for_context(
            session, application.id, row.learning_context_id
        )
        if _is_tvet_context(session, row.learning_context_id):
            if not (_TVET_EVIDENCE_TYPES & linked):
                blockers.append(
                    "a vocational (TVET) context needs verified "
                    "vocational-qualification or professional-experience evidence"
                )
        if application.category == (
            TeacherVerificationCategory.INDEPENDENT_TUTOR.value
        ):
            if not (
                {
                    TeacherEvidenceType.ACADEMIC_QUALIFICATION.value,
                    TeacherEvidenceType.VOCATIONAL_QUALIFICATION.value,
                }
                & linked
            ):
                # Experience route: professional experience + a PASSED
                # subject-specific assessment for this context's subject.
                subject_id = _context_subject_id(
                    session, row.learning_context_id
                )
                if (
                    TeacherEvidenceType.PROFESSIONAL_EXPERIENCE.value not in linked
                    or subject_id is None
                    or not _has_passed_assessment(
                        session, application.id, subject_id
                    )
                ):
                    blockers.append(
                        "an independent-tutor context needs verified "
                        "academic/vocational qualification evidence, or "
                        "professional experience plus a passed subject assessment"
                    )
    return blockers


def decide_application(
    session: Session,
    admin_user: User,
    application_id: uuid.UUID,
    payload: ApplicationDecision,
) -> ApplicationAdminRead:
    """Approve (with explicit per-context outcomes) or reject."""
    application = _load_application_detail(session, application_id)
    if application.status != TeacherApplicationStatus.SUBMITTED.value:
        raise LearningConflictError(
            f"application is {application.status!r}; only a submitted "
            "application may be decided"
        )

    if not payload.approve:
        if not payload.reason or not payload.reason.strip():
            raise LearningValidationError("a rejection reason is required")
        application.status = TeacherApplicationStatus.REJECTED.value
        application.decision_reason = payload.reason
        application.decided_at = _now()
        # First-cycle rejection is reflected on the profile; a rejected
        # scope-extension never revokes an existing approval.
        profile = session.get(Teacher, application.teacher_id)
        if (
            profile is not None
            and profile.verification_status
            != TeacherVerificationStatus.APPROVED.value
        ):
            profile.verification_status = TeacherVerificationStatus.REJECTED.value
        session.flush()
        _log(
            session,
            user_id=_teacher_subject_id(session, application.teacher_id),
            event_type="application_rejected",
            actor_user_id=admin_user.id,
            payload={
                "application_id": str(application.id),
                "reason": payload.reason,
            },
        )
        session.flush()
        return _read_admin(application)

    # --- approval path ---------------------------------------------------
    if not payload.contexts:
        raise LearningValidationError(
            "an approval must supply an explicit outcome for every "
            "requested learning context"
        )
    requested_ids = [c.learning_context_id for c in application.contexts]
    supplied_ids = [c.learning_context_id for c in payload.contexts]
    if sorted(map(str, supplied_ids)) != sorted(map(str, requested_ids)):
        raise LearningValidationError(
            "the decision must cover every requested learning context "
            "exactly once"
        )

    rows_by_id = {c.learning_context_id: c for c in application.contexts}
    approved_rows: list[TeacherApplicationContext] = []
    for decision in payload.contexts:
        row = rows_by_id[decision.learning_context_id]
        if decision.status not in (
            ApplicationContextStatus.APPROVED.value,
            ApplicationContextStatus.REJECTED.value,
        ):
            raise LearningValidationError(
                "each context outcome must be 'approved' or 'rejected'"
            )
        if (
            decision.status == ApplicationContextStatus.REJECTED.value
            and not (decision.reason and decision.reason.strip())
        ):
            raise LearningValidationError(
                "a rejected context requires a reason"
            )
        row.status = decision.status
        row.decision_reason = decision.reason
        row.decided_by = admin_user.id
        row.decided_at = _now()
        if decision.status == ApplicationContextStatus.APPROVED.value:
            approved_rows.append(row)

    blockers = _approval_blockers(session, application, approved_rows)
    if blockers:
        raise LearningValidationError(
            "the application cannot be approved yet: " + "; ".join(blockers)
        )

    application.status = TeacherApplicationStatus.APPROVED.value
    application.decided_at = _now()
    application.decision_reason = None

    profile = session.get(Teacher, application.teacher_id)
    if profile is None:
        raise LearningNotFoundError("no teacher profile for this application")
    profile.verification_status = TeacherVerificationStatus.APPROVED.value
    profile.verification_category = application.category

    for row in approved_rows:
        session.add(
            TeacherApprovedContext(
                teacher_id=application.teacher_id,
                learning_context_id=row.learning_context_id,
                application_id=application.id,
                approved_by=admin_user.id,
                approved_at=_now(),
            )
        )
    session.flush()

    _log(
        session,
        user_id=_teacher_subject_id(session, application.teacher_id),
        event_type="application_approved",
        actor_user_id=admin_user.id,
        payload={
            "application_id": str(application.id),
            "category": application.category,
            "approved_context_ids": [
                str(r.learning_context_id) for r in approved_rows
            ],
        },
    )
    for row in approved_rows:
        _log(
            session,
            user_id=_teacher_subject_id(session, application.teacher_id),
            event_type="context_approved",
            actor_user_id=admin_user.id,
            payload={
                "application_id": str(application.id),
                "learning_context_id": str(row.learning_context_id),
            },
        )
    for row in application.contexts:
        if row.status == ApplicationContextStatus.REJECTED.value:
            _log(
                session,
                user_id=_teacher_subject_id(session, application.teacher_id),
                event_type="context_rejected",
                actor_user_id=admin_user.id,
                payload={
                    "application_id": str(application.id),
                    "learning_context_id": str(row.learning_context_id),
                    "reason": row.decision_reason,
                },
            )
    session.flush()
    logger.info(
        "teacher application approved",
        extra={
            "application_id": str(application.id),
            "teacher_id": str(application.teacher_id),
        },
    )
    return _read_admin(application)
