"""Student content access + basic material progress (Phase 2, slice 2C).

All under ``/me/learning-content/{enrollment_id}`` and guarded by
``get_current_student`` -- identity always comes from the Bearer token:

- ``GET  /`` -- overview counts for that ACTIVE enrollment;
- ``GET  /topics`` -- topics under the enrolled offering;
- ``GET  /lessons`` -- lessons under the enrolled offering;
- ``GET  /materials`` -- PUBLISHED materials only (with personal progress);
- ``GET  /materials/{material_id}`` -- one published material;
- ``GET  /materials/{material_id}/content`` -- authorized file bytes
  (``Cache-Control: private, no-store``; never a permanent public URL);
- ``GET  /materials/{material_id}/progress`` -- the caller's progress
  (absent row answers not_started);
- ``PUT  /materials/{material_id}/progress`` -- record in_progress or
  completed (forward-only, idempotent on the current status).

Authorization chain (service-enforced): ACTIVE enrollment owned by caller
-> its offering -> PUBLISHED material under that offering. Unknown or
foreign enrollment/material ids answer the same 404 (L6 existence leak).
Ended enrollments and non-published materials are invisible on this surface.
Teachers and admins are refused by the route guard -- one role per endpoint.

Endpoint bodies stay thin: validate schema -> delegate to the service ->
map the Phase 1 error family onto HTTP -> commit on progress writes.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_STUDENT
from app.core.auth_dependencies import get_current_student
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.student import Student
from app.schemas.student_content import (
    LearningContentOverviewRead,
    MaterialProgressRead,
    MaterialProgressUpdate,
    StudentLessonRead,
    StudentMaterialRead,
    StudentTopicRead,
)
from app.services import student_content_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/me/learning-content", tags=[TAG_STUDENT])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get(
    "/{enrollment_id}",
    response_model=LearningContentOverviewRead,
    summary="Learning content overview for one enrollment",
    description=(
        "Structure and counts for one ACTIVE learning enrollment you own: "
        "offering status, teacher, flattened learning context, and topic / "
        "lesson / published-material counts. Unknown, foreign or ended "
        "enrollment ids answer the same 404 (L6). Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown enrollment id, not yours, or not active"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_learning_content_overview(
    request: Request,
    enrollment_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> LearningContentOverviewRead:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return student_content_service.get_overview(
            session, student.user, enrollment_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{enrollment_id}/topics",
    response_model=list[StudentTopicRead],
    summary="List topics under your enrolled offering",
    description=(
        "The curriculum structure of the offering your ACTIVE enrollment "
        "points at, in stored order. Only structure is returned -- no "
        "teacher drafts or moderation state. Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown enrollment id, not yours, or not active"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_learning_topics(
    request: Request,
    enrollment_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[StudentTopicRead]:
    try:
        return student_content_service.list_topics(
            session, student.user, enrollment_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{enrollment_id}/lessons",
    response_model=list[StudentLessonRead],
    summary="List lessons under your enrolled offering",
    description=(
        "Every lesson under any topic of the offering your ACTIVE "
        "enrollment points at, in stored order. Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown enrollment id, not yours, or not active"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_learning_lessons(
    request: Request,
    enrollment_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[StudentLessonRead]:
    try:
        return student_content_service.list_lessons(
            session, student.user, enrollment_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{enrollment_id}/materials",
    response_model=list[StudentMaterialRead],
    summary="List published materials under your enrolled offering",
    description=(
        "Only PUBLISHED materials under the offering your ACTIVE "
        "enrollment points at. Drafts, pending-review, rejected and "
        "archived works are invisible on this surface. Each row carries "
        "a relative ``content_url`` and your personal progress (if any). "
        "Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown enrollment id, not yours, or not active"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_learning_materials(
    request: Request,
    enrollment_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> list[StudentMaterialRead]:
    try:
        return student_content_service.list_materials(
            session, student.user, enrollment_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{enrollment_id}/materials/{material_id}",
    response_model=StudentMaterialRead,
    summary="Read one published material",
    description=(
        "One PUBLISHED material under the offering of your ACTIVE "
        "enrollment. The material must belong to that enrollment's "
        "offering; drafts, rejected works, other teachers' materials and "
        "unknown ids all answer the same 404 (L6). Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown material, or not under your active enrollment"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_learning_material(
    request: Request,
    enrollment_id: uuid.UUID,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> StudentMaterialRead:
    try:
        return student_content_service.get_material(
            session, student.user, enrollment_id, material_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{enrollment_id}/materials/{material_id}/content",
    summary="Download authorized material file bytes",
    description=(
        "Streams the material's file after re-checking the full "
        "authorization chain (ACTIVE enrollment -> offering -> PUBLISHED "
        "material). Response headers: declared content type, "
        "``Content-Disposition: inline``, ``Cache-Control: private, "
        "no-store``. This is application-level controlled delivery -- no "
        "permanent public storage URL is stored or returned, and this is "
        "not DRM. Students only."
    ),
    responses={
        200: {"description": "File bytes with declared content type"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown material, or not under your active enrollment"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def download_learning_material_content(
    request: Request,
    enrollment_id: uuid.UUID,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> Response:
    try:
        data, content_type, filename = student_content_service.get_material_content(
            session, student.user, enrollment_id, material_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc

    safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "") or "download"
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "/{enrollment_id}/materials/{material_id}/progress",
    response_model=MaterialProgressRead,
    summary="Read your progress on one material",
    description=(
        "The caller's own progress on one published material. A missing "
        "row answers ``status=not_started`` (progress_id null) without "
        "writing anything. Foreign or unpublished materials answer 404. "
        "Students only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown material, or not under your active enrollment"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_learning_material_progress(
    request: Request,
    enrollment_id: uuid.UUID,
    material_id: uuid.UUID,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> MaterialProgressRead:
    try:
        return student_content_service.get_progress(
            session, student.user, enrollment_id, material_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.put(
    "/{enrollment_id}/materials/{material_id}/progress",
    response_model=MaterialProgressRead,
    summary="Record your progress on one material",
    description=(
        "Create or advance the caller's progress row. Only ``in_progress`` "
        "and ``completed`` are writable; re-sending the current status is "
        "idempotent (200, no new row); moving backwards is 409. Progress "
        "is personal to you and this material -- it never transfers across "
        "teachers or offerings. Students only."
    ),
    responses={
        200: {"description": "Progress created or advanced (or idempotent re-send)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a student account"},
        404: {"description": "Unknown material, or not under your active enrollment"},
        409: {"description": "Backwards move, or a non-writable status"},
        422: {"description": "Invalid body (unknown keys / bad status)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def put_learning_material_progress(
    request: Request,
    enrollment_id: uuid.UUID,
    material_id: uuid.UUID,
    payload: MaterialProgressUpdate,
    session: Session = Depends(get_db),
    student: Student = Depends(get_current_student),
) -> MaterialProgressRead:
    try:
        updated = student_content_service.set_progress(
            session, student.user, enrollment_id, material_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated
