"""Unit tests: administrator book intake service (book P1).

In-memory SQLite + an in-memory storage backend, no HTTP — the service
contract:

- staging streams the upload to a bounded temp file, proving filename,
  ``application/pdf``, the ``%PDF-`` signature, the byte ceiling and the
  real ``pypdf`` structure before a single row exists;
- intake is administrator-only; a direct call by a teacher or student is
  refused with the shared 403 error;
- a book is created together with edition 1 (``uploaded``), its stored
  ``file_assets`` row and exactly one canonical ``learning_contexts``
  association per requested context — unknown ids 404, zero ids 422, and
  neither leaves a half-created book behind;
- success writes the ``book_uploaded`` audit event; an intake refusal
  writes ``book_validation_failed`` with a stable reason code and no
  business rows;
- every failure path removes the stored bytes (compensation), so a
  refused or broken request can never strand an orphaned PDF.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers, UploadFile

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.book import Book
from app.models.book_revision import BookRevision
from app.models.book_revision_context import BookRevisionContext
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    BookRevisionStatus,
    FileAssetStatus,
    UserRole,
    UserStatus,
)
from app.models.file_asset import FileAsset
from app.models.learning_context import LearningContext
from app.models.pathway import Pathway
from app.models.subject import Subject
from app.models.user import User
from app.services import book_service
from app.services.book_validation import BookUploadError
from app.services.learning_context_service import (
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)
from app.services.storage import StorageBackend
from tests.conftest import build_minimal_pdf

PASSWORD = "correct horse battery staple"


class MemoryStorage(StorageBackend):
    """In-memory backend so unit tests never touch the real filesystem."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def save(self, storage_key: str, data: bytes, *, content_type: str) -> None:
        self.objects[storage_key] = data

    def save_file(
        self, storage_key: str, source_path, *, content_type: str
    ) -> None:
        self.objects[storage_key] = Path(source_path).read_bytes()

    def read(self, storage_key: str) -> bytes:
        return self.objects[storage_key]

    def delete(self, storage_key: str) -> None:
        self.objects.pop(storage_key, None)

    def exists(self, storage_key: str) -> bool:
        return storage_key in self.objects


class ExplodingStorage(MemoryStorage):
    """A backend that refuses the write (storage failure path)."""

    def save_file(self, storage_key, source_path, *, content_type: str) -> None:
        raise OSError("disk on fire")


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


def _user(session: Session, role: str) -> User:
    user = User(
        email=f"{role}-{uuid.uuid4().hex[:8]}@books.example.com",
        role=role,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def admin(session: Session) -> User:
    return _user(session, UserRole.ADMIN.value)


@pytest.fixture()
def teacher(session: Session) -> User:
    return _user(session, UserRole.TEACHER.value)


@pytest.fixture()
def student(session: Session) -> User:
    return _user(session, UserRole.STUDENT.value)


def _learning_context(session: Session) -> LearningContext:
    """A minimal real context row (catalog FKs point at real rows)."""
    year = session.scalar(select(AcademicYear).limit(1))
    if year is None:
        year = AcademicYear(
            name="2026/2027",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 6, 30),
            status=AcademicYearStatus.ACTIVE.value,
        )
        session.add(year)
        session.flush()
    pathway = Pathway(code=f"P{uuid.uuid4().hex[:6].upper()}", name="O Level")
    level = EducationLevel(code=f"L{uuid.uuid4().hex[:6].upper()}", name="Senior 3", level_number=3)
    subject = Subject(code=f"S{uuid.uuid4().hex[:6].upper()}", name="Mathematics")
    session.add_all([year, pathway, level, subject])
    session.flush()
    row = LearningContext(
        academic_year_id=year.id,
        pathway_id=pathway.id,
        education_level_id=level.id,
        subject_id=subject.id,
        program_version_id=None,
    )
    session.add(row)
    session.flush()
    session.commit()
    return row


@pytest.fixture()
def context(session: Session) -> LearningContext:
    return _learning_context(session)


# --- staging -----------------------------------------------------------------------


def _stage(
    data: bytes,
    *,
    filename: str = "textbook.pdf",
    content_type: str = "application/pdf",
) -> book_service.StagedBookUpload:
    upload = UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )
    return asyncio.run(book_service.stage_book_upload(upload))


def _stage_fails(data: bytes, **kwargs) -> BookUploadError:
    with pytest.raises(BookUploadError) as excinfo:
        _stage(data, **kwargs)
    return excinfo.value


def test_staging_records_size_checksum_and_page_count() -> None:
    data = build_minimal_pdf(pages=2)
    staged = _stage(data)
    try:
        assert staged.path.exists()
        assert staged.size_bytes == len(data)
        assert staged.checksum_sha256 == hashlib.sha256(data).hexdigest()
        assert staged.page_count == 2
        assert staged.content_type == "application/pdf"
        assert staged.original_filename == "textbook.pdf"
        assert staged.path.read_bytes() == data
    finally:
        staged.cleanup()
    assert not staged.path.exists()


def test_staging_cleans_up_after_every_refusal() -> None:
    import tempfile

    def _leftovers() -> set[Path]:
        return set(Path(tempfile.gettempdir()).glob("st-book-*"))

    before = _leftovers()
    _stage_fails(b"not a pdf at all")  # magic mismatch
    _stage_fails(build_minimal_pdf(), content_type="image/png")  # envelope
    _stage_fails(b"", filename="empty.pdf")  # empty payload
    _stage_fails(build_minimal_pdf(None))  # image-only PDF
    assert _leftovers() == before


def test_the_byte_ceiling_refuses_oversized_uploads_mid_stream(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "BOOK_MAX_FILE_BYTES", 64)
    error = _stage_fails(build_minimal_pdf())
    assert error.reason == "file_too_large"


# --- happy path ---------------------------------------------------------------------


def _upload(
    session: Session,
    user: User,
    context: LearningContext,
    storage: MemoryStorage,
    *,
    context_ids: list[uuid.UUID] | None = None,
    title: str = "Integrated Science S3",
    description: str | None = "Textbook for Senior 3",
    data: bytes | None = None,
    staged: book_service.StagedBookUpload | None = None,
):
    own_staged = staged or _stage(data if data is not None else build_minimal_pdf())
    try:
        return book_service.create_book_upload(
            session,
            user,
            staged=own_staged,
            title=title,
            description=description,
            learning_context_ids=context_ids
            if context_ids is not None
            else [context.id],
            storage=storage,
        )
    finally:
        own_staged.cleanup()


def test_an_administrator_upload_creates_book_revision_asset_and_context(
    session: Session, admin: User, context: LearningContext, storage: MemoryStorage
) -> None:
    data = build_minimal_pdf()
    read = _upload(session, admin, context, storage, data=data)

    assert read.title == "Integrated Science S3"
    assert read.description == "Textbook for Senior 3"
    assert read.created_by_user_id == admin.id
    assert read.learning_context_ids == [context.id]
    assert read.revision.revision_number == 1
    assert read.revision.status == BookRevisionStatus.UPLOADED
    assert read.revision.uploaded_by_user_id == admin.id

    # Rows on disk-side: one book, one edition 1, one context link, one asset.
    assert session.query(Book).count() == 1
    revision = session.scalar(select(BookRevision))
    assert revision.status == BookRevisionStatus.UPLOADED.value
    assert revision.revision_number == 1
    assert session.query(BookRevisionContext).count() == 1

    asset = session.scalar(select(FileAsset))
    assert asset.validation_status == FileAssetStatus.STORED.value
    assert asset.storage_key == f"file_assets/{asset.id}"
    assert asset.content_type == "application/pdf"
    assert asset.checksum_sha256 == hashlib.sha256(data).hexdigest()

    # The bytes really reached the backend under the opaque key.
    assert set(storage.objects) == {asset.storage_key}
    assert storage.objects[asset.storage_key] == data

    # The response is metadata only: never a storage path.
    dumped = read.model_dump(mode="json")
    assert "storage_key" not in str(dumped)
    assert dumped["revision"]["file_asset"]["checksum_sha256"] == asset.checksum_sha256


def test_a_successful_upload_is_audited_with_the_acting_administrator(
    session: Session, admin: User, context: LearningContext, storage: MemoryStorage
) -> None:
    read = _upload(session, admin, context, storage)
    session.flush()

    events = session.scalars(
        select(AuthEvent).where(AuthEvent.event_type == "book_uploaded")
    ).all()
    assert len(events) == 1
    event = events[0]
    assert event.user_id == admin.id
    assert event.actor_user_id == admin.id
    assert str(read.book_id) in (event.metadata_json or "")
    assert str(read.revision.file_asset.file_asset_id) in (event.metadata_json or "")
    # Fixed vocabulary only, and short enough for auth_events.event_type.
    assert len(event.event_type) <= 32


def test_several_canonical_contexts_attach_to_one_revision(
    session: Session, admin: User, context: LearningContext, storage: MemoryStorage
) -> None:
    other = _learning_context(session)
    read = _upload(
        session, admin, context, storage, context_ids=[context.id, other.id]
    )
    assert sorted(read.learning_context_ids) == sorted([context.id, other.id])
    assert session.query(BookRevisionContext).count() == 2


def test_duplicate_context_ids_collapse_into_one_association(
    session: Session, admin: User, context: LearningContext, storage: MemoryStorage
) -> None:
    read = _upload(
        session, admin, context, storage, context_ids=[context.id, context.id]
    )
    assert read.learning_context_ids == [context.id]
    assert session.query(BookRevisionContext).count() == 1


# --- authorization and validation failures ------------------------------------------


@pytest.mark.parametrize("fixture", ["teacher", "student"])
def test_only_an_administrator_may_create_a_book(
    session: Session,
    context: LearningContext,
    storage: MemoryStorage,
    fixture: str,
    request: pytest.FixtureRequest,
) -> None:
    caller = request.getfixturevalue(fixture)
    with pytest.raises(LearningForbiddenError):
        _upload(session, caller, context, storage)
    assert session.query(Book).count() == 0
    assert session.query(FileAsset).count() == 0
    assert storage.objects == {}


def test_an_unknown_learning_context_404s_and_creates_nothing(
    session: Session, admin: User, context: LearningContext, storage: MemoryStorage
) -> None:
    missing = uuid.uuid4()
    with pytest.raises(LearningNotFoundError):
        _upload(session, admin, context, storage, context_ids=[context.id, missing])
    session.rollback()
    assert session.query(Book).count() == 0
    assert session.query(BookRevision).count() == 0
    assert session.query(FileAsset).count() == 0
    assert storage.objects == {}


def test_zero_learning_contexts_is_refused(
    session: Session, admin: User, context: LearningContext, storage: MemoryStorage
) -> None:
    with pytest.raises(LearningValidationError):
        _upload(session, admin, context, storage, context_ids=[])
    session.rollback()
    assert session.query(Book).count() == 0
    assert storage.objects == {}


def test_a_storage_failure_is_a_422_and_leaves_no_rows(
    session: Session, admin: User, context: LearningContext
) -> None:
    broken = ExplodingStorage()
    with pytest.raises(BookUploadError) as excinfo:
        _upload(session, admin, context, broken)
    assert excinfo.value.reason == "storage_failed"
    session.rollback()
    assert session.query(Book).count() == 0
    assert session.query(FileAsset).count() == 0
    assert broken.objects == {}


def test_a_database_failure_after_the_write_deletes_the_stored_bytes(
    session: Session,
    admin: User,
    context: LearningContext,
    storage: MemoryStorage,
    monkeypatch,
) -> None:
    from app.services import book_service as service_module

    def _explode(*args, **kwargs):
        raise RuntimeError("row write failed")

    monkeypatch.setattr(service_module.book_repo, "create_book", _explode)
    with pytest.raises(RuntimeError):
        _upload(session, admin, context, storage)
    session.rollback()
    assert storage.objects == {}  # compensation removed the orphaned PDF
    assert session.query(FileAsset).count() == 0


def test_delete_stored_asset_removes_the_bytes(
    storage: MemoryStorage,
) -> None:
    asset_id = uuid.uuid4()
    storage.save(f"file_assets/{asset_id}", b"bytes", content_type="application/pdf")
    book_service.delete_stored_asset(asset_id, storage=storage)
    assert storage.objects == {}


# --- intake refusal audit -------------------------------------------------------------


def test_a_validation_failure_is_audited_without_business_rows(
    session: Session, admin: User
) -> None:
    book_service.log_book_validation_failed(
        session,
        admin,
        reason="encrypted_pdf",
        original_filename="locked.pdf",
        declared_content_type="application/pdf",
    )
    session.commit()

    event = session.scalar(
        select(AuthEvent).where(AuthEvent.event_type == "book_validation_failed")
    )
    assert event is not None
    assert event.user_id == admin.id
    assert event.actor_user_id == admin.id
    assert "encrypted_pdf" in (event.metadata_json or "")
    assert "locked.pdf" in (event.metadata_json or "")
    assert session.query(Book).count() == 0
    assert session.query(FileAsset).count() == 0
    assert len(event.event_type) <= 32


# --- reads ---------------------------------------------------------------------------


def test_library_reads_are_admin_only_newest_first_and_bounded(
    session: Session, admin: User, teacher: User, context: LearningContext, storage: MemoryStorage
) -> None:
    with pytest.raises(LearningForbiddenError):
        book_service.list_books(session, teacher)
    with pytest.raises(LearningForbiddenError):
        book_service.get_book(session, teacher, uuid.uuid4())

    first = _upload(session, admin, context, storage, title="First book")
    second = _upload(session, admin, context, storage, title="Second book")

    # SQLite's ``now()`` only resolves seconds, so the two inserts can tie;
    # give them distinct timestamps to assert the actual ORDER BY clause
    # (PostgreSQL resolves microseconds and keeps them apart naturally).
    from datetime import datetime, timezone

    session.get(Book, first.book_id).created_at = datetime(
        2026, 1, 1, tzinfo=timezone.utc
    )
    session.get(Book, second.book_id).created_at = datetime(
        2026, 1, 2, tzinfo=timezone.utc
    )
    session.flush()

    listed = book_service.list_books(session, admin, limit=10, offset=0)
    assert [b.title for b in listed] == ["Second book", "First book"]
    assert [b.book_id for b in listed] == [second.book_id, first.book_id]

    page = book_service.list_books(session, admin, limit=1, offset=1)
    assert [b.book_id for b in page] == [first.book_id]

    detail = book_service.get_book(session, admin, first.book_id)
    assert detail.learning_context_ids == [context.id]
    assert detail.revision.file_asset.original_filename == "textbook.pdf"


def test_an_unknown_book_id_is_the_same_404_as_any_other(
    session: Session, admin: User
) -> None:
    with pytest.raises(LearningNotFoundError):
        book_service.get_book(session, admin, uuid.uuid4())


def test_the_first_revision_of_a_second_book_is_always_edition_one(
    session: Session, admin: User, context: LearningContext, storage: MemoryStorage
) -> None:
    """Every P1 upload starts a NEW book at revision 1 — no shared editions."""
    _upload(session, admin, context, storage, title="Book A")
    _upload(session, admin, context, storage, title="Book B")
    numbers = sorted(
        revision.revision_number
        for revision in session.scalars(select(BookRevision))
    )
    assert numbers == [1, 1]
    assert session.query(Book).count() == 2
