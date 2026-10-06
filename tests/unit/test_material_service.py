"""Unit tests: teaching materials + admin moderation (Phase 2, 2B).

In-memory SQLite, no HTTP — the service layer's own contract:

- only the teacher who owns an offering may create/amend/submit/archive
  its materials; a foreign offering/material id answers the same 404 as
  an unknown one (L6 existence leak);
- the upload pipeline validates size, content type and magic bytes before
  anything is stored; a refused upload leaves no material row;
- materials are born ``draft``; only an administrator may publish
  (approve) or reject; a teacher cannot publish directly;
- published materials are effectively immutable (409 on amend);
- a rejected material revises back to draft and may be resubmitted;
- moderation rows record reviewer, decision, reason.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    MaterialStatus,
    MaterialType,
    TeacherVerificationStatus,
    UserRole,
    UserStatus,
)
from app.models.file_asset import FileAsset
from app.models.material import Material
from app.models.material_moderation import MaterialModeration
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.subject import Subject
from app.models.teacher import Teacher
from app.models.user import User
from app.schemas.material import MaterialRejectRequest, MaterialUpdate
from app.schemas.learning import TeachingOfferingCreate
from app.services import material_service, teaching_offering_service
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)
from app.services.storage import StorageBackend

PASSWORD = "correct horse battery staple"

_PDF_BYTES = b"%PDF-1.4 fake pdf content"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class MemoryStorage(StorageBackend):
    """In-memory backend so unit tests never touch the real filesystem."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def save(self, storage_key: str, data: bytes, *, content_type: str) -> None:
        self.objects[storage_key] = data

    def read(self, storage_key: str) -> bytes:
        return self.objects[storage_key]

    def delete(self, storage_key: str) -> None:
        self.objects.pop(storage_key, None)

    def exists(self, storage_key: str) -> bool:
        return storage_key in self.objects


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def storage() -> MemoryStorage:
    return MemoryStorage()


@pytest.fixture()
def catalog(session: Session) -> dict:
    from datetime import date

    year = AcademicYear(
        name="2026/2027",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 6, 30),
        status=AcademicYearStatus.ACTIVE.value,
    )
    pathway = Pathway(code="O_LEVEL", name="O Level")
    level = EducationLevel(code="S3", name="Senior 3", level_number=3)
    session.add_all([year, pathway, level])
    session.flush()
    session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

    program = Program(code="COMB", name="Combination", program_type="combination")
    session.add(program)
    session.flush()
    program_version = ProgramVersion(
        program_id=program.id,
        academic_year_id=year.id,
        pathway_id=pathway.id,
        education_level_id=level.id,
        code="COMB-2026",
        name="Combination 2026",
    )
    session.add(program_version)

    maths = Subject(code="MATH", name="Mathematics")
    session.add(maths)
    session.flush()
    session.add(
        ProgramSubject(
            program_version_id=program_version.id,
            subject_id=maths.id,
        )
    )
    session.commit()
    return {
        "year": year,
        "pathway": pathway,
        "level": level,
        "program_version": program_version,
        "maths": maths,
    }


def _teacher_user(
    session: Session, *, verification: str, email: str | None = None
) -> User:
    user = User(
        email=email or f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(
            user_id=user.id,
            full_name="Test Teacher",
            verification_status=verification,
        )
    )
    session.commit()
    return user


@pytest.fixture()
def approved_teacher(session: Session) -> User:
    return _teacher_user(session, verification=TeacherVerificationStatus.APPROVED.value)


@pytest.fixture()
def other_teacher(session: Session) -> User:
    return _teacher_user(session, verification=TeacherVerificationStatus.APPROVED.value)


@pytest.fixture()
def student(session: Session) -> User:
    user = User(
        email=f"student-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def admin(session: Session) -> User:
    user = User(
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.ADMIN.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.commit()
    return user


def _offering(session: Session, catalog: dict, teacher: User) -> uuid.UUID:
    read = teaching_offering_service.create_offering(
        session,
        teacher,
        TeachingOfferingCreate(
            academic_year_id=catalog["year"].id,
            pathway_id=catalog["pathway"].id,
            education_level_id=catalog["level"].id,
            program_version_id=catalog["program_version"].id,
            subject_id=catalog["maths"].id,
        ),
    )
    session.commit()
    return read.offering_id


@pytest.fixture()
def offering_id(session: Session, catalog: dict, approved_teacher: User) -> uuid.UUID:
    return _offering(session, catalog, approved_teacher)


@pytest.fixture()
def other_offering_id(
    session: Session, catalog: dict, other_teacher: User
) -> uuid.UUID:
    return _offering(session, catalog, other_teacher)


def _upload(
    session: Session,
    teacher: User,
    offering: uuid.UUID,
    storage: MemoryStorage,
    *,
    title: str = "Algebra worksheet",
    material_type: MaterialType = MaterialType.EXERCISE,
    data: bytes = _PDF_BYTES,
    content_type: str = "application/pdf",
    filename: str = "worksheet.pdf",
    lesson_id: uuid.UUID | None = None,
):
    return material_service.create_material(
        session,
        teacher,
        offering,
        title=title,
        material_type=material_type,
        description="Practice set",
        lesson_id=lesson_id,
        file_bytes=data,
        original_filename=filename,
        content_type=content_type,
        storage=storage,
    )


# --- upload pipeline ------------------------------------------------------------------


def test_teacher_can_upload_and_get_a_draft_material(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
) -> None:
    read = _upload(session, approved_teacher, offering_id, storage)
    session.commit()

    assert read.offering_id == offering_id
    assert read.title == "Algebra worksheet"
    assert read.material_type == MaterialType.EXERCISE
    assert read.status == MaterialStatus.DRAFT
    assert read.file_asset.validation_status == "stored"
    assert read.file_asset.original_filename == "worksheet.pdf"
    assert len(read.file_asset.checksum_sha256) == 64
    assert read.file_asset.size_bytes == len(_PDF_BYTES)
    # Opaque storage key is written to the backend; never returned to clients.
    assert any(key.startswith("file_assets/") for key in storage.objects)
    assert not hasattr(read, "storage_key")

    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
    ).all()
    material_events = [e.event_type for e in events if e.event_type.startswith("material_")]
    assert material_events == ["material_created"]


@pytest.mark.parametrize(
    ("data", "content_type", "filename"),
    [
        (b"", "application/pdf", "empty.pdf"),
        (b"not a pdf", "application/pdf", "fake.pdf"),  # magic mismatch
        (_PNG_BYTES, "application/pdf", "lying.png"),  # magic vs declared
        (b"hello", "application/x-msdownload", "evil.exe"),  # disallowed type
        (_PDF_BYTES, "application/pdf", ""),  # missing filename
    ],
)
def test_invalid_upload_is_refused_and_leaves_no_material(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
    data: bytes,
    content_type: str,
    filename: str,
) -> None:
    with pytest.raises(LearningValidationError):
        _upload(
            session,
            approved_teacher,
            offering_id,
            storage,
            data=data,
            content_type=content_type,
            filename=filename,
        )
    session.rollback()
    assert session.scalars(select(Material)).all() == []
    assert session.scalars(select(FileAsset)).all() == []


def test_teacher_cannot_upload_for_another_teachers_offering(
    session: Session,
    approved_teacher: User,
    other_offering_id: uuid.UUID,
    storage: MemoryStorage,
) -> None:
    unknown = uuid.uuid4()
    for target in (other_offering_id, unknown):
        with pytest.raises(LearningNotFoundError) as excinfo:
            _upload(session, approved_teacher, target, storage)
        message = str(excinfo.value)
        assert message.startswith("no teaching offering with id ")
        assert not any(word in message for word in ("other", "not yours", "teacher"))
    assert session.scalars(select(Material)).all() == []


def test_non_teacher_cannot_use_the_material_service(
    session: Session, student: User, offering_id: uuid.UUID, storage: MemoryStorage
) -> None:
    with pytest.raises(LearningForbiddenError):
        _upload(session, student, offering_id, storage)


# --- lifecycle ------------------------------------------------------------------------


def test_teacher_cannot_publish_and_admin_approval_publishes(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
) -> None:
    created = _upload(session, approved_teacher, offering_id, storage)
    session.commit()

    # No teacher-side transition to published exists: submit only.
    submitted = material_service.submit_material(
        session, approved_teacher, offering_id, created.material_id
    )
    session.commit()
    assert submitted.status == MaterialStatus.PENDING_REVIEW

    # Admin-only publish.
    published = material_service.admin_approve_material(
        session, admin, created.material_id
    )
    session.commit()
    assert published.status == MaterialStatus.PUBLISHED

    moderation = session.scalars(
        select(MaterialModeration).where(
            MaterialModeration.material_id == created.material_id
        )
    ).one()
    assert moderation.decision == "approved"
    assert moderation.reviewer_user_id == admin.id
    assert moderation.reason is None

    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
    ).all()
    material_events = [e.event_type for e in events if e.event_type.startswith("material_")]
    assert "material_approved" in material_events


def test_teacher_is_refused_when_reaching_admin_moderation_directly(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
) -> None:
    created = _upload(session, approved_teacher, offering_id, storage)
    session.commit()
    with pytest.raises(LearningForbiddenError):
        material_service.admin_approve_material(
            session, approved_teacher, created.material_id
        )


def test_admin_reject_records_reason_and_teacher_can_revise_and_resubmit(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
) -> None:
    created = _upload(session, approved_teacher, offering_id, storage)
    session.commit()
    material_service.submit_material(
        session, approved_teacher, offering_id, created.material_id
    )
    session.commit()

    rejected = material_service.admin_reject_material(
        session,
        admin,
        created.material_id,
        MaterialRejectRequest(reason="exercises are incomplete"),
    )
    session.commit()
    assert rejected.status == MaterialStatus.REJECTED

    moderation = session.scalars(
        select(MaterialModeration).where(
            MaterialModeration.material_id == created.material_id
        )
    ).one()
    assert moderation.decision == "rejected"
    assert moderation.reason == "exercises are incomplete"

    revised = material_service.revise_material(
        session, approved_teacher, offering_id, created.material_id
    )
    session.commit()
    assert revised.status == MaterialStatus.DRAFT

    updated = material_service.update_material(
        session,
        approved_teacher,
        offering_id,
        created.material_id,
        MaterialUpdate(title="Algebra worksheet v2"),
    )
    session.commit()
    assert updated.title == "Algebra worksheet v2"

    resubmitted = material_service.submit_material(
        session, approved_teacher, offering_id, created.material_id
    )
    session.commit()
    assert resubmitted.status == MaterialStatus.PENDING_REVIEW


def test_published_material_is_immutable_and_archivable(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
) -> None:
    created = _upload(session, approved_teacher, offering_id, storage)
    session.commit()
    material_service.submit_material(
        session, approved_teacher, offering_id, created.material_id
    )
    session.commit()
    material_service.admin_approve_material(session, admin, created.material_id)
    session.commit()

    with pytest.raises(LearningConflictError):
        material_service.update_material(
            session,
            approved_teacher,
            offering_id,
            created.material_id,
            MaterialUpdate(title="sneaky edit"),
        )
    session.rollback()

    with pytest.raises(LearningConflictError):
        material_service.submit_material(
            session, approved_teacher, offering_id, created.material_id
        )
    session.rollback()

    archived = material_service.archive_material(
        session, approved_teacher, offering_id, created.material_id
    )
    session.commit()
    assert archived.status == MaterialStatus.ARCHIVED

    with pytest.raises(LearningConflictError):
        material_service.delete_material(
            session, approved_teacher, offering_id, created.material_id
        )
    session.rollback()


def test_invalid_transitions_are_conflicts(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
) -> None:
    created = _upload(session, approved_teacher, offering_id, storage)
    session.commit()

    # Draft cannot be revised or archived.
    with pytest.raises(LearningConflictError):
        material_service.revise_material(
            session, approved_teacher, offering_id, created.material_id
        )
    session.rollback()
    with pytest.raises(LearningConflictError):
        material_service.archive_material(
            session, approved_teacher, offering_id, created.material_id
        )
    session.rollback()

    # Draft cannot be approved/rejected by admin (not in the queue).
    with pytest.raises(LearningConflictError):
        material_service.admin_approve_material(session, admin, created.material_id)
    session.rollback()
    with pytest.raises(LearningConflictError):
        material_service.admin_reject_material(
            session,
            admin,
            created.material_id,
            MaterialRejectRequest(reason="too early"),
        )
    session.rollback()


def test_foreign_material_id_404s_like_an_unknown_one(
    session: Session,
    approved_teacher: User,
    other_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
    storage: MemoryStorage,
) -> None:
    created = _upload(session, approved_teacher, offering_id, storage)
    other_material = _upload(
        session, other_teacher, other_offering_id, storage, title="Not mine"
    )
    session.commit()

    unknown = uuid.uuid4()
    for target in (other_material.material_id, unknown):
        with pytest.raises(LearningNotFoundError):
            material_service.get_material(
                session, approved_teacher, offering_id, target
            )
        with pytest.raises(LearningNotFoundError):
            material_service.submit_material(
                session, approved_teacher, offering_id, target
            )


def test_delete_only_for_draft_or_rejected(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
) -> None:
    created = _upload(session, approved_teacher, offering_id, storage)
    session.commit()
    material_service.delete_material(
        session, approved_teacher, offering_id, created.material_id
    )
    session.commit()
    assert session.get(Material, created.material_id) is None

    second = _upload(session, approved_teacher, offering_id, storage, title="Keep me")
    session.commit()
    material_service.submit_material(
        session, approved_teacher, offering_id, second.material_id
    )
    session.commit()
    with pytest.raises(LearningConflictError):
        material_service.delete_material(
            session, approved_teacher, offering_id, second.material_id
        )
    session.rollback()
    assert session.get(Material, second.material_id) is not None


def test_admin_list_defaults_to_the_moderation_queue(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
) -> None:
    draft = _upload(session, approved_teacher, offering_id, storage, title="Draft only")
    pending = _upload(
        session, approved_teacher, offering_id, storage, title="In the queue"
    )
    session.commit()
    material_service.submit_material(
        session, approved_teacher, offering_id, pending.material_id
    )
    session.commit()

    # Unfiltered listing includes every material.
    everything = material_service.admin_list_materials(session, admin)
    assert {m.material_id for m in everything} == {
        draft.material_id,
        pending.material_id,
    }

    queue = material_service.admin_list_materials(
        session, admin, status=MaterialStatus.PENDING_REVIEW
    )
    assert [m.material_id for m in queue] == [pending.material_id]

    detail = material_service.admin_get_material(session, admin, pending.material_id)
    assert detail.status == MaterialStatus.PENDING_REVIEW
    assert detail.moderations == []
