"""Student content access + basic material progress (Phase 2, slice 2C).

Authorization chain enforced for every read and every progress write:

    Student (role + profile)
        -> ACTIVE LearningEnrollment (path enrollment, owned by caller)
        -> TeachingOffering
        -> Lesson / Topic (optional structure under the offering)
        -> PUBLISHED Material

Knowing a material_id is never enough: without an ACTIVE enrollment on the
material's offering, the material answers exactly like an unknown id (L6
existence leak). Cross-teacher isolation falls out of this rule -- Teacher
Alice's and Teacher Brian's offerings are different rows, so a student
enrolled in Alice's cannot reach Brian's materials.

Only content published for the student's active offering is returned.
Drafts, moderation metadata and storage paths never appear on this surface.

Basic progress is personal to (student, material):

- forward-only: not_started -> in_progress -> completed;
- re-sending the current status is idempotent (no error, no new row);
- backwards moves are a 409;
- progress cannot be recorded for unpublished material, or for a material
  outside the student's active offering;
- historical progress stays with the material it was earned on -- switching
  teachers does not transfer it.

File access is application-level: the content endpoint re-checks the full
authorization chain and returns bytes from the storage backend. No permanent
public URL is ever stored or returned; this is controlled delivery, not DRM.

Nothing commits here -- the API layer owns the transaction.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import (
    LearningEnrollmentStatus,
    MaterialProgressStatus,
    MaterialStatus,
    UserRole,
)
from app.models.file_asset import FileAsset
from app.models.learning_enrollment import LearningEnrollment
from app.models.lesson import Lesson
from app.models.material import Material
from app.models.material_progress import MaterialProgress
from app.models.student import Student
from app.models.teaching_offering import TeachingOffering
from app.models.topic import Topic
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import learning_enrollment_repository as enrollment_repo
from app.repositories import material_progress_repository as progress_repo
from app.repositories import material_repository as material_repo
from app.repositories import topic_repository as topic_repo
from app.schemas.student_content import (
    LearningContentOverviewRead,
    MaterialProgressRead,
    MaterialProgressUpdate,
    StudentLessonRead,
    StudentMaterialFileRead,
    StudentMaterialRead,
    StudentTopicRead,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)
from app.services.storage import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

_PUBLISHED = MaterialStatus.PUBLISHED.value

_WRITABLE_PROGRESS = frozenset(
    {
        MaterialProgressStatus.IN_PROGRESS.value,
        MaterialProgressStatus.COMPLETED.value,
    }
)

_PROGRESS_RANK = {
    MaterialProgressStatus.NOT_STARTED.value: 0,
    MaterialProgressStatus.IN_PROGRESS.value: 1,
    MaterialProgressStatus.COMPLETED.value: 2,
}


@dataclass(frozen=True)
class EnrollmentRef:
    """Resolved (enrollment, offering) pair for the authorization chain."""

    enrollment: LearningEnrollment
    offering: TeachingOffering

    @property
    def offering_id(self) -> uuid.UUID:
        return self.offering.id

    @property
    def enrollment_id(self) -> uuid.UUID:
        return self.enrollment.id


def _load_student(session: Session, user: User) -> Student:
    """The caller's student profile (refuses non-students, 404 if missing)."""
    if user.role != UserRole.STUDENT.value:
        raise LearningForbiddenError("student role required for this operation")
    profile = session.scalar(
        select(Student).where(Student.user_id == user.id)
    )
    if profile is None:
        raise LearningNotFoundError("no student profile for this account")
    return profile


def _active_enrollment(
    session: Session, student: Student, enrollment_id: uuid.UUID
) -> EnrollmentRef:
    """One ACTIVE enrollment owned by the caller -- else 404 (L6).

    Foreign or unknown enrollment ids answer the same 404; an enrollment
    that exists but is ended also 404s on this surface (active access only).
    """
    enrollment = enrollment_repo.get_by_id_for_update(session, enrollment_id)
    if (
        enrollment is None
        or enrollment.student_id != student.id
        or enrollment.status != LearningEnrollmentStatus.ACTIVE.value
    ):
        raise LearningNotFoundError(
            f"no active learning enrollment with id {enrollment_id}"
        )
    offering = session.get(TeachingOffering, enrollment.teaching_offering_id)
    if offering is None:
        raise LearningNotFoundError(
            f"no active learning enrollment with id {enrollment_id}"
        )
    return EnrollmentRef(enrollment=enrollment, offering=offering)


def _published_material(
    session: Session,
    student: Student,
    material_id: uuid.UUID,
    *,
    offering_id: uuid.UUID,
) -> Material:
    """A PUBLISHED material under the student's ACTIVE offering (else 404).

    Draft / pending_review / rejected / archived materials and materials
    under another offering answer the same 404 as unknown ids.
    """
    material = material_repo.get_by_id_for_update(session, material_id)
    if material is None or material.status != _PUBLISHED:
        raise LearningNotFoundError(f"no material with id {material_id}")
    if material.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no material with id {material_id}")
    return material


def _read_progress(row: MaterialProgress | None) -> MaterialProgressRead | None:
    if row is None:
        return None
    return MaterialProgressRead(
        progress_id=row.id,
        material_id=row.material_id,
        student_id=row.student_id,
        status=MaterialProgressStatus(row.status),
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _content_url(enrollment_id: uuid.UUID, material_id: uuid.UUID) -> str:
    """Relative application path -- never a storage URL."""
    return (
        f"/api/v1/me/learning-content/{enrollment_id}"
        f"/materials/{material_id}/content"
    )


def _read_material(
    material: Material,
    *,
    enrollment_id: uuid.UUID,
    progress: MaterialProgress | None,
) -> StudentMaterialRead:
    asset: FileAsset = material.file_asset
    return StudentMaterialRead(
        material_id=material.id,
        offering_id=material.teaching_offering_id,
        lesson_id=material.lesson_id,
        title=material.title,
        description=material.description,
        material_type=material.material_type,
        status=material.status,
        file=StudentMaterialFileRead(
            file_asset_id=asset.id,
            original_filename=asset.original_filename,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
        ),
        content_url=_content_url(enrollment_id, material.id),
        progress=_read_progress(progress),
        created_at=material.created_at,
        updated_at=material.updated_at,
    )


def _audit_progress(
    session: Session,
    *,
    student: Student,
    material_id: uuid.UUID,
    event_type: str,
    status: str,
) -> None:
    auth_event_repo.log_event(
        session,
        user_id=student.user_id,
        event_type=event_type,
        metadata_json=json.dumps(
            {"material_id": str(material_id), "status": status}
        ),
    )
    session.flush()


def _list_lessons_for_offering(
    session: Session, teaching_offering_id: uuid.UUID
) -> list[Lesson]:
    """Every lesson under any topic of this offering, in stored order."""
    stmt = (
        select(Lesson)
        .join(Topic, Lesson.topic_id == Topic.id)
        .where(Topic.teaching_offering_id == teaching_offering_id)
        .order_by(Topic.display_order, Topic.id, Lesson.display_order, Lesson.id)
    )
    return list(session.scalars(stmt))


def get_overview(
    session: Session, user: User, enrollment_id: uuid.UUID
) -> LearningContentOverviewRead:
    """Counts + offering context for one ACTIVE enrollment (student only)."""
    student = _load_student(session, user)
    ref = _active_enrollment(session, student, enrollment_id)
    context = ref.enrollment.learning_context
    topics = topic_repo.list_for_offering(session, ref.offering_id)
    lessons = _list_lessons_for_offering(session, ref.offering_id)
    published = material_repo.list_for_admin(
        session, status=_PUBLISHED, teaching_offering_id=ref.offering_id
    )
    return LearningContentOverviewRead(
        enrollment_id=ref.enrollment_id,
        offering_id=ref.offering_id,
        offering_status=ref.offering.status,
        offering_description=ref.offering.description,
        teacher_name=ref.offering.teacher.full_name,
        learning_context_id=context.id,
        academic_year=context.academic_year.name,
        pathway_code=context.pathway.code,
        pathway_name=context.pathway.name,
        level_code=context.education_level.code,
        level_name=context.education_level.name,
        subject_code=context.subject.code,
        subject_name=context.subject.name,
        topic_count=len(topics),
        lesson_count=len(lessons),
        published_material_count=len(published),
    )


def list_topics(
    session: Session, user: User, enrollment_id: uuid.UUID
) -> list[StudentTopicRead]:
    """Topics under the student's enrolled offering (structure, stored order)."""
    student = _load_student(session, user)
    ref = _active_enrollment(session, student, enrollment_id)
    return [
        StudentTopicRead(
            topic_id=topic.id,
            offering_id=topic.teaching_offering_id,
            title=topic.title,
            description=topic.description,
            display_order=topic.display_order,
            created_at=topic.created_at,
            updated_at=topic.updated_at,
        )
        for topic in topic_repo.list_for_offering(session, ref.offering_id)
    ]


def list_lessons(
    session: Session, user: User, enrollment_id: uuid.UUID
) -> list[StudentLessonRead]:
    """Lessons under the student's enrolled offering (structure, stored order)."""
    student = _load_student(session, user)
    ref = _active_enrollment(session, student, enrollment_id)
    lessons = _list_lessons_for_offering(session, ref.offering_id)
    return [
        StudentLessonRead(
            lesson_id=lesson.id,
            topic_id=lesson.topic_id,
            offering_id=ref.offering_id,
            title=lesson.title,
            description=lesson.description,
            display_order=lesson.display_order,
            created_at=lesson.created_at,
            updated_at=lesson.updated_at,
        )
        for lesson in lessons
    ]


def list_materials(
    session: Session, user: User, enrollment_id: uuid.UUID
) -> list[StudentMaterialRead]:
    """Published materials under the student's enrolled offering only."""
    student = _load_student(session, user)
    ref = _active_enrollment(session, student, enrollment_id)
    materials = material_repo.list_for_admin(
        session, status=_PUBLISHED, teaching_offering_id=ref.offering_id
    )
    progress_by_material = {
        row.material_id: row
        for row in progress_repo.list_published_for_offering(
            session, student.id, ref.offering_id
        )
    }
    return [
        _read_material(
            material,
            enrollment_id=ref.enrollment_id,
            progress=progress_by_material.get(material.id),
        )
        for material in materials
    ]


def get_material(
    session: Session, user: User, enrollment_id: uuid.UUID, material_id: uuid.UUID
) -> StudentMaterialRead:
    """One published material under the student's enrolled offering."""
    student = _load_student(session, user)
    ref = _active_enrollment(session, student, enrollment_id)
    material = _published_material(
        session, student, material_id, offering_id=ref.offering_id
    )
    progress = progress_repo.get_for_student_material(session, student.id, material.id)
    return _read_material(material, enrollment_id=ref.enrollment_id, progress=progress)


def get_material_content(
    session: Session,
    user: User,
    enrollment_id: uuid.UUID,
    material_id: uuid.UUID,
    *,
    storage: StorageBackend | None = None,
) -> tuple[bytes, str, str]:
    """Authorized file bytes + content type + filename (application delivery).

    Re-checks the full authorization chain on every fetch. The storage
    backend is never exposed to the client -- only the bytes, the declared
    content type and a safe download filename leave this function.
    """
    student = _load_student(session, user)
    ref = _active_enrollment(session, student, enrollment_id)
    material = _published_material(
        session, student, material_id, offering_id=ref.offering_id
    )
    asset = material.file_asset
    backend = storage or get_storage_backend()
    data = backend.read(asset.storage_key)
    return data, asset.content_type, asset.original_filename


def get_progress(
    session: Session, user: User, enrollment_id: uuid.UUID, material_id: uuid.UUID
) -> MaterialProgressRead:
    """The caller's progress on one published material (absent -> not_started).

    A missing row is returned as ``not_started`` rather than 404 so the
    client always has a status to render; no row is written on this read.
    """
    student = _load_student(session, user)
    ref = _active_enrollment(session, student, enrollment_id)
    material = _published_material(
        session, student, material_id, offering_id=ref.offering_id
    )
    row = progress_repo.get_for_student_material(session, student.id, material_id)
    if row is None:
        return MaterialProgressRead(
            progress_id=None,
            material_id=material.id,
            student_id=student.id,
            status=MaterialProgressStatus.NOT_STARTED,
            started_at=None,
            completed_at=None,
            created_at=material.created_at,
            updated_at=material.updated_at,
        )
    return _read_progress(row)  # type: ignore[return-value]


def set_progress(
    session: Session,
    user: User,
    enrollment_id: uuid.UUID,
    material_id: uuid.UUID,
    payload: MaterialProgressUpdate,
) -> MaterialProgressRead:
    """Create or advance the caller's progress on one published material.

    Idempotent on the current status; forward-only otherwise (409 on a
    backwards move). A new row is stamped ``started_at`` when entering
    ``in_progress`` and ``completed_at`` when entering ``completed``.
    """
    student = _load_student(session, user)
    ref = _active_enrollment(session, student, enrollment_id)
    _published_material(
        session, student, material_id, offering_id=ref.offering_id
    )

    target = payload.status.value
    if target not in _WRITABLE_PROGRESS:
        raise LearningConflictError(
            f"progress status {target!r} is not writable -- "
            "record in_progress or completed"
        )

    row = progress_repo.get_for_student_material_for_update(
        session, student.id, material_id
    )
    now = datetime.now(timezone.utc)

    if row is None:
        started_at = now if target == MaterialProgressStatus.IN_PROGRESS.value else None
        completed_at = now if target == MaterialProgressStatus.COMPLETED.value else None
        try:
            row = progress_repo.create(
                session,
                student_id=student.id,
                material_id=material_id,
                status=target,
                started_at=started_at,
                completed_at=completed_at,
            )
        except IntegrityError as exc:
            raise LearningConflictError(
                "a progress record already exists for this material"
            ) from exc
        _audit_progress(
            session,
            student=student,
            material_id=material_id,
            event_type="material_progress_created",
            status=target,
        )
        session.flush()
        logger.info(
            "material progress created",
            extra={
                "student_id": str(student.id),
                "material_id": str(material_id),
                "status": target,
            },
        )
        return _read_progress(row)  # type: ignore[return-value]

    # Idempotent re-send of the current status.
    if row.status == target:
        return _read_progress(row)  # type: ignore[return-value]

    # Forward-only.
    if _PROGRESS_RANK[target] < _PROGRESS_RANK[row.status]:
        raise LearningConflictError(
            f"progress cannot move from {row.status!r} to {target!r}"
        )

    previous = row.status
    row.status = target
    if target == MaterialProgressStatus.IN_PROGRESS.value and row.started_at is None:
        row.started_at = now
    if target == MaterialProgressStatus.COMPLETED.value and row.completed_at is None:
        row.completed_at = now

    _audit_progress(
        session,
        student=student,
        material_id=material_id,
        event_type="material_progress_updated",
        status=f"{previous}->{target}",
    )
    session.flush()
    logger.info(
        "material progress updated",
        extra={
            "student_id": str(student.id),
            "material_id": str(material_id),
            "status": target,
        },
    )
    return _read_progress(row)  # type: ignore[return-value]
