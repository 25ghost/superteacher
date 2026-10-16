"""Teacher verification application API (policy workflow endpoints).

Role-split (no endpoint serves two roles):

Applicant self-service under ``/me/teacher`` (``require_teacher``):

- ``GET    /me/teacher/application`` — your active (else latest) application.
- ``POST   /me/teacher/application`` — open a draft (409 when one is open).
- ``PATCH  /me/teacher/application/{id}`` — edit the claimed profile while
  the application is draft/needs-information.
- ``PUT    /me/teacher/application/{id}/contexts`` — replace the requested
  canonical learning-context set.
- ``POST   /me/teacher/application/{id}/evidence`` — attach one evidence
  document (multipart: PDF/JPEG/PNG, private storage only).
- ``DELETE /me/teacher/application/{id}/evidence/{evidence_id}`` —
  soft-remove one document while editable.
- ``POST   /me/teacher/application/{id}/submit`` — completeness-checked
  submit.
- ``POST   /me/teacher/application/{id}/withdraw`` — applicant withdrawal.
- ``GET    /me/teacher/verification/scope`` — your canonical approved
  teaching scope.

Administrative review under ``/admin`` (``require_admin``):

- ``GET  /admin/teacher-applications`` — the review queue.
- ``GET  /admin/teacher-applications/{id}`` — full detail incl. checks.
- ``GET  /admin/teacher-applications/{id}/evidence/{evidence_id}/file`` —
  private evidence download (never a public URL).
- ``POST /admin/teacher-applications/{id}/checks`` — record one check.
- ``POST /admin/teacher-applications/{id}/assessments`` — record one
  subject-assessment outcome (experience route).
- ``POST /admin/teacher-applications/{id}/request-information`` —
  submitted → needs_information.
- ``POST /admin/teacher-applications/{id}/decide`` — approve with explicit
  per-context outcomes, or reject with a reason.

Endpoint bodies stay thin: validate schema → delegate to the service →
map the ``LearningError`` family onto HTTP → commit on success. No AI is
involved anywhere in this workflow.
"""
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_ADMIN, TAG_TEACHER
from app.core.auth_dependencies import require_admin, require_teacher
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.enums import TeacherApplicationStatus, TeacherEvidenceType
from app.models.user import User
from app.schemas.teacher_verification import (
    ApplicationAdminRead,
    ApplicationDecision,
    ApplicationRead,
    ApplicationStart,
    ApplicationSummaryRead,
    ApplicationUpdate,
    AssessmentRead,
    AssessmentRecord,
    ContextsReplace,
    EvidenceRead,
    InformationRequest,
    ScopeContextRead,
    VerificationCheckRead,
    VerificationCheckRecord,
)
from app.services import teacher_verification_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

teacher_router = APIRouter(prefix="/me/teacher", tags=[TAG_TEACHER])
admin_router = APIRouter(prefix="/admin", tags=[TAG_ADMIN])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


# --------------------------------------------------------------------------
# applicant self-service
# --------------------------------------------------------------------------


@teacher_router.get(
    "/application",
    response_model=ApplicationRead,
    summary="Your teacher verification application",
    description=(
        "The calling teacher's active (else most recent) verification "
        "application with its requested contexts and attached evidence. "
        "404 when the account has never applied. Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
        404: {"description": "No application on this account"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_ME_READ)
def get_my_application(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> ApplicationRead:
    try:
        application = teacher_verification_service.get_my_application(
            session, user
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    if application is None:
        raise HTTPException(status_code=404, detail="no teacher application on this account")
    return application


@teacher_router.post(
    "/application",
    response_model=ApplicationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start a teacher verification application",
    description=(
        "Opens a draft application claiming a verification category "
        "(formal_teacher or independent_tutor). A re-application after a "
        "terminal decision creates a NEW row preserving the chain. An "
        "already-open application → 409. Unknown school id → 404. "
        "Starting an application grants no teaching access."
    ),
    responses={
        201: {"description": "Draft application created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
        404: {"description": "Unknown claimed school id"},
        409: {"description": "An application is already open"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def start_my_application(
    request: Request,
    payload: ApplicationStart,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> ApplicationRead:
    try:
        created = teacher_verification_service.start_application(
            session, user, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return created


@teacher_router.patch(
    "/application/{application_id}",
    response_model=ApplicationRead,
    summary="Edit your application's claimed profile",
    description=(
        "Amend category, claimed school, statement or the independent-"
        "tutor misrepresentation declaration while the application is "
        "draft or needs-information. A category change is audited. "
        "Any other state → 409; a foreign/unknown id → 404. Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
        404: {"description": "Unknown or foreign application id"},
        409: {"description": "Application is not editable"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def patch_my_application(
    request: Request,
    application_id: uuid.UUID,
    payload: ApplicationUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> ApplicationRead:
    try:
        updated = teacher_verification_service.update_application(
            session, user, application_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated


@teacher_router.put(
    "/application/{application_id}/contexts",
    response_model=ApplicationRead,
    summary="Replace your application's requested contexts",
    description=(
        "Sets the full requested learning-context set (canonical tuples "
        "resolved server-side; duplicates → 409). Allowed only while the "
        "application is draft or needs-information. Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
        404: {"description": "Unknown or foreign application id, or unknown catalog id"},
        409: {"description": "Application is not editable, or a context is duplicated"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def put_my_application_contexts(
    request: Request,
    application_id: uuid.UUID,
    payload: ContextsReplace,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> ApplicationRead:
    try:
        updated = teacher_verification_service.replace_contexts(
            session, user, application_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated


@teacher_router.post(
    "/application/{application_id}/evidence",
    response_model=EvidenceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Attach one evidence document",
    description=(
        "Multipart upload (PDF/JPEG/PNG, magic-byte checked, size-capped) "
        "stored privately — evidence never has a public URL. The document "
        "is linked to the requested contexts it substantiates. Draft/"
        "needs-information only; a mismatched link or disallowed file → "
        "422. Teachers only."
    ),
    responses={
        201: {"description": "Evidence stored and linked"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
        404: {"description": "Unknown or foreign application id"},
        409: {"description": "Application is not editable"},
        422: {"description": "Invalid upload or metadata"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
async def upload_my_evidence(
    request: Request,
    application_id: uuid.UUID,
    evidence_type: TeacherEvidenceType = Form(...),
    title: str = Form(..., min_length=1, max_length=200),
    issuing_organization: str | None = Form(default=None, max_length=200),
    country: str | None = Form(default=None, max_length=80),
    credential_reference: str | None = Form(default=None, max_length=200),
    learning_context_ids: list[uuid.UUID] = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> EvidenceRead:
    from app.schemas.teacher_verification import EvidenceUpload

    file_bytes = await file.read()
    payload = EvidenceUpload(
        evidence_type=evidence_type,
        title=title,
        issuing_organization=issuing_organization,
        country=country,
        credential_reference=credential_reference,
        learning_context_ids=learning_context_ids,
    )
    try:
        created = teacher_verification_service.add_evidence(
            session,
            user,
            application_id,
            payload,
            file_bytes=file_bytes,
            original_filename=file.filename or "",
            content_type=file.content_type or "",
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return created


@teacher_router.delete(
    "/application/{application_id}/evidence/{evidence_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove one evidence document",
    description=(
        "Soft-removes one attached document (history stays auditable) "
        "while the application is draft or needs-information. Unknown or "
        "foreign ids → 404; already removed → 409. Teachers only."
    ),
    responses={
        204: {"description": "Evidence removed"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
        404: {"description": "Unknown or foreign application/evidence id"},
        409: {"description": "Application is not editable, or evidence already removed"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def delete_my_evidence(
    request: Request,
    application_id: uuid.UUID,
    evidence_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> Response:
    try:
        teacher_verification_service.remove_evidence(
            session, user, application_id, evidence_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@teacher_router.post(
    "/application/{application_id}/submit",
    response_model=ApplicationRead,
    summary="Submit your application for review",
    description=(
        "Completeness-checked move draft/needs_information → submitted "
        "(category claimed, contexts requested, required evidence attached "
        "per category). Rule violations → 422 listing every blocker. "
        "Submitting grants no teaching access. Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
        404: {"description": "Unknown or foreign application id"},
        409: {"description": "Application is not submittable from its current state"},
        422: {"description": "The application is not ready to submit"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def submit_my_application(
    request: Request,
    application_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> ApplicationRead:
    try:
        updated = teacher_verification_service.submit_application(
            session, user, application_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated


@teacher_router.post(
    "/application/{application_id}/withdraw",
    response_model=ApplicationRead,
    summary="Withdraw your application",
    description=(
        "Applicant-initiated withdrawal from any non-terminal state. "
        "A decided application cannot be withdrawn (409). Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
        404: {"description": "Unknown or foreign application id"},
        409: {"description": "Application is already terminal"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def withdraw_my_application(
    request: Request,
    application_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> ApplicationRead:
    try:
        updated = teacher_verification_service.withdraw_application(
            session, user, application_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated


@teacher_router.get(
    "/verification/scope",
    response_model=list[ScopeContextRead],
    summary="Your approved teaching scope",
    description=(
        "The canonical approved learning contexts granted by completed "
        "application approvals. An empty list means no context may be "
        "taught, regardless of account state. Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_ME_READ)
def get_my_verification_scope(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[ScopeContextRead]:
    try:
        return teacher_verification_service.get_my_scope(session, user)
    except LearningError as exc:
        raise _http_error(exc) from exc


# --------------------------------------------------------------------------
# administrative review
# --------------------------------------------------------------------------


@admin_router.get(
    "/teacher-applications",
    response_model=list[ApplicationSummaryRead],
    summary="Review queue of teacher applications",
    description=(
        "Newest-first list, optionally filtered by status and/or teacher. "
        "Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not an admin account"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def list_teacher_applications(
    request: Request,
    application_status: TeacherApplicationStatus | None = None,
    teacher_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
    session: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> list[ApplicationSummaryRead]:
    try:
        return teacher_verification_service.list_applications(
            session,
            status=application_status,
            teacher_id=teacher_id,
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@admin_router.get(
    "/teacher-applications/{application_id}",
    response_model=ApplicationAdminRead,
    summary="One application with its full review trail",
    description=(
        "Requested contexts with decisions, attached evidence (no "
        "credential references), recorded verification checks and "
        "assessments. Unknown id → 404. Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not an admin account"},
        404: {"description": "Unknown application id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def get_teacher_application(
    request: Request,
    application_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> ApplicationAdminRead:
    try:
        return teacher_verification_service.get_application_detail(
            session, application_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@admin_router.get(
    "/teacher-applications/{application_id}/evidence/{evidence_id}/file",
    summary="Download one evidence file (private)",
    description=(
        "Authorized reviewer download of the stored evidence bytes. "
        "Evidence is never served from a public URL. Unknown ids → 404. "
        "Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not an admin account"},
        404: {"description": "Unknown application/evidence id, or file unavailable"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_READ)
def download_teacher_evidence_file(
    request: Request,
    application_id: uuid.UUID,
    evidence_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> Response:
    try:
        data, content_type, filename = (
            teacher_verification_service.download_evidence_file(
                session, application_id, evidence_id
            )
        )
    except LearningError as exc:
        raise _http_error(exc) from exc
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@admin_router.post(
    "/teacher-applications/{application_id}/checks",
    response_model=VerificationCheckRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record one verification check on one evidence document",
    description=(
        "Deterministic human review: method (registry lookup, issuer "
        "confirmation, documented manual contact, other), outcome "
        "(verified/failed/inconclusive), source institution and notes. "
        "The latest check per evidence (monotonic seq) drives approval "
        "accounting. Under-review applications only. Administrators only."
    ),
    responses={
        201: {"description": "Check recorded"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not an admin account"},
        404: {"description": "Unknown application or evidence id"},
        409: {"description": "Application is not under review, or evidence removed"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_WRITE)
def post_teacher_application_check(
    request: Request,
    application_id: uuid.UUID,
    payload: VerificationCheckRecord,
    session: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> VerificationCheckRead:
    try:
        created = teacher_verification_service.record_check(
            session, user, application_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return created


@admin_router.post(
    "/teacher-applications/{application_id}/assessments",
    response_model=AssessmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record one subject-assessment outcome",
    description=(
        "Records the outcome (passed/failed) of a subject-specific "
        "assessment performed under the reviewer's own standards — the "
        "platform never scores. Only a recorded pass satisfies the "
        "independent-tutor experience route. Under-review only. "
        "Administrators only."
    ),
    responses={
        201: {"description": "Assessment recorded"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not an admin account"},
        404: {"description": "Unknown application id"},
        409: {"description": "Application is not under review"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_WRITE)
def post_teacher_application_assessment(
    request: Request,
    application_id: uuid.UUID,
    payload: AssessmentRecord,
    session: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> AssessmentRead:
    try:
        created = teacher_verification_service.record_assessment(
            session, user, application_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return created


@admin_router.post(
    "/teacher-applications/{application_id}/request-information",
    response_model=ApplicationRead,
    summary="Request specific corrections from the applicant",
    description=(
        "submitted → needs_information with the reviewer's message stored "
        "as the decision reason; the applicant may edit and resubmit. "
        "Any other state → 409. Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not an admin account"},
        404: {"description": "Unknown application id"},
        409: {"description": "Application is not submitted"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_WRITE)
def post_teacher_application_information_request(
    request: Request,
    application_id: uuid.UUID,
    payload: InformationRequest,
    session: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> ApplicationRead:
    try:
        updated = teacher_verification_service.request_information(
            session, user, application_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated


@admin_router.post(
    "/teacher-applications/{application_id}/decide",
    response_model=ApplicationAdminRead,
    summary="Approve or reject an application",
    description=(
        "Approval requires an explicit outcome for EVERY requested "
        "context (reason required when rejected), every non-removed "
        "evidence's latest check to be 'verified', the category's "
        "evidence rules and per-context coverage; approved contexts "
        "become the teacher's canonical approved scope and the profile "
        "moves to approved with the application's category. Rejection "
        "requires a reason and never revokes an existing approval "
        "(scope-extension applications). Submitted only. "
        "Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not an admin account"},
        404: {"description": "Unknown application id"},
        409: {"description": "Application is not submitted"},
        422: {"description": "Decision incomplete or evidence not verified"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_REGISTRATION_WRITE)
def post_teacher_application_decision(
    request: Request,
    application_id: uuid.UUID,
    payload: ApplicationDecision,
    session: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> ApplicationAdminRead:
    try:
        decided = teacher_verification_service.decide_application(
            session, user, application_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return decided
