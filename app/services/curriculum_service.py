"""Topics and lessons: the teacher's curriculum beneath their offerings.

Business rules live here (the same Option-A split as the rest of the
marketplace); the endpoint stays thin and the repositories only write
rows:

- only the teacher who **owns** a teaching offering may create or amend
  its topics/lessons — a foreign offering/topic/lesson id answers the
  same 404 as an unknown one (L6 existence leak), never a 403 that would
  confirm the row exists;
- students and administrators are refused at the route guard; if the
  service is reached directly it refuses a non-teacher caller with the
  shared ``LearningForbiddenError``;
- topics stay inside the offering's educational context: the topic hangs
  off ``teaching_offerings`` and never re-declares the Admin-owned
  catalog (``learning_contexts``);
- ordering is a domain invariant: ``display_order`` is unique within the
  parent (offering for topics, topic for lessons) and >= 1. Creating
  assigns the next free position; a PATCH onto a held position is a 409,
  so the stored sequence stays stable rather than silently reordering;
- deleting a topic removes its lessons (explicit service delete, with the
  FK cascade as the database-level backstop).

Nothing commits here — the API layer owns the transaction.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import UserRole
from app.models.lesson import Lesson
from app.models.teacher import Teacher
from app.models.topic import Topic
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import lesson_repository as lesson_repo
from app.repositories import teaching_offering_repository as offering_repo
from app.repositories import topic_repository as topic_repo
from app.schemas.curriculum import (
    LessonCreate,
    LessonRead,
    LessonUpdate,
    TopicCreate,
    TopicRead,
    TopicUpdate,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)

logger = logging.getLogger(__name__)


def _load_profile(session: Session, user: User) -> Teacher:
    """The caller's teacher profile (404 when the account has none)."""
    if user.role != UserRole.TEACHER.value:
        raise LearningForbiddenError("teacher role required for this operation")
    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    if profile is None:
        raise LearningNotFoundError("no teacher profile for this account")
    return profile


def _owned_offering(
    session: Session, profile: Teacher, offering_id: uuid.UUID
):
    """One of the caller's offerings — a foreign id 404s like an unknown one.

    UUIDs are identifiers, not authorization: answering 403 for someone
    else's id would confirm that it exists.
    """
    offering = offering_repo.get_by_id_for_update(session, offering_id)
    if offering is None or offering.teacher_id != profile.id:
        raise LearningNotFoundError(f"no teaching offering with id {offering_id}")
    return offering


def _owned_topic(session: Session, offering_id: uuid.UUID, topic_id: uuid.UUID) -> Topic:
    """One topic under the given offering — foreign or unknown → 404."""
    topic = topic_repo.get_by_id_for_update(session, topic_id)
    if topic is None or topic.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no topic with id {topic_id}")
    return topic


def _owned_lesson(session: Session, topic_id: uuid.UUID, lesson_id: uuid.UUID) -> Lesson:
    """One lesson under the given topic — foreign or unknown → 404."""
    lesson = lesson_repo.get_by_id_for_update(session, lesson_id)
    if lesson is None or lesson.topic_id != topic_id:
        raise LearningNotFoundError(f"no lesson with id {lesson_id}")
    return lesson


def _resolve_order(
    session: Session,
    *,
    repo_next,
    parent_id: uuid.UUID,
    supplied: int | None,
) -> int:
    """The position to write: the supplied one, or the parent's next free slot."""
    if supplied is not None:
        return supplied
    return repo_next(session, parent_id)


def _read_topic(topic: Topic) -> TopicRead:
    return TopicRead(
        topic_id=topic.id,
        offering_id=topic.teaching_offering_id,
        title=topic.title,
        description=topic.description,
        display_order=topic.display_order,
        created_at=topic.created_at,
        updated_at=topic.updated_at,
    )


def _read_lesson(lesson: Lesson, *, offering_id: uuid.UUID) -> LessonRead:
    return LessonRead(
        lesson_id=lesson.id,
        topic_id=lesson.topic_id,
        offering_id=offering_id,
        title=lesson.title,
        description=lesson.description,
        display_order=lesson.display_order,
        created_at=lesson.created_at,
        updated_at=lesson.updated_at,
    )


# --- topics ---------------------------------------------------------------------------


def create_topic(
    session: Session, user: User, offering_id: uuid.UUID, payload: TopicCreate
) -> TopicRead:
    """Add one topic under the caller's own offering, then audit it."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)

    display_order = _resolve_order(
        session,
        repo_next=topic_repo.next_display_order,
        parent_id=offering_id,
        supplied=payload.display_order,
    )
    try:
        topic = topic_repo.create(
            session,
            teaching_offering_id=offering_id,
            title=payload.title,
            description=payload.description,
            display_order=display_order,
        )
    except IntegrityError as exc:
        # Lost the race on uq_topics_teaching_offering_display_order_key.
        raise LearningConflictError(
            f"a topic already occupies display_order {display_order} "
            "in this offering"
        ) from exc

    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="topic_created",
        metadata_json=json.dumps(
            {
                "topic_id": str(topic.id),
                "teaching_offering_id": str(offering_id),
                "display_order": topic.display_order,
            }
        ),
    )
    session.flush()
    logger.info(
        "topic created",
        extra={"user_id": str(user.id), "topic_id": str(topic.id)},
    )
    return _read_topic(topic)


def list_topics(
    session: Session, user: User, offering_id: uuid.UUID
) -> list[TopicRead]:
    """Every topic of one of the caller's offerings, in stored order."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    return [
        _read_topic(topic)
        for topic in topic_repo.list_for_offering(session, offering_id)
    ]


def get_topic(
    session: Session, user: User, offering_id: uuid.UUID, topic_id: uuid.UUID
) -> TopicRead:
    """Read one of the caller's topics (404 for unknown *or* foreign)."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    return _read_topic(_owned_topic(session, offering_id, topic_id))


def update_topic(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    payload: TopicUpdate,
) -> TopicRead:
    """Amend title, description and/or position of one of the caller's topics."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    topic = _owned_topic(session, offering_id, topic_id)

    if "title" in payload.model_fields_set:
        topic.title = payload.title
    if "description" in payload.model_fields_set:
        topic.description = payload.description
    if "display_order" in payload.model_fields_set:
        topic.display_order = payload.display_order
    requested_order = topic.display_order

    try:
        session.flush()
    except IntegrityError as exc:
        raise LearningConflictError(
            f"a topic already occupies display_order {requested_order} "
            "in this offering"
        ) from exc

    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="topic_updated",
        metadata_json=json.dumps(
            {
                "topic_id": str(topic.id),
                "teaching_offering_id": str(offering_id),
                "display_order": topic.display_order,
            }
        ),
    )
    session.flush()
    logger.info(
        "topic updated",
        extra={"user_id": str(user.id), "topic_id": str(topic.id)},
    )
    return _read_topic(topic)


def delete_topic(
    session: Session, user: User, offering_id: uuid.UUID, topic_id: uuid.UUID
) -> None:
    """Remove one of the caller's topics — and every lesson beneath it."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    topic = _owned_topic(session, offering_id, topic_id)

    lesson_count = lesson_repo.count_for_topic(session, topic_id)
    lesson_repo.delete_many_for_topic(session, topic_id)
    topic_repo.delete(session, topic)

    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="topic_deleted",
        metadata_json=json.dumps(
            {
                "topic_id": str(topic_id),
                "teaching_offering_id": str(offering_id),
                "lesson_count": lesson_count,
            }
        ),
    )
    session.flush()
    logger.info(
        "topic deleted",
        extra={"user_id": str(user.id), "topic_id": str(topic_id)},
    )


# --- lessons --------------------------------------------------------------------------


def create_lesson(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    payload: LessonCreate,
) -> LessonRead:
    """Add one lesson under the caller's own topic, then audit it."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    _owned_topic(session, offering_id, topic_id)

    display_order = _resolve_order(
        session,
        repo_next=lesson_repo.next_display_order,
        parent_id=topic_id,
        supplied=payload.display_order,
    )
    try:
        lesson = lesson_repo.create(
            session,
            topic_id=topic_id,
            title=payload.title,
            description=payload.description,
            display_order=display_order,
        )
    except IntegrityError as exc:
        raise LearningConflictError(
            f"a lesson already occupies display_order {display_order} "
            "in this topic"
        ) from exc

    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="lesson_created",
        metadata_json=json.dumps(
            {
                "lesson_id": str(lesson.id),
                "topic_id": str(topic_id),
                "teaching_offering_id": str(offering_id),
                "display_order": lesson.display_order,
            }
        ),
    )
    session.flush()
    logger.info(
        "lesson created",
        extra={"user_id": str(user.id), "lesson_id": str(lesson.id)},
    )
    return _read_lesson(lesson, offering_id=offering_id)


def list_lessons(
    session: Session, user: User, offering_id: uuid.UUID, topic_id: uuid.UUID
) -> list[LessonRead]:
    """Every lesson of one of the caller's topics, in stored order."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    _owned_topic(session, offering_id, topic_id)
    return [
        _read_lesson(lesson, offering_id=offering_id)
        for lesson in lesson_repo.list_for_topic(session, topic_id)
    ]


def get_lesson(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    lesson_id: uuid.UUID,
) -> LessonRead:
    """Read one of the caller's lessons (404 for unknown *or* foreign)."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    _owned_topic(session, offering_id, topic_id)
    return _read_lesson(
        _owned_lesson(session, topic_id, lesson_id), offering_id=offering_id
    )


def update_lesson(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    lesson_id: uuid.UUID,
    payload: LessonUpdate,
) -> LessonRead:
    """Amend title, description and/or position of one of the caller's lessons."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    _owned_topic(session, offering_id, topic_id)
    lesson = _owned_lesson(session, topic_id, lesson_id)

    if "title" in payload.model_fields_set:
        lesson.title = payload.title
    if "description" in payload.model_fields_set:
        lesson.description = payload.description
    if "display_order" in payload.model_fields_set:
        lesson.display_order = payload.display_order
    requested_order = lesson.display_order

    try:
        session.flush()
    except IntegrityError as exc:
        raise LearningConflictError(
            f"a lesson already occupies display_order {requested_order} "
            "in this topic"
        ) from exc

    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="lesson_updated",
        metadata_json=json.dumps(
            {
                "lesson_id": str(lesson.id),
                "topic_id": str(topic_id),
                "teaching_offering_id": str(offering_id),
                "display_order": lesson.display_order,
            }
        ),
    )
    session.flush()
    logger.info(
        "lesson updated",
        extra={"user_id": str(user.id), "lesson_id": str(lesson.id)},
    )
    return _read_lesson(lesson, offering_id=offering_id)


def delete_lesson(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    topic_id: uuid.UUID,
    lesson_id: uuid.UUID,
) -> None:
    """Remove one of the caller's lessons."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    _owned_topic(session, offering_id, topic_id)
    lesson = _owned_lesson(session, topic_id, lesson_id)
    lesson_repo.delete(session, lesson)

    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="lesson_deleted",
        metadata_json=json.dumps(
            {
                "lesson_id": str(lesson_id),
                "topic_id": str(topic_id),
                "teaching_offering_id": str(offering_id),
            }
        ),
    )
    session.flush()
    logger.info(
        "lesson deleted",
        extra={"user_id": str(user.id), "lesson_id": str(lesson_id)},
    )
