"""Unit tests: student content access + basic material progress (Phase 2, 2C).

In-memory SQLite, no HTTP — the service layer's own contract:

- only an ACTIVE enrollment owned by the caller unlocks its offering's
  published content (unknown / foreign / ended enrollments answer 404);
- only PUBLISHED materials under that enrollment's offering are visible
  (drafts, pending, rejected and archived answer 404);
- cross-teacher isolation: Alice's enrollment cannot reach Brian's
  materials of the same subject (L6);
- teachers cannot use the student surface (403);
- content bytes stream only after the full authorization chain;
- progress is personal, forward-only, idempotent on the current status,
  and never created for unpublished or foreign materials.
"""
from __future__ import annotations

import uuid
from datetime import date

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
    LearningEnrollmentStatus,
    MaterialProgressStatus,
    MaterialStatus,
    MaterialType,
    TeacherVerificationStatus,
    UserRole,
    UserStatus,
)
from app.models.file_asset import FileAsset
from app.models.learning_enrollment import LearningEnrollment
from app.models.material import Material
from app.models.material_progress import MaterialProgress
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.student import Student
from app.models.subject import Subject
from app.models.teacher import Teacher
from app.models.teaching_offering import TeachingOffering
from app.models.topic import Topic
from app.models.user import User
from app.repositories import learning_enrollment_repository as enrollment_repo
from app.repositories import material_progress_repository as progress_repo
from app.repositories import material_repository as material_repo
from app.schemas.learning import TeachingOfferingCreate
from app.schemas.student_content import MaterialProgressUpdate
from app.services import (
    material_service,
    student_content_service,
    teaching_offering_service,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)
from app.services.storage import StorageBackend

PASSWORD = "correct horse battery staple"
_PDF_BYTES = b"%PDF-1.4 fake pdf content"


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
    session: Session, *, verification: str, full_name: str = "Test Teacher"
) -> User:
    user = User(
        email=f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(
            user_id=user.id,
            full_name=full_name,
            verification_status=verification,
        )
    )
    session.commit()
    return user


def _student_user(session: Session, *, full_name: str = "Test Student") -> User:
    user = User(
        email=f"student-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(Student(user_id=user.id, full_name=full_name))
    session.commit()
    return user


def _admin_user(session: Session) -> User:
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


def _publish_material(
    session: Session,
    teacher: User,
    offering: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
    *,
    title: str = "Algebra worksheet",
    material_type: MaterialType = MaterialType.EXERCISE,
) -> uuid.UUID:
    created = material_service.create_material(
        session,
        teacher,
        offering,
        title=title,
        material_type=material_type,
        description="Practice set",
        lesson_id=None,
        file_bytes=_PDF_BYTES,
        original_filename="worksheet.pdf",
        content_type="application/pdf",
        storage=storage,
    )
    session.commit()
    material_service.submit_material(
        session, teacher, offering, created.material_id
    )
    session.commit()
    material_service.admin_approve_material(session, admin, created.material_id)
    session.commit()
    return created.material_id


def _enroll(
    session: Session, student_user: User, offering_id: uuid.UUID
) -> LearningEnrollment:
    student = session.scalar(select(Student).where(Student.user_id == student_user.id))
    offering = session.get(TeachingOffering, offering_id)
    enrollment = enrollment_repo.create(
        session,
        student_id=student.id,
        teaching_offering_id=offering.id,
        learning_context_id=offering.learning_context_id,
    )
    session.commit()
    return enrollment


def _finish_enrollment(session: Session, enrollment: LearningEnrollment) -> None:
    enrollment.status = LearningEnrollmentStatus.ENDED.value
    session.commit()


@pytest.fixture()
def alice(session: Session) -> User:
    return _teacher_user(
        session,
        verification=TeacherVerificationStatus.APPROVED.value,
        full_name="Alice",
    )


@pytest.fixture()
def brian(session: Session) -> User:
    return _teacher_user(
        session,
        verification=TeacherVerificationStatus.APPROVED.value,
        full_name="Brian",
    )


@pytest.fixture()
def david(session: Session) -> User:
    return _student_user(session, full_name="David")


@pytest.fixture()
def admin(session: Session) -> User:
    return _admin_user(session)


@pytest.fixture()
def alice_offering(
    session: Session, catalog: dict, alice: User
) -> uuid.UUID:
    return _offering(session, catalog, alice)


@pytest.fixture()
def brian_offering(
    session: Session, catalog: dict, brian: User
) -> uuid.UUID:
    return _offering(session, catalog, brian)


@pytest.fixture()
def published_material(
    session: Session,
    alice: User,
    alice_offering: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
) -> uuid.UUID:
    return _publish_material(
        session, alice, alice_offering, storage, admin
    )


@pytest.fixture()
def active_enrollment(
    session: Session, david: User, alice_offering: uuid.UUID
) -> LearningEnrollment:
    return _enroll(session, david, alice_offering)


@pytest.fixture()
def brian_material(
    session: Session,
    brian: User,
    brian_offering: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
) -> uuid.UUID:
    return _publish_material(
        session, brian, brian_offering, storage, admin, title="Brian notes"
    )


def _david(session: Session, david: User) -> Student:
    return session.scalar(select(Student).where(Student.user_id == david.id))


# --- overview + navigation -------------------------------------------------------------


def test_overview_reports_counts_for_active_enrollment(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
) -> None:
    overview = student_content_service.get_overview(
        session, david, active_enrollment.id
    )
    assert overview.enrollment_id == active_enrollment.id
    assert overview.teacher_name == "Alice"
    assert overview.published_material_count == 1
    assert overview.topic_count == 0
    assert overview.lesson_count == 0
    assert overview.pathway_code == "O_LEVEL"
    assert overview.subject_code == "MATH"


def test_unknown_and_foreign_and_ended_enrollments_answer_404(
    session: Session,
    david: User,
    alice: User,
    alice_offering: uuid.UUID,
    active_enrollment: LearningEnrollment,
) -> None:
    other_student = _student_user(session, full_name="Other")
    foreign = _enroll(session, other_student, alice_offering)
    _finish_enrollment(session, active_enrollment)

    for enrollment_id in (uuid.uuid4(), foreign.id, active_enrollment.id):
        with pytest.raises(LearningNotFoundError) as excinfo:
            student_content_service.get_overview(session, david, enrollment_id)
        assert excinfo.value.status_code == 404


def test_teacher_is_refused_on_student_content_surface(
    session: Session,
    alice: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
) -> None:
    with pytest.raises(LearningForbiddenError):
        student_content_service.get_overview(session, alice, active_enrollment.id)
    with pytest.raises(LearningForbiddenError):
        student_content_service.get_material(
            session, alice, active_enrollment.id, published_material
        )
    with pytest.raises(LearningForbiddenError):
        student_content_service.set_progress(
            session,
            alice,
            active_enrollment.id,
            published_material,
            MaterialProgressUpdate(status=MaterialProgressStatus.IN_PROGRESS),
        )


def test_topics_and_lessons_are_offering_structure(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
) -> None:
    topic = Topic(
        teaching_offering_id=active_enrollment.teaching_offering_id,
        title="Linear equations",
        description="Solving for x",
        display_order=1,
    )
    session.add(topic)
    session.flush()
    session.add(
        Topic(
            teaching_offering_id=active_enrollment.teaching_offering_id,
            title="Systems",
            display_order=2,
        )
    )
    from app.models.lesson import Lesson

    session.add(
        Lesson(topic_id=topic.id, title="One unknown", display_order=1)
    )
    session.commit()

    topics = student_content_service.list_topics(
        session, david, active_enrollment.id
    )
    assert [t.title for t in topics] == ["Linear equations", "Systems"]
    lessons = student_content_service.list_lessons(
        session, david, active_enrollment.id
    )
    assert [l.title for l in lessons] == ["One unknown"]
    assert lessons[0].topic_id == topic.id


# --- material access -------------------------------------------------------------------


def test_enrolled_student_reads_published_material(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
) -> None:
    read = student_content_service.get_material(
        session, david, active_enrollment.id, published_material
    )
    assert read.material_id == published_material
    assert read.status == MaterialStatus.PUBLISHED.value
    assert read.material_type == MaterialType.EXERCISE
    assert read.file.original_filename == "worksheet.pdf"
    assert read.progress is None or read.progress.status != MaterialProgressStatus.COMPLETED
    assert str(active_enrollment.id) in read.content_url
    assert str(published_material) in read.content_url
    # Never a storage path or absolute URL.
    assert "file_assets/" not in read.content_url
    assert not read.content_url.startswith("http")


def test_list_materials_returns_only_published_for_offering(
    session: Session,
    david: User,
    alice: User,
    alice_offering: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
    brian_material: uuid.UUID,
) -> None:
    draft = material_service.create_material(
        session,
        alice,
        alice_offering,
        title="Draft notes",
        material_type=MaterialType.NOTE,
        description=None,
        lesson_id=None,
        file_bytes=_PDF_BYTES,
        original_filename="draft.pdf",
        content_type="application/pdf",
        storage=storage,
    )
    session.commit()
    submitted = material_service.submit_material(
        session, alice, alice_offering, draft.material_id
    )
    session.commit()
    assert submitted.status == MaterialStatus.PENDING_REVIEW.value

    listed = student_content_service.list_materials(
        session, david, active_enrollment.id
    )
    ids = {m.material_id for m in listed}
    assert published_material in ids
    assert draft.material_id not in ids
    assert brian_material not in ids


def test_pending_and_rejected_materials_are_invisible(
    session: Session,
    alice: User,
    alice_offering: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
    david: User,
    active_enrollment: LearningEnrollment,
) -> None:
    pending = material_service.create_material(
        session,
        alice,
        alice_offering,
        title="Pending work",
        material_type=MaterialType.BOOK,
        description=None,
        lesson_id=None,
        file_bytes=_PDF_BYTES,
        original_filename="pending.pdf",
        content_type="application/pdf",
        storage=storage,
    )
    session.commit()
    material_service.submit_material(
        session, alice, alice_offering, pending.material_id
    )
    session.commit()

    rejected = material_service.create_material(
        session,
        alice,
        alice_offering,
        title="Rejected work",
        material_type=MaterialType.BOOK,
        description=None,
        lesson_id=None,
        file_bytes=_PDF_BYTES,
        original_filename="rejected.pdf",
        content_type="application/pdf",
        storage=storage,
    )
    session.commit()
    material_service.submit_material(
        session, alice, alice_offering, rejected.material_id
    )
    session.commit()
    from app.schemas.material import MaterialRejectRequest

    material_service.admin_reject_material(
        session, admin, rejected.material_id,
        MaterialRejectRequest(reason="not ready"),
    )
    session.commit()

    for material_id in (pending.material_id, rejected.material_id):
        with pytest.raises(LearningNotFoundError) as excinfo:
            student_content_service.get_material(
                session, david, active_enrollment.id, material_id
            )
        assert str(material_id) in str(excinfo.value)


def test_cross_teacher_materials_are_404_for_enrolled_student(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
    brian_material: uuid.UUID,
) -> None:
    """David is enrolled in Alice's offering; Brian's material is invisible.

    Same subject, different offering/teacher — the id must answer exactly
    like an unknown material (L6), never 403 that would confirm existence.
    """
    with pytest.raises(LearningNotFoundError) as excinfo:
        student_content_service.get_material(
            session, david, active_enrollment.id, brian_material
        )
    assert excinfo.value.status_code == 404
    assert "no material" in str(excinfo.value).lower()

    with pytest.raises(LearningNotFoundError):
        student_content_service.get_material_content(
            session, david, active_enrollment.id, brian_material,
            storage=MemoryStorage(),
        )


def test_non_enrolled_student_cannot_read_material(
    session: Session,
    storage: MemoryStorage,
    admin: User,
    alice: User,
    alice_offering: uuid.UUID,
    published_material: uuid.UUID,
) -> None:
    stranger = _student_user(session, full_name="Stranger")
    # No enrollment at all — try a random enrollment id shape and a real
    # enrollment id that does not belong to them via a second enrollment.
    other = _student_user(session, full_name="Other")
    foreign = _enroll(session, other, alice_offering)
    _finish_enrollment(session, foreign)

    # Stranger has no enrollment; enrollment id is foreign -> 404.
    with pytest.raises(LearningNotFoundError):
        student_content_service.get_material(
            session, stranger, foreign.id, published_material
        )


def test_ended_enrollment_loses_content_access(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
    storage: MemoryStorage,
) -> None:
    # Prove access first.
    assert student_content_service.get_material(
        session, david, active_enrollment.id, published_material
    ).material_id == published_material

    _finish_enrollment(session, active_enrollment)
    with pytest.raises(LearningNotFoundError):
        student_content_service.get_material(
            session, david, active_enrollment.id, published_material
        )
    with pytest.raises(LearningNotFoundError):
        student_content_service.get_material_content(
            session, david, active_enrollment.id, published_material,
            storage=storage,
        )


def test_content_bytes_stream_after_authorization(
    session: Session,
    storage: MemoryStorage,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
) -> None:
    data, content_type, filename = student_content_service.get_material_content(
        session,
        david,
        active_enrollment.id,
        published_material,
        storage=storage,
    )
    assert data == _PDF_BYTES
    assert content_type == "application/pdf"
    assert filename == "worksheet.pdf"
    # Opaque key never leaves the service.
    assert not any(key in str(filename) for key in storage.objects)


# --- basic progress ---------------------------------------------------------------------


def test_progress_absent_reads_as_not_started_without_writing(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
) -> None:
    read = student_content_service.get_progress(
        session, david, active_enrollment.id, published_material
    )
    assert read.status == MaterialProgressStatus.NOT_STARTED
    assert read.progress_id is None
    assert session.scalars(select(MaterialProgress)).all() == []


def test_progress_is_personal_and_forward_only(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
) -> None:
    student = _david(session, david)

    created = student_content_service.set_progress(
        session,
        david,
        active_enrollment.id,
        published_material,
        MaterialProgressUpdate(status=MaterialProgressStatus.IN_PROGRESS),
    )
    session.commit()
    assert created.status == MaterialProgressStatus.IN_PROGRESS
    assert created.student_id == student.id
    assert created.material_id == published_material
    assert created.started_at is not None
    assert created.completed_at is None
    assert created.progress_id is not None

    # Idempotent re-send of the same status.
    again = student_content_service.set_progress(
        session,
        david,
        active_enrollment.id,
        published_material,
        MaterialProgressUpdate(status=MaterialProgressStatus.IN_PROGRESS),
    )
    session.commit()
    assert again.progress_id == created.progress_id
    assert again.status == MaterialProgressStatus.IN_PROGRESS
    assert len(session.scalars(select(MaterialProgress)).all()) == 1

    # Forward move to completed.
    completed = student_content_service.set_progress(
        session,
        david,
        active_enrollment.id,
        published_material,
        MaterialProgressUpdate(status=MaterialProgressStatus.COMPLETED),
    )
    session.commit()
    assert completed.status == MaterialProgressStatus.COMPLETED
    assert completed.completed_at is not None
    assert completed.started_at is not None
    assert len(session.scalars(select(MaterialProgress)).all()) == 1

    # Completed stays completed (idempotent).
    still = student_content_service.set_progress(
        session,
        david,
        active_enrollment.id,
        published_material,
        MaterialProgressUpdate(status=MaterialProgressStatus.COMPLETED),
    )
    session.commit()
    assert still.status == MaterialProgressStatus.COMPLETED

    # Backwards move is refused.
    with pytest.raises(LearningConflictError) as excinfo:
        student_content_service.set_progress(
            session,
            david,
            active_enrollment.id,
            published_material,
            MaterialProgressUpdate(status=MaterialProgressStatus.IN_PROGRESS),
        )
    assert excinfo.value.status_code == 409

    # History stays with this material row (audit trail). Idempotent
    # re-sends deliberately write no extra audit event.
    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == student.user_id)
    ).all()
    progress_events = [
        e.event_type for e in events if e.event_type.startswith("material_progress")
    ]
    assert progress_events == [
        "material_progress_created",
        "material_progress_updated",
    ]


def test_progress_cannot_be_created_for_unpublished_or_foreign_material(
    session: Session,
    alice: User,
    alice_offering: uuid.UUID,
    storage: MemoryStorage,
    admin: User,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
    brian_material: uuid.UUID,
) -> None:
    draft = material_service.create_material(
        session,
        alice,
        alice_offering,
        title="Draft progress target",
        material_type=MaterialType.NOTE,
        description=None,
        lesson_id=None,
        file_bytes=_PDF_BYTES,
        original_filename="draft2.pdf",
        content_type="application/pdf",
        storage=storage,
    )
    session.commit()

    for material_id in (draft.material_id, brian_material):
        with pytest.raises(LearningNotFoundError):
            student_content_service.set_progress(
                session,
                david,
                active_enrollment.id,
                material_id,
                MaterialProgressUpdate(status=MaterialProgressStatus.IN_PROGRESS),
            )
    assert session.scalars(select(MaterialProgress)).all() == []


def test_non_writable_progress_status_is_409(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
) -> None:
    with pytest.raises(LearningConflictError) as excinfo:
        student_content_service.set_progress(
            session,
            david,
            active_enrollment.id,
            published_material,
            MaterialProgressUpdate(status=MaterialProgressStatus.NOT_STARTED),
        )
    assert excinfo.value.status_code == 409


def test_unique_index_blocks_duplicate_progress_rows(
    session: Session,
    david: User,
    active_enrollment: LearningEnrollment,
    published_material: uuid.UUID,
) -> None:
    student = _david(session, david)
    progress_repo.create(
        session,
        student_id=student.id,
        material_id=published_material,
        status=MaterialProgressStatus.IN_PROGRESS.value,
    )
    session.commit()
    with pytest.raises(Exception):  # unique index, not a service-level guess
        progress_repo.create(
            session,
            student_id=student.id,
            material_id=published_material,
            status=MaterialProgressStatus.COMPLETED.value,
        )
    session.rollback()


def test_progress_does_not_transfer_across_teachers(
    session: Session,
    david: User,
    storage: MemoryStorage,
    admin: User,
    alice: User,
    alice_offering: uuid.UUID,
    published_material: uuid.UUID,
    brian: User,
    brian_offering: uuid.UUID,
) -> None:
    """David completes Alice's material, then joins Brian's offering.

    Brian's material of the same subject starts at not_started -- progress
    is keyed to the material row it was earned on.
    """
    enrollment = _enroll(session, david, alice_offering)
    session.commit()
    student_content_service.set_progress(
        session,
        david,
        enrollment.id,
        published_material,
        MaterialProgressUpdate(status=MaterialProgressStatus.COMPLETED),
    )
    session.commit()

    # Leave Alice, join Brian.
    enrollment.status = LearningEnrollmentStatus.ENDED.value
    session.commit()
    brian_enrollment = _enroll(session, david, brian_offering)
    session.commit()

    brian_material_id = _publish_material(
        session, brian, brian_offering, storage, admin, title="Brian same subject"
    )
    read = student_content_service.get_material(
        session, david, brian_enrollment.id, brian_material_id
    )
    assert (
        read.progress is None
        or read.progress.status == MaterialProgressStatus.NOT_STARTED
    )

    # Alice's published material is no longer reachable (ended enrollment).
    with pytest.raises(LearningNotFoundError):
        student_content_service.get_material(
            session, david, enrollment.id, published_material
        )


def test_material_progress_model_check_constraint_values() -> None:
    assert MaterialProgressStatus.NOT_STARTED.value == "not_started"
    assert MaterialProgressStatus.IN_PROGRESS.value == "in_progress"
    assert MaterialProgressStatus.COMPLETED.value == "completed"
