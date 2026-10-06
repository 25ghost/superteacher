"""Teaching materials: authoring under an offering + admin moderation.

Business rules live here (the same Option-A split as the rest of the
marketplace); the endpoint stays thin and the repositories only write rows:

- only the teacher who **owns** a teaching offering may create or amend
  its materials — a foreign offering/material id answers the same 404 as
  an unknown one (L6 existence leak), never a 403 that would confirm the
  row exists;
- students and administrators are refused at the route guard; if the
  service is reached directly it refuses a non-teacher caller with the
  shared ``LearningForbiddenError``;
- materials stay inside the offering's educational context: they hang off
  ``teaching_offerings`` (and optionally a lesson) and never re-declare
  the Admin-owned catalog — teacher/year/pathway/level/subject are
  derivable through the offering;
- the upload pipeline is: validate (size, content type, magic bytes) →
  write bytes through the ``StorageBackend`` → record a ``file_assets``
  row (storage_key is opaque; never a public path) → create the material
  in ``draft``;
- moderation lifecycle: draft → pending_review → (published | rejected)
  → archived. Only an administrator approves/rejects; a teacher cannot
  publish directly. A rejected material returns to draft through the
  revision flow. Published materials are effectively immutable (409 on
  amend); archive retires them without deleting history;
- deleting a material removes its moderation rows (FK cascade) but the
  stored file bytes are left for a later cleanup job — slice 2B does not
  run background deletion.

Nothing commits here — the API layer owns the transaction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    FileAssetStatus,
    MaterialStatus,
    MaterialType,
    UserRole,
)
from app.models.file_asset import FileAsset
from app.models.lesson import Lesson
from app.models.material import Material
from app.models.teacher import Teacher
from app.models.topic import Topic
from app.models.teaching_offering import TeachingOffering
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import file_asset_repository as file_asset_repo
from app.repositories import material_moderation_repository as moderation_repo
from app.repositories import material_repository as material_repo
from app.repositories import teaching_offering_repository as offering_repo
from app.schemas.material import (
    FileAssetRead,
    MaterialDetailRead,
    MaterialModerationRead,
    MaterialRead,
    MaterialRejectRequest,
    MaterialUpdate,
)
from app.services import file_validation
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)
from app.services.storage import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

#: Editable lifecycle states (amend + revise). Pending review is frozen
#: while a decision is outstanding; published/archived are immutable.
_EDITABLE_STATUSES = frozenset(
    {MaterialStatus.DRAFT.value, MaterialStatus.REJECTED.value}
)

#: Teacher-deletable states (draft and rejected only — never published).
_DELETABLE_STATUSES = frozenset(
    {MaterialStatus.DRAFT.value, MaterialStatus.REJECTED.value}
)


def _load_profile(session: Session, user: User) -> Teacher:
    """The caller's teacher profile (404 when the account has none)."""
    if user.role != UserRole.TEACHER.value:
        raise LearningForbiddenError("teacher role required for this operation")
    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    if profile is None:
        raise LearningNotFoundError("no teacher profile for this account")
    return profile


def _owned_offering(session: Session, profile: Teacher, offering_id: uuid.UUID):
    """One of the caller's offerings — a foreign id 404s like an unknown one."""
    offering = offering_repo.get_by_id_for_update(session, offering_id)
    if offering is None or offering.teacher_id != profile.id:
        raise LearningNotFoundError(f"no teaching offering with id {offering_id}")
    return offering


def _owned_material(
    session: Session, profile: Teacher, material_id: uuid.UUID
) -> Material:
    """One material of the caller's offerings — foreign or unknown → 404."""
    material = material_repo.get_by_id_for_update(session, material_id)
    if material is None:
        raise LearningNotFoundError(f"no material with id {material_id}")
    # Re-check ownership through the offering (not a 403 on foreign rows).
    offering = session.get(TeachingOffering, material.teaching_offering_id)
    if offering is None or offering.teacher_id != profile.id:
        raise LearningNotFoundError(f"no material with id {material_id}")
    return material


def _admin_material(session: Session, material_id: uuid.UUID) -> Material:
    """Any material (admin namespace) — unknown → 404."""
    material = material_repo.get_by_id_for_update(session, material_id)
    if material is None:
        raise LearningNotFoundError(f"no material with id {material_id}")
    return material


def _require_admin(user: User) -> None:
    if user.role != UserRole.ADMIN.value:
        raise LearningForbiddenError("administrator role required for this operation")


def _read_file_asset(asset: FileAsset) -> FileAssetRead:
    return FileAssetRead(
        file_asset_id=asset.id,
        original_filename=asset.original_filename,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        checksum_sha256=asset.checksum_sha256,
        validation_status=asset.validation_status,
        validation_error=asset.validation_error,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _read_material(material: Material, *, with_moderations: bool = False) -> MaterialRead | MaterialDetailRead:
    base = MaterialRead(
        material_id=material.id,
        offering_id=material.teaching_offering_id,
        lesson_id=material.lesson_id,
        title=material.title,
        description=material.description,
        material_type=MaterialType(material.material_type),
        status=MaterialStatus(material.status),
        file_asset=_read_file_asset(material.file_asset),
        created_at=material.created_at,
        updated_at=material.updated_at,
    )
    if not with_moderations:
        return base
    return MaterialDetailRead(
        **base.model_dump(),
        moderations=[
            MaterialModerationRead(
                moderation_id=row.id,
                material_id=row.material_id,
                reviewer_user_id=row.reviewer_user_id,
                decision=row.decision,
                reason=row.reason,
                created_at=row.created_at,
            )
            for row in material.moderations
        ],
    )


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
        metadata_json=json.dumps(payload),
        actor_user_id=actor_user_id,
    )


# --- teacher authoring -----------------------------------------------------------------


def create_material(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    *,
    title: str,
    material_type: MaterialType,
    description: str | None,
    lesson_id: uuid.UUID | None,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    storage: StorageBackend | None = None,
) -> MaterialRead:
    """Validate an upload, store it, and create a draft material.

    Pipeline: content validation → storage write → file_assets row →
    materials row (draft). Any refusal leaves no material row; a storage
    failure after the asset insert rolls back via the API transaction.
    """
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)

    # Optional lesson must sit under this same offering.
    if lesson_id is not None:
        lesson = session.get(Lesson, lesson_id)
        if lesson is None:
            raise LearningNotFoundError(f"no lesson with id {lesson_id}")
        topic_row = session.get(Topic, lesson.topic_id)
        if topic_row is None or topic_row.teaching_offering_id != offering_id:
            raise LearningNotFoundError(f"no lesson with id {lesson_id}")

    # 1. Content validation (raises LearningValidationError on refusal).
    try:
        file_validation.validate_upload(
            data=file_bytes,
            content_type=content_type,
            original_filename=original_filename,
        )
    except file_validation.ValidationError as exc:
        raise LearningValidationError(str(exc)) from exc

    normalized_type = (content_type or "").split(";")[0].strip().lower()
    checksum = hashlib.sha256(file_bytes).hexdigest()

    # 2. File-asset row in validating state; storage key uses the PK once
    #    the row is inserted, so the backend never sees a client-supplied path.
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

    # 3. Material row, always born as draft.
    material = material_repo.create(
        session,
        teaching_offering_id=offering_id,
        lesson_id=lesson_id,
        title=title,
        description=description,
        material_type=material_type.value,
        status=MaterialStatus.DRAFT.value,
        file_asset_id=asset.id,
    )

    _log(
        session,
        user_id=user.id,
        event_type="material_created",
        payload={
            "material_id": str(material.id),
            "teaching_offering_id": str(offering_id),
            "material_type": material.material_type,
            "file_asset_id": str(asset.id),
            "size_bytes": asset.size_bytes,
            "checksum_sha256": asset.checksum_sha256,
        },
    )
    session.flush()
    logger.info(
        "material created",
        extra={"user_id": str(user.id), "material_id": str(material.id)},
    )
    return _read_material(material)


def list_materials(
    session: Session, user: User, offering_id: uuid.UUID
) -> list[MaterialRead]:
    """Every material of one of the caller's offerings, newest first."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    return [
        _read_material(material)  # type: ignore[arg-type]
        for material in material_repo.list_for_offering(session, offering_id)
    ]


def get_material(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    material_id: uuid.UUID,
    *,
    with_moderations: bool = False,
) -> MaterialRead | MaterialDetailRead:
    """Read one of the caller's materials (404 for unknown *or* foreign)."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    material = _owned_material(session, profile, material_id)
    if material.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no material with id {material_id}")
    return _read_material(material, with_moderations=with_moderations)


def update_material(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    material_id: uuid.UUID,
    payload: MaterialUpdate,
) -> MaterialRead:
    """Amend metadata of a draft/rejected material (published is immutable)."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    material = _owned_material(session, profile, material_id)
    if material.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no material with id {material_id}")

    if material.status not in _EDITABLE_STATUSES:
        raise LearningConflictError(
            f"material is {material.status} and can no longer be amended"
        )

    if "title" in payload.model_fields_set:
        material.title = payload.title  # type: ignore[assignment]
    if "description" in payload.model_fields_set:
        material.description = payload.description
    if "material_type" in payload.model_fields_set:
        material.material_type = payload.material_type.value  # type: ignore[union-attr]
    if "lesson_id" in payload.model_fields_set:
        _assert_lesson_in_offering(session, offering_id, payload.lesson_id)
        material.lesson_id = payload.lesson_id

    session.flush()
    _log(
        session,
        user_id=user.id,
        event_type="material_updated",
        payload={
            "material_id": str(material.id),
            "teaching_offering_id": str(offering_id),
            "status": material.status,
        },
    )
    session.flush()
    logger.info(
        "material updated",
        extra={"user_id": str(user.id), "material_id": str(material.id)},
    )
    return _read_material(material)  # type: ignore[arg-type]


def _assert_lesson_in_offering(
    session: Session, offering_id: uuid.UUID, lesson_id: uuid.UUID | None
) -> None:
    if lesson_id is None:
        return
    lesson = session.get(Lesson, lesson_id)
    if lesson is None:
        raise LearningNotFoundError(f"no lesson with id {lesson_id}")
    topic_row = session.get(Topic, lesson.topic_id)
    if topic_row is None or topic_row.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no lesson with id {lesson_id}")


def submit_material(
    session: Session, user: User, offering_id: uuid.UUID, material_id: uuid.UUID
) -> MaterialRead:
    """draft → pending_review: put the material in the admin queue."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    material = _owned_material(session, profile, material_id)
    if material.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no material with id {material_id}")

    if material.status != MaterialStatus.DRAFT.value:
        raise LearningConflictError(
            f"only draft materials can be submitted (material is {material.status})"
        )

    material.status = MaterialStatus.PENDING_REVIEW.value
    session.flush()
    _log(
        session,
        user_id=user.id,
        event_type="material_submitted",
        payload={
            "material_id": str(material.id),
            "teaching_offering_id": str(offering_id),
        },
    )
    session.flush()
    return _read_material(material)  # type: ignore[arg-type]


def revise_material(
    session: Session, user: User, offering_id: uuid.UUID, material_id: uuid.UUID
) -> MaterialRead:
    """rejected → draft: the revision flow before resubmission."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    material = _owned_material(session, profile, material_id)
    if material.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no material with id {material_id}")

    if material.status != MaterialStatus.REJECTED.value:
        raise LearningConflictError(
            f"only rejected materials can be revised (material is {material.status})"
        )

    material.status = MaterialStatus.DRAFT.value
    session.flush()
    _log(
        session,
        user_id=user.id,
        event_type="material_revised",
        payload={
            "material_id": str(material.id),
            "teaching_offering_id": str(offering_id),
        },
    )
    session.flush()
    return _read_material(material)  # type: ignore[arg-type]


def archive_material(
    session: Session, user: User, offering_id: uuid.UUID, material_id: uuid.UUID
) -> MaterialRead:
    """published → archived: retire without deleting history."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    material = _owned_material(session, profile, material_id)
    if material.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no material with id {material_id}")

    if material.status != MaterialStatus.PUBLISHED.value:
        raise LearningConflictError(
            f"only published materials can be archived (material is {material.status})"
        )

    material.status = MaterialStatus.ARCHIVED.value
    session.flush()
    _log(
        session,
        user_id=user.id,
        event_type="material_archived",
        payload={
            "material_id": str(material.id),
            "teaching_offering_id": str(offering_id),
        },
    )
    session.flush()
    return _read_material(material)  # type: ignore[arg-type]


def delete_material(
    session: Session, user: User, offering_id: uuid.UUID, material_id: uuid.UUID
) -> None:
    """Remove a draft/rejected material (moderation rows cascade)."""
    profile = _load_profile(session, user)
    _owned_offering(session, profile, offering_id)
    material = _owned_material(session, profile, material_id)
    if material.teaching_offering_id != offering_id:
        raise LearningNotFoundError(f"no material with id {material_id}")

    if material.status not in _DELETABLE_STATUSES:
        raise LearningConflictError(
            f"only draft or rejected materials can be deleted "
            f"(material is {material.status})"
        )

    material_repo.delete(session, material)
    _log(
        session,
        user_id=user.id,
        event_type="material_deleted",
        payload={
            "material_id": str(material_id),
            "teaching_offering_id": str(offering_id),
        },
    )
    session.flush()
    logger.info(
        "material deleted",
        extra={"user_id": str(user.id), "material_id": str(material_id)},
    )


# --- admin moderation ------------------------------------------------------------------


def admin_list_materials(
    session: Session,
    user: User,
    *,
    status: MaterialStatus | None = None,
    offering_id: uuid.UUID | None = None,
) -> list[MaterialRead]:
    """Moderation queue (or a filtered archive listing)."""
    _require_admin(user)
    return [
        _read_material(material)  # type: ignore[arg-type]
        for material in material_repo.list_for_admin(
            session,
            status=status.value if status else None,
            teaching_offering_id=offering_id,
        )
    ]


def admin_get_material(
    session: Session, user: User, material_id: uuid.UUID
) -> MaterialDetailRead:
    """Any material with its full moderation trail (admin namespace)."""
    _require_admin(user)
    material = _admin_material(session, material_id)
    return _read_material(material, with_moderations=True)  # type: ignore[return-value]


def admin_approve_material(
    session: Session, user: User, material_id: uuid.UUID
) -> MaterialRead:
    """pending_review → published (administrator only)."""
    _require_admin(user)
    material = _admin_material(session, material_id)
    if material.status != MaterialStatus.PENDING_REVIEW.value:
        raise LearningConflictError(
            f"only pending_review materials can be approved "
            f"(material is {material.status})"
        )

    material.status = MaterialStatus.PUBLISHED.value
    moderation_repo.create(
        session,
        material_id=material.id,
        reviewer_user_id=user.id,
        decision="approved",
        reason=None,
    )
    _log(
        session,
        user_id=material.file_asset.uploaded_by_user_id,
        event_type="material_approved",
        payload={"material_id": str(material.id)},
        actor_user_id=user.id,
    )
    session.flush()
    logger.info(
        "material approved",
        extra={"admin_id": str(user.id), "material_id": str(material.id)},
    )
    return _read_material(material)  # type: ignore[arg-type]


def admin_reject_material(
    session: Session,
    user: User,
    material_id: uuid.UUID,
    payload: MaterialRejectRequest,
) -> MaterialRead:
    """pending_review → rejected, recording reviewer + reason (admin only)."""
    _require_admin(user)
    material = _admin_material(session, material_id)
    if material.status != MaterialStatus.PENDING_REVIEW.value:
        raise LearningConflictError(
            f"only pending_review materials can be rejected "
            f"(material is {material.status})"
        )

    material.status = MaterialStatus.REJECTED.value
    moderation_repo.create(
        session,
        material_id=material.id,
        reviewer_user_id=user.id,
        decision="rejected",
        reason=payload.reason,
    )
    _log(
        session,
        user_id=material.file_asset.uploaded_by_user_id,
        event_type="material_rejected",
        payload={"material_id": str(material.id), "reason": payload.reason},
        actor_user_id=user.id,
    )
    session.flush()
    logger.info(
        "material rejected",
        extra={"admin_id": str(user.id), "material_id": str(material.id)},
    )
    return _read_material(material)  # type: ignore[arg-type]


def admin_archive_material(
    session: Session, user: User, material_id: uuid.UUID
) -> MaterialRead:
    """published → archived (administrator path)."""
    _require_admin(user)
    material = _admin_material(session, material_id)
    if material.status != MaterialStatus.PUBLISHED.value:
        raise LearningConflictError(
            f"only published materials can be archived "
            f"(material is {material.status})"
        )

    material.status = MaterialStatus.ARCHIVED.value
    _log(
        session,
        user_id=material.file_asset.uploaded_by_user_id,
        event_type="material_archived",
        payload={"material_id": str(material.id)},
        actor_user_id=user.id,
    )
    session.flush()
    return _read_material(material)  # type: ignore[arg-type]
