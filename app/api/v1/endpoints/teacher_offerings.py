"""Teacher self-service: teaching offerings (Phase 1).

Role-split (no endpoint serves two roles) — all under ``/me/teacher`` and
guarded by ``require_teacher``:

- ``POST /me/teacher/offerings`` — publish an offering: the context is
  validated against the catalog (and created the first time it is seen),
  then the offer is stored as ``active``.
- ``GET /me/teacher/offerings`` — every offering you own, newest first.
- ``GET /me/teacher/offerings/{offering_id}`` — one of yours; a foreign id
  answers the same 404 as an unknown one (L6 existence leak).
- ``PATCH /me/teacher/offerings/{offering_id}`` — amend the description
  and/or move active → paused → archived.

Publishing additionally requires an **approved** verification: an account
that may log in is not enough, so the vetting decision stays meaningful
after the invitation is accepted (403 otherwise).

Endpoint bodies stay thin: validate schema → delegate to the service →
map the Phase 1 error family onto HTTP → commit on success.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_TEACHER
from app.core.auth_dependencies import require_teacher
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.learning import (
    TeachingOfferingCreate,
    TeachingOfferingRead,
    TeachingOfferingUpdate,
)
from app.services import teaching_offering_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/me/teacher", tags=[TAG_TEACHER])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post(
    "/offerings",
    response_model=TeachingOfferingRead,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a teaching offering",
    description=(
        "Publishes one offering of a learning context (academic year, "
        "pathway, education level, optional program version, subject). The "
        "context is validated against the catalog — pathway spans the level, "
        "program version belongs to the tuple and is active, the subject is "
        "actually taught by it — and created the first time it is seen, so "
        "every teacher offering the same tuple shares one row. Requires an "
        "APPROVED teacher verification (403 otherwise). Already offering "
        "this context actively → 409. Unknown catalog id → 404; incoherent "
        "tuple → 422. Administrators approve teachers via "
        "PATCH /admin/teachers/{user_id}/verification."
    ),
    responses={
        201: {"description": "Offering created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Not a teacher account, or verification is not approved"},
        404: {"description": "Unknown academic year, pathway, level, program version or subject"},
        409: {"description": "An active offering for this context already exists"},
        422: {"description": "Incoherent context (pathway-level pair, program-version context, subject membership, archived year)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def create_my_offering(
    request: Request,
    payload: TeachingOfferingCreate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> TeachingOfferingRead:
    try:
        return teaching_offering_service.create_offering(session, user, payload)
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.get(
    "/offerings",
    response_model=list[TeachingOfferingRead],
    summary="List your teaching offerings",
    description=(
        "Every offering you own (all statuses), newest first, with its "
        "context flattened onto the row so no catalog request is needed. "
        "Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_my_offerings(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[TeachingOfferingRead]:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return teaching_offering_service.list_my_offerings(session, user)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/offerings/{offering_id}",
    response_model=TeachingOfferingRead,
    summary="Retrieve one of your teaching offerings",
    description=(
        "Full summary of one of YOUR offering ids (identity from the Bearer "
        "token). An offering that exists but belongs to another teacher "
        "returns the SAME 404 as an unknown id (L6 existence leak). "
        "Administrators and students read offerings through the marketplace."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering id, or not one of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_offering(
    request: Request,
    offering_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> TeachingOfferingRead:
    try:
        return teaching_offering_service.get_my_offering(session, user, offering_id)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.patch(
    "/offerings/{offering_id}",
    response_model=TeachingOfferingRead,
    summary="Amend one of your teaching offerings",
    description=(
        "Change the description and/or the status of one of your offerings. "
        "Status moves active → paused → archived (either live state may be "
        "archived); archived is terminal and any other pair is 409. A "
        "supplied status must differ from the current one (same status → "
        "409, never a silent 200). Supplying only description changes the "
        "text (explicit null clears it). Exactly one audit event is written: "
        "teaching_offering_status_changed when status is supplied, otherwise "
        "teaching_offering_updated. Unknown or foreign id → 404."
    ),
    responses={
        200: {"description": "Offering updated"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering id, or not one of yours"},
        409: {"description": "Same status, or the transition is not allowed"},
        422: {"description": "Invalid request (no fields, null status, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def update_my_offering(
    request: Request,
    offering_id: uuid.UUID,
    payload: TeachingOfferingUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> TeachingOfferingRead:
    try:
        updated = teaching_offering_service.update_my_offering(
            session, user, offering_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated
