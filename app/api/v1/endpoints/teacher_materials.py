"""Teacher self-service: teaching materials under an offering (Phase 2, 2B).

Nested resource semantics — every path starts from the offering the
teacher owns, then attaches materials to it (optionally to one of its
lessons):

- ``POST   /me/teacher/offerings/{offering_id}/materials`` — upload a
  file + create a draft material (multipart)
- ``GET    /me/teacher/offerings/{offering_id}/materials`` — list them
- ``GET    /me/teacher/offerings/{offering_id}/materials/{material_id}``
- ``PATCH  .../materials/{material_id}`` — amend draft/rejected metadata
- ``POST   .../materials/{material_id}/submit`` — draft → pending_review
- ``POST   .../materials/{material_id}/revise`` — rejected → draft
- ``POST   .../materials/{material_id}/archive`` — published → archived
- ``DELETE .../materials/{material_id}`` — draft/rejected only

Only the teacher who owns the offering may touch its materials; a
foreign offering/material id answers the same 404 as an unknown one
(L6 existence leak). Students and administrators are refused by the
route guard — this namespace is teacher-only, one role per endpoint.
Publication is *not* possible here: approving/rejecting is admin-only
(slice 2B), and students read published materials in slice 2C.

Endpoint bodies stay thin: validate schema → delegate to the service →
map the Phase 1 error family onto HTTP → commit on success.
"""
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_TEACHER
from app.core.auth_dependencies import require_teacher
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.enums import MaterialType
from app.models.user import User
from app.schemas.material import MaterialRead, MaterialUpdate
from app.services import material_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/me/teacher", tags=[TAG_TEACHER])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post(
    "/offerings/{offering_id}/materials",
    response_model=MaterialRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file and create a draft material",
    description=(
        "multipart/form-data: title, material_type (book|note|exercise), "
        "optional description, optional lesson_id, and one file. The file "
        "is validated (size, content type, magic bytes), written through "
        "the storage backend and recorded as a file asset; the material "
        "is created in ``draft`` — publication is administrator-only. "
        "Unknown or foreign offering id → 404; an unknown lesson id or a "
        "lesson outside this offering → 404; a disallowed file → 422. "
        "Teachers only."
    ),
    responses={
        201: {"description": "Material created (draft) with its file asset"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering/lesson id, or not one of yours"},
        422: {"description": "Invalid upload or metadata"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
async def create_my_material(
    request: Request,
    offering_id: uuid.UUID,
    title: str = Form(..., min_length=1, max_length=200),
    material_type: MaterialType = Form(...),
    description: str | None = Form(default=None, max_length=2000),
    lesson_id: uuid.UUID | None = Form(default=None),
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> MaterialRead:
    file_bytes = await file.read()
    try:
        created = material_service.create_material(
            session,
            user,
            offering_id,
            title=title,
            material_type=material_type,
            description=description,
            lesson_id=lesson_id,
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


@router.get(
    "/offerings/{offering_id}/materials",
    response_model=list[MaterialRead],
    summary="List your materials for one offering",
    description=(
        "Every material under one of YOUR offerings, newest first. "
        "Unknown or foreign offering id → 404. Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering id, or not one of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_my_materials(
    request: Request,
    offering_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[MaterialRead]:
    try:
        return material_service.list_materials(session, user, offering_id)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/offerings/{offering_id}/materials/{material_id}",
    response_model=MaterialRead,
    summary="Retrieve one of your materials",
    description=(
        "Full summary of one material under an offering you own. A "
        "material that exists but sits under another offering returns "
        "the SAME 404 as an unknown id (L6 existence leak). Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown material id, or not under an offering of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_material(
    request: Request,
    offering_id: uuid.UUID,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> MaterialRead:
    try:
        return material_service.get_material(
            session, user, offering_id, material_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.patch(
    "/offerings/{offering_id}/materials/{material_id}",
    response_model=MaterialRead,
    summary="Amend one of your draft or rejected materials",
    description=(
        "Change title, description, material_type and/or lesson_id. "
        "Draft and rejected materials only — published materials are "
        "effectively immutable (409). Unknown or foreign id → 404. "
        "Teachers only."
    ),
    responses={
        200: {"description": "Material updated"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown material id, or not under an offering of yours"},
        409: {"description": "Material is pending_review/published/archived"},
        422: {"description": "Invalid request (no fields, null title, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def update_my_material(
    request: Request,
    offering_id: uuid.UUID,
    material_id: uuid.UUID,
    payload: MaterialUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> MaterialRead:
    try:
        return material_service.update_material(
            session, user, offering_id, material_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.post(
    "/offerings/{offering_id}/materials/{material_id}/submit",
    response_model=MaterialRead,
    summary="Submit a draft material for administrator review",
    description=(
        "draft → pending_review. Only draft materials can be submitted "
        "(409 otherwise). Publication is never possible from this "
        "namespace — only an administrator may publish. Teachers only."
    ),
    responses={
        200: {"description": "Material moved to pending_review"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown material id, or not under an offering of yours"},
        409: {"description": "Material is not in draft"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def submit_my_material(
    request: Request,
    offering_id: uuid.UUID,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> MaterialRead:
    try:
        return material_service.submit_material(
            session, user, offering_id, material_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.post(
    "/offerings/{offering_id}/materials/{material_id}/revise",
    response_model=MaterialRead,
    summary="Revise a rejected material back to draft",
    description=(
        "rejected → draft: the revision flow before resubmission. Only "
        "rejected materials can be revised (409 otherwise). Teachers only."
    ),
    responses={
        200: {"description": "Material returned to draft"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown material id, or not under an offering of yours"},
        409: {"description": "Material is not rejected"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def revise_my_material(
    request: Request,
    offering_id: uuid.UUID,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> MaterialRead:
    try:
        return material_service.revise_material(
            session, user, offering_id, material_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.post(
    "/offerings/{offering_id}/materials/{material_id}/archive",
    response_model=MaterialRead,
    summary="Archive one of your published materials",
    description=(
        "published → archived: retire without deleting history. Only "
        "published materials can be archived (409 otherwise). Teachers only."
    ),
    responses={
        200: {"description": "Material archived"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown material id, or not under an offering of yours"},
        409: {"description": "Material is not published"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def archive_my_material(
    request: Request,
    offering_id: uuid.UUID,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> MaterialRead:
    try:
        return material_service.archive_material(
            session, user, offering_id, material_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.delete(
    "/offerings/{offering_id}/materials/{material_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of your draft or rejected materials",
    description=(
        "Removes a draft or rejected material (moderation rows cascade). "
        "Published materials are archived, never deleted. Unknown or "
        "foreign id → 404. Teachers only."
    ),
    responses={
        204: {"description": "Material removed"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown material id, or not under an offering of yours"},
        409: {"description": "Material is pending_review/published/archived"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def delete_my_material(
    request: Request,
    offering_id: uuid.UUID,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> None:
    try:
        material_service.delete_material(
            session, user, offering_id, material_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
