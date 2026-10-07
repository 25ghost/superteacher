"""Teacher self-service: topics and lessons under an offering (Phase 2, 2A).

Nested resource semantics — every path starts from the offering the
teacher owns, then descends into its curriculum:

- ``POST   /me/teacher/offerings/{offering_id}/topics`` — add a topic
- ``GET    /me/teacher/offerings/{offering_id}/topics`` — list them (stored order)
- ``GET    /me/teacher/offerings/{offering_id}/topics/{topic_id}`` — one topic
- ``PATCH  /me/teacher/offerings/{offering_id}/topics/{topic_id}`` — amend
- ``DELETE /me/teacher/offerings/{offering_id}/topics/{topic_id}`` — remove
  (and every lesson beneath it)
- ``POST   .../topics/{topic_id}/lessons`` — add a lesson
- ``GET    .../topics/{topic_id}/lessons`` — list them (stored order)
- ``GET    .../topics/{topic_id}/lessons/{lesson_id}`` — one lesson
- ``PATCH  .../topics/{topic_id}/lessons/{lesson_id}`` — amend
- ``DELETE .../topics/{topic_id}/lessons/{lesson_id}`` — remove

Only the teacher who owns the offering may touch its curriculum; a
foreign offering/topic/lesson id answers the same 404 as an unknown one
(L6 existence leak). Authoring (create/amend/remove) additionally
requires an **approved** teacher verification (403 otherwise); reads stay
open to the owning teacher. Students and administrators are refused by
the route guard — this namespace is teacher-only, one role per endpoint.
Unpublished-content exposure is deferred to slice 2C: there are no
student read routes here yet.

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
from app.schemas.curriculum import (
    LessonCreate,
    LessonRead,
    LessonUpdate,
    TopicCreate,
    TopicRead,
    TopicUpdate,
)
from app.services import curriculum_service
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/me/teacher", tags=[TAG_TEACHER])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


# --- topics ---------------------------------------------------------------------------


@router.post(
    "/offerings/{offering_id}/topics",
    response_model=TopicRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a topic under your teaching offering",
    description=(
        "Adds one topic to an offering you own. The topic stays inside "
        "that offering's educational context — it never re-declares the "
        "Admin-owned catalog. ``display_order`` is optional: omitted "
        "assigns the next free position; a position already held by "
        "another topic of the offering is 409. Unknown or foreign "
        "offering id → 404. Teachers only."
    ),
    responses={
        201: {"description": "Topic created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering id, or not one of yours"},
        409: {"description": "The display_order is already held in this offering"},
        422: {"description": "Invalid request (empty body, null title, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def create_my_topic(
    request: Request,
    offering_id: uuid.UUID,
    payload: TopicCreate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> TopicRead:
    try:
        return curriculum_service.create_topic(session, user, offering_id, payload)
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.get(
    "/offerings/{offering_id}/topics",
    response_model=list[TopicRead],
    summary="List your topics for one offering",
    description=(
        "Every topic under one of YOUR offerings, in the stored order "
        "(display_order, then id). Unknown or foreign offering id → 404. "
        "Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering id, or not one of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_my_topics(
    request: Request,
    offering_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[TopicRead]:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return curriculum_service.list_topics(session, user, offering_id)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/offerings/{offering_id}/topics/{topic_id}",
    response_model=TopicRead,
    summary="Retrieve one of your topics",
    description=(
        "Full summary of one topic under an offering you own. A topic "
        "that exists but sits under another offering returns the SAME "
        "404 as an unknown id (L6 existence leak). Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown topic id, or not under an offering of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_topic(
    request: Request,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> TopicRead:
    try:
        return curriculum_service.get_topic(session, user, offering_id, topic_id)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.patch(
    "/offerings/{offering_id}/topics/{topic_id}",
    response_model=TopicRead,
    summary="Amend one of your topics",
    description=(
        "Change the title, description and/or display_order of one of "
        "your topics. At least one field must be supplied. A "
        "display_order already held by another topic of the same "
        "offering is 409 — the unique index keeps the stored sequence "
        "stable rather than silently reordering. Unknown or foreign id → "
        "404. Teachers only."
    ),
    responses={
        200: {"description": "Topic updated"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown topic id, or not under an offering of yours"},
        409: {"description": "The display_order is already held in this offering"},
        422: {"description": "Invalid request (no fields, null title/order, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def update_my_topic(
    request: Request,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    payload: TopicUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> TopicRead:
    try:
        updated = curriculum_service.update_topic(
            session, user, offering_id, topic_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated


@router.delete(
    "/offerings/{offering_id}/topics/{topic_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of your topics",
    description=(
        "Removes one topic under an offering you own — and every lesson "
        "beneath it (the FK is ON DELETE CASCADE; the service deletes "
        "lessons explicitly first so SQLite matches PostgreSQL). "
        "Unknown or foreign id → 404. Teachers only."
    ),
    responses={
        204: {"description": "Topic (and its lessons) removed"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown topic id, or not under an offering of yours"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def delete_my_topic(
    request: Request,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> None:
    try:
        curriculum_service.delete_topic(session, user, offering_id, topic_id)
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


# --- lessons --------------------------------------------------------------------------


@router.post(
    "/offerings/{offering_id}/topics/{topic_id}/lessons",
    response_model=LessonRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a lesson under your topic",
    description=(
        "Adds one lesson to a topic under an offering you own. The "
        "lesson stays inside that topic's educational context. "
        "``display_order`` is optional: omitted assigns the next free "
        "position; a position already held by another lesson of the "
        "topic is 409. Unknown or foreign offering/topic id → 404. "
        "Teachers only."
    ),
    responses={
        201: {"description": "Lesson created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering/topic id, or not one of yours"},
        409: {"description": "The display_order is already held in this topic"},
        422: {"description": "Invalid request (empty body, null title, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def create_my_lesson(
    request: Request,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    payload: LessonCreate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> LessonRead:
    try:
        return curriculum_service.create_lesson(
            session, user, offering_id, topic_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()


@router.get(
    "/offerings/{offering_id}/topics/{topic_id}/lessons",
    response_model=list[LessonRead],
    summary="List your lessons for one topic",
    description=(
        "Every lesson under one of YOUR topics, in the stored order "
        "(display_order, then id). Unknown or foreign offering/topic id "
        "→ 404. Teachers only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown offering/topic id, or not one of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def list_my_lessons(
    request: Request,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> list[LessonRead]:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return curriculum_service.list_lessons(session, user, offering_id, topic_id)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/offerings/{offering_id}/topics/{topic_id}/lessons/{lesson_id}",
    response_model=LessonRead,
    summary="Retrieve one of your lessons",
    description=(
        "Full summary of one lesson under a topic of an offering you "
        "own. A lesson that exists but sits under another topic returns "
        "the SAME 404 as an unknown id (L6 existence leak). Teachers "
        "only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown lesson id, or not under a topic of yours"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def read_my_lesson(
    request: Request,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    lesson_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> LessonRead:
    try:
        return curriculum_service.get_lesson(
            session, user, offering_id, topic_id, lesson_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.patch(
    "/offerings/{offering_id}/topics/{topic_id}/lessons/{lesson_id}",
    response_model=LessonRead,
    summary="Amend one of your lessons",
    description=(
        "Change the title, description and/or display_order of one of "
        "your lessons. At least one field must be supplied. A "
        "display_order already held by another lesson of the same topic "
        "is 409 — the unique index keeps the stored sequence stable "
        "rather than silently reordering. Unknown or foreign id → 404. "
        "Teachers only."
    ),
    responses={
        200: {"description": "Lesson updated"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown lesson id, or not under a topic of yours"},
        409: {"description": "The display_order is already held in this topic"},
        422: {"description": "Invalid request (no fields, null title/order, unknown keys)"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def update_my_lesson(
    request: Request,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    lesson_id: uuid.UUID,
    payload: LessonUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> LessonRead:
    try:
        updated = curriculum_service.update_lesson(
            session, user, offering_id, topic_id, lesson_id, payload
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
    return updated


@router.delete(
    "/offerings/{offering_id}/topics/{topic_id}/lessons/{lesson_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of your lessons",
    description=(
        "Removes one lesson under a topic of an offering you own. "
        "Unknown or foreign id → 404. Teachers only."
    ),
    responses={
        204: {"description": "Lesson removed"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not a teacher account"},
        404: {"description": "Unknown lesson id, or not under a topic of yours"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def delete_my_lesson(
    request: Request,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    lesson_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_teacher),
) -> None:
    try:
        curriculum_service.delete_lesson(
            session, user, offering_id, topic_id, lesson_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    session.commit()
