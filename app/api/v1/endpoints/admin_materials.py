"""Administrative material moderation API — ``/admin/materials`` (role-split).

Every route requires an administrator (``require_admin``); no handler here
serves a student or teacher token — cross-role calls are refused with 403
before any logic runs, and anonymous callers get 401.

- ``GET  /admin/materials`` — the moderation queue (default
  ``status=pending_review``) or a filtered archive listing.
- ``GET  /admin/materials/{material_id}`` — one material with its full
  moderation trail (reviewer, decision, reason, timestamp).
- ``POST /admin/materials/{material_id}/approve`` — pending_review →
  published.
- ``POST /admin/materials/{material_id}/reject`` — pending_review →
  rejected; the reason is mandatory and recorded on the moderation row.
- ``POST /admin/materials/{material_id}/archive`` — published → archived.

Publication is administrator-only: a teacher cannot publish their own
material from ``/me/teacher`` (slice 2B). Audit: each mutation writes an
``auth_events`` row naming the acting administrator (``actor_user_id``)
and the subject teacher; the ``material_moderations`` row is the durable
decision record. Error contract: unknown ids → 404, invalid transitions →
409, validation → 422 (shared ``LearningError`` family).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_ADMIN
from app.core.auth_dependencies import require_admin
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.enums import MaterialStatus
from app.models.user import User
from app.schemas.material import (
    MaterialDetailRead,
    MaterialRead,
    MaterialRejectRequest,
)
from app.services import material_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/admin", tags=[TAG_ADMIN])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get(
    "/materials",
    response_model=list[MaterialRead],
    summary="List materials (moderation queue or archive)",
    description=(
        "Administrator-only. Defaults to the pending_review queue; pass "
        "``status`` and/or ``offering_id`` to filter. Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def admin_list_materials(
    request: Request,
    status_filter: MaterialStatus | None = Query(
        default=None,
        alias="status",
        description="Filter by moderation status (defaults to all).",
    ),
    offering_id: uuid.UUID | None = Query(default=None),
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[MaterialRead]:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return material_service.admin_list_materials(
            session,
            admin,
            status=status_filter,
            offering_id=offering_id,
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/materials/{material_id}",
    response_model=MaterialDetailRead,
    summary="Retrieve one material with its moderation trail",
    description=(
        "Full summary plus every recorded approve/reject decision "
        "(reviewer, decision, reason, timestamp). Unknown id → 404. "
        "Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown material id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def admin_read_material(
    request: Request,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MaterialDetailRead:
    try:
        return material_service.admin_get_material(session, admin, material_id)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/materials/{material_id}/approve",
    response_model=MaterialRead,
    summary="Approve a pending material (publish it)",
    description=(
        "pending_review → published. Only pending_review materials can be "
        "approved (409 otherwise). The decision is recorded on "
        "material_moderations and in the auth_events audit trail. "
        "Administrators only."
    ),
    responses={
        200: {"description": "Material published"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown material id"},
        409: {"description": "Material is not pending_review"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_approve_material(
    request: Request,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MaterialRead:
    try:
        return material_service.admin_approve_material(session, admin, material_id)
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.post(
    "/materials/{material_id}/reject",
    response_model=MaterialRead,
    summary="Reject a pending material (with a mandatory reason)",
    description=(
        "pending_review → rejected. The reason is mandatory (min 1 "
        "character) and stored on the moderation row; the teacher may "
        "revise and resubmit. Only pending_review materials can be "
        "rejected (409 otherwise). Administrators only."
    ),
    responses={
        200: {"description": "Material rejected"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown material id"},
        409: {"description": "Material is not pending_review"},
        422: {"description": "Invalid request (missing/blank reason, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_reject_material(
    request: Request,
    material_id: uuid.UUID,
    payload: MaterialRejectRequest,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MaterialRead:
    try:
        return material_service.admin_reject_material(
            session, admin, material_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.post(
    "/materials/{material_id}/archive",
    response_model=MaterialRead,
    summary="Archive a published material",
    description=(
        "published → archived (administrator path). Only published "
        "materials can be archived (409 otherwise). Administrators only."
    ),
    responses={
        200: {"description": "Material archived"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown material id"},
        409: {"description": "Material is not published"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_archive_material(
    request: Request,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MaterialRead:
    try:
        return material_service.admin_archive_material(session, admin, material_id)
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
