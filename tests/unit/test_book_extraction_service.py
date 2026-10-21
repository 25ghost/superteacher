"""Unit tests: extraction orchestration and the background sweep (book P2).

In-memory SQLite + an in-memory storage backend, no HTTP — the durable
half of the pipeline:

- **claim → extract → persist**: the oldest ``uploaded`` revision is
  claimed, parsed and rewritten as a ``book_units`` tree in ONE
  transaction that also flips the status to ``pending_review``, stores
  the extraction summary and writes the ``book_extract_started`` /
  ``book_extract_completed`` audit pair;
- **honest failures**: a document problem (``ExtractionError``) commits
  ``extraction_failed`` with the fixed ``code: message`` and the failed
  audit event, and leaves NO unit hierarchy behind;
- **transient failures**: an unexpected exception (storage outage)
  propagates without committing — the revision stays ``uploaded`` with
  no committed audit rows, so the next sweep retries it;
- **idempotency**: re-running a pass (retry, or a crash that reached
  write twice) deletes the previous tree before rewriting it — sibling
  order indexes would reject a duplicate, and units never double;
- **retry**: ``retry_book_extraction`` is administrator-only, accepts
  only ``extraction_failed`` (anything else is a conflict, an unknown
  book a not-found), flips back to ``uploaded`` and audits who asked;
- **the sweeper**: same lifecycle contract as the class-end sweeper —
  immediate startup pass (restart recovery), fresh short-lived session
  per pass, a failing pass never kills the thread, stop joins it, and
  the application lifespan wires start/stop.

PostgreSQL ``SELECT ... FOR UPDATE`` concurrency (two workers claiming
the same revision) is covered by the integration suite, where real row
locks exist.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.book import Book
from app.models.book_revision import BookRevision
from app.models.book_unit import BookUnit
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    AssessmentRequirement,
    BookRevisionStatus,
    BookUnitKind,
    FileAssetStatus,
    UserRole,
    UserStatus,
)
from app.models.file_asset import FileAsset
from app.models.learning_context import LearningContext
from app.models.pathway import Pathway
from app.models.subject import Subject
from app.models.user import User
from app.services import book_extraction_service
from app.services import book_extraction_sweeper as sweeper_module
from app.services.book_extraction import (
    ERROR_NO_TEXT,
    WARNING_NO_OUTLINE,
    extract_book_pdf,
)
from app.services.book_extraction_service import (
    EVENT_EXTRACT_COMPLETED,
    EVENT_EXTRACT_FAILED,
    EVENT_EXTRACT_RETRY,
    EVENT_EXTRACT_STARTED,
    extract_due_revisions,
    retry_book_extraction,
)
from app.services.book_extraction_sweeper import BookExtractionSweeper
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)
from app.services.storage import StorageBackend, StorageError
from tests.conftest import build_minimal_pdf, build_multiline_pdf

PASSWORD = "correct horse battery staple"


class MemoryStorage(StorageBackend):
    """In-memory backend so unit tests never touch the real filesystem."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def save(self, storage_key: str, data: bytes, *, content_type: str) -> None:
        self.objects[storage_key] = data

    def save_file(self, storage_key: str, source_path, *, content_type: str) -> None:
        self.objects[storage_key] = Path(source_path).read_bytes()

    def read(self, storage_key: str) -> bytes:
        return self.objects[storage_key]

    def delete(self, storage_key: str) -> None:
        self.objects.pop(storage_key, None)

    def exists(self, storage_key: str) -> bool:
        return storage_key in self.objects


class OutageStorage(MemoryStorage):
    """A backend whose read fails (transient storage outage)."""

    def read(self, storage_key: str) -> bytes:
        raise StorageError("storage node unreachable")


# --- fixtures --------------------------------------------------------------------------


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


@pytest.fixture()
def teacher(session: Session) -> User:
    user = User(
        email=f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.commit()
    return user


def _seed_book(session: Session, admin: User, pdf: bytes, *, storage: MemoryStorage):
    """One book + edition 1 (``uploaded``) + its stored PDF, committed.

    The catalog row is reused across calls (one academic year name per
    database), so a test can seed several books in one session.
    """
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
    pathway = session.scalar(select(Pathway).limit(1))
    level = session.scalar(select(EducationLevel).limit(1))
    subject = session.scalar(select(Subject).limit(1))
    if pathway is None or level is None or subject is None:
        pathway = pathway or Pathway(code="BP", name="O Level")
        level = level or EducationLevel(code="BL", name="Senior 3", level_number=3)
        subject = subject or Subject(code="BS", name="Mathematics")
        session.add_all([pathway, level, subject])
        session.flush()
    context = session.scalar(select(LearningContext).limit(1))
    if context is None:
        context = LearningContext(
            academic_year_id=year.id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            subject_id=subject.id,
            program_version_id=None,
        )
        session.add(context)
        session.flush()

    asset = FileAsset(
        uploaded_by_user_id=admin.id,
        original_filename="textbook.pdf",
        content_type="application/pdf",
        size_bytes=len(pdf),
        checksum_sha256="deadbeef",
        storage_key="pending",
        validation_status=FileAssetStatus.STORED.value,
    )
    session.add(asset)
    session.flush()
    asset.storage_key = f"file_assets/{asset.id}"
    storage.objects[asset.storage_key] = pdf

    book = Book(title="Science S3", created_by_user_id=admin.id)
    session.add(book)
    session.flush()
    revision = BookRevision(
        book_id=book.id,
        revision_number=1,
        file_asset_id=asset.id,
        uploaded_by_user_id=admin.id,
        status=BookRevisionStatus.UPLOADED.value,
    )
    session.add(revision)
    session.flush()
    from app.models.book_revision_context import BookRevisionContext

    session.add(
        BookRevisionContext(
            book_revision_id=revision.id, learning_context_id=context.id
        )
    )
    session.commit()
    return book, revision


def _science_pdf() -> bytes:
    return build_multiline_pdf(
        [
            ["Chapter 1 Introduction", "Science studies the world."],
            ["1.1 Water Cycle", "Water evaporates.", "Exercises", "1. Solve it."],
        ]
    )


def _row(session: Session, revision_id) -> BookRevision:
    session.expire_all()
    return session.get(BookRevision, revision_id)


def _units(session: Session, revision_id) -> list[BookUnit]:
    return list(
        session.scalars(
            select(BookUnit)
            .where(BookUnit.book_revision_id == revision_id)
            .order_by(BookUnit.created_at, BookUnit.display_order)
        )
    )


def _events(session: Session, event_type: str) -> list[AuthEvent]:
    return list(
        session.scalars(
            select(AuthEvent).where(AuthEvent.event_type == event_type)
        )
    )


# --- the happy path ---------------------------------------------------------------------


def test_an_uploaded_revision_is_extracted_to_pending_review(
    session: Session, admin: User
) -> None:
    storage = MemoryStorage()
    book, revision = _seed_book(session, admin, _science_pdf(), storage=storage)

    processed = extract_due_revisions(session, limit=10, storage=storage)

    assert processed == 1
    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.PENDING_REVIEW.value
    assert row.extraction_error is None
    summary = json.loads(row.extraction_summary)
    assert summary["page_count"] == 2
    assert summary["unit_count"] == len(_units(session, revision.id))
    assert summary["by_kind"]["chapter"] == 1
    assert summary["by_kind"]["topic"] == 1
    assert summary["by_kind"]["exercise_set"] == 1
    assert WARNING_NO_OUTLINE in summary["warnings"]

    # The unit tree: chapters from printed headings, topics nested, leaves
    # carved, every row traceable to its source pages.
    units = _units(session, revision.id)
    roots = [u for u in units if u.parent_unit_id is None]
    assert [u.kind for u in roots] == ["chapter"]
    assert roots[0].title == "Chapter 1 Introduction"
    water = next(u for u in units if u.title == "1.1 Water Cycle")
    assert water.kind == BookUnitKind.TOPIC.value
    assert water.parent_unit_id == roots[0].id
    assert water.source_page_start == 2 and water.source_page_end == 2
    exercises = next(u for u in units if u.title == "Exercises")
    assert exercises.kind == BookUnitKind.EXERCISE_SET.value
    assert exercises.parent_unit_id == water.id
    # Sibling order is 1..n per parent.
    assert sorted(
        u.display_order for u in units if u.parent_unit_id == roots[0].id
    ) == [1]
    assert all(
        u.assessment_requirement == AssessmentRequirement.UNRESOLVED.value
        for u in units
    )

    # Audit: exactly one started + one completed, both about the revision.
    started = _events(session, EVENT_EXTRACT_STARTED)
    completed = _events(session, EVENT_EXTRACT_COMPLETED)
    assert len(started) == 1 and len(completed) == 1
    assert started[0].user_id == admin.id and started[0].actor_user_id is None
    assert json.loads(completed[0].metadata_json)["unit_count"] == len(units)
    # The source asset and checksum are untouched.
    assert storage.read(row.file_asset.storage_key) == _science_pdf()


def test_a_second_sweep_finds_nothing_to_do(
    session: Session, admin: User
) -> None:
    storage = MemoryStorage()
    _, revision = _seed_book(session, admin, _science_pdf(), storage=storage)

    assert extract_due_revisions(session, limit=10, storage=storage) == 1
    assert extract_due_revisions(session, limit=10, storage=storage) == 0

    assert len(_events(session, EVENT_EXTRACT_STARTED)) == 1
    assert len(_events(session, EVENT_EXTRACT_COMPLETED)) == 1
    assert len(_units(session, revision.id)) > 0


def test_the_batch_limit_bounds_one_pass(
    session: Session, admin: User
) -> None:
    storage = MemoryStorage()
    _seed_book(session, admin, _science_pdf(), storage=storage)
    _seed_book(session, admin, _science_pdf(), storage=storage)

    assert extract_due_revisions(session, limit=1, storage=storage) == 1
    remaining = session.scalar(
        select(func.count()).select_from(BookRevision).where(
            BookRevision.status == BookRevisionStatus.UPLOADED.value
        )
    )
    assert remaining == 1
    assert extract_due_revisions(session, limit=10, storage=storage) == 1


def test_repeated_processing_never_duplicates_the_tree(
    session: Session, admin: User
) -> None:
    """A re-run (retry or double claim) rewrites, never doubles, the tree."""
    storage = MemoryStorage()
    _, revision = _seed_book(session, admin, _science_pdf(), storage=storage)

    assert extract_due_revisions(session, limit=10, storage=storage) == 1
    first_ids = {u.id for u in _units(session, revision.id)}
    first_count = len(first_ids)
    assert first_count > 0

    # Force a re-run exactly the way a retry would.
    row = _row(session, revision.id)
    row.status = BookRevisionStatus.UPLOADED.value
    session.commit()

    assert extract_due_revisions(session, limit=10, storage=storage) == 1
    second_ids = {u.id for u in _units(session, revision.id)}
    assert len(second_ids) == first_count
    assert second_ids.isdisjoint(first_ids)  # fresh rows, same count


# --- failures ----------------------------------------------------------------------------


def test_a_document_problem_commits_extraction_failed_and_no_units(
    session: Session, admin: User
) -> None:
    storage = MemoryStorage()
    # Bytes that parse as a PDF container but yield no text.
    _, revision = _seed_book(session, admin, build_minimal_pdf(None), storage=storage)

    assert extract_due_revisions(session, limit=10, storage=storage) == 1

    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.EXTRACTION_FAILED.value
    assert row.extraction_error == (
        "no_extractable_text: the document contains no extractable text"
    )
    assert row.extraction_summary is None
    assert _units(session, revision.id) == []

    started = _events(session, EVENT_EXTRACT_STARTED)
    failed = _events(session, EVENT_EXTRACT_FAILED)
    assert len(started) == 1 and len(failed) == 1
    payload = json.loads(failed[0].metadata_json)
    assert payload["reason"] == ERROR_NO_TEXT
    # Audit carries identifiers and fixed codes — never document text.
    assert "no document prose" not in json.dumps(payload)
    # The original bytes and their checksum row survive the failure.
    assert storage.read(row.file_asset.storage_key) == build_minimal_pdf(None)


def test_a_transient_storage_outage_rolls_back_and_keeps_the_revision_claimable(
    session: Session, admin: User
) -> None:
    """Unexpected exception: no commit, no audit, still ``uploaded``."""
    storage = OutageStorage()
    _, revision = _seed_book(session, admin, _science_pdf(), storage=storage)

    with pytest.raises(StorageError):
        extract_due_revisions(session, limit=10, storage=storage)
    session.rollback()  # what the sweeper does on a failing pass

    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.UPLOADED.value
    assert row.extraction_summary is None
    assert row.extraction_error is None
    assert _units(session, revision.id) == []
    assert _events(session, EVENT_EXTRACT_STARTED) == []
    assert _events(session, EVENT_EXTRACT_FAILED) == []

    # The next pass, with storage back, extracts normally.
    fixed = MemoryStorage()
    fixed.objects[row.file_asset.storage_key] = _science_pdf()
    assert extract_due_revisions(session, limit=10, storage=fixed) == 1
    assert _row(session, revision.id).status == (
        BookRevisionStatus.PENDING_REVIEW.value
    )


def test_a_crash_mid_extraction_leaves_no_partial_tree(
    session: Session, admin: User
) -> None:
    """Interrupted after claim: rollback releases everything, retry works."""
    storage = MemoryStorage()
    _, revision = _seed_book(session, admin, _science_pdf(), storage=storage)

    original = book_extraction_service._write_units

    def _boom(*args, **kwargs):
        raise RuntimeError("process dying mid-write")

    book_extraction_service._write_units = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            extract_due_revisions(session, limit=10, storage=storage)
    finally:
        book_extraction_service._write_units = original  # type: ignore[assignment]
    session.rollback()

    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.UPLOADED.value
    assert _units(session, revision.id) == []
    assert _events(session, EVENT_EXTRACT_COMPLETED) == []

    assert extract_due_revisions(session, limit=10, storage=storage) == 1
    assert _row(session, revision.id).status == (
        BookRevisionStatus.PENDING_REVIEW.value
    )
    assert _units(session, revision.id) != []


# --- retry --------------------------------------------------------------------------------


def test_retry_requeues_only_a_failed_revision_as_an_administrator(
    session: Session, admin: User
) -> None:
    storage = MemoryStorage()
    book, revision = _seed_book(session, admin, build_minimal_pdf(None), storage=storage)
    assert extract_due_revisions(session, limit=10, storage=storage) == 1
    assert _row(session, revision.id).status == (
        BookRevisionStatus.EXTRACTION_FAILED.value
    )

    read = retry_book_extraction(session, admin, book_id=book.id)
    session.commit()

    assert read.revision.status == BookRevisionStatus.UPLOADED.value
    assert read.revision.extraction_error is None
    events = _events(session, EVENT_EXTRACT_RETRY)
    assert len(events) == 1
    assert events[0].actor_user_id == admin.id  # who asked, not the uploader alone
    payload = json.loads(events[0].metadata_json)
    assert payload["book_id"] == str(book.id)

    # The sweeper's next pass actually re-extracts the stored PDF — with
    # GOOD bytes now stored, the retry path completes end to end.
    fixed = MemoryStorage()
    fixed.objects[_row(session, revision.id).file_asset.storage_key] = _science_pdf()
    assert extract_due_revisions(session, limit=10, storage=fixed) == 1
    assert _row(session, revision.id).status == (
        BookRevisionStatus.PENDING_REVIEW.value
    )


def test_retry_is_administrator_only(
    session: Session, admin: User, teacher: User
) -> None:
    storage = MemoryStorage()
    book, revision = _seed_book(session, admin, build_minimal_pdf(None), storage=storage)
    assert extract_due_revisions(session, limit=10, storage=storage) == 1

    with pytest.raises(LearningForbiddenError):
        retry_book_extraction(session, teacher, book_id=book.id)
    session.rollback()
    assert _row(session, revision.id).status == (
        BookRevisionStatus.EXTRACTION_FAILED.value
    )
    assert _events(session, EVENT_EXTRACT_RETRY) == []


def test_retry_of_an_unknown_book_is_a_not_found(session: Session, admin: User) -> None:
    with pytest.raises(LearningNotFoundError):
        retry_book_extraction(session, admin, book_id=uuid.uuid4())


def test_retry_of_anything_but_a_failed_state_is_a_conflict(
    session: Session, admin: User
) -> None:
    storage = MemoryStorage()
    book, revision = _seed_book(session, admin, _science_pdf(), storage=storage)

    # uploaded (not yet extracted)
    with pytest.raises(LearningConflictError):
        retry_book_extraction(session, admin, book_id=book.id)
    session.rollback()

    assert extract_due_revisions(session, limit=10, storage=storage) == 1
    # pending_review (already done)
    with pytest.raises(LearningConflictError):
        retry_book_extraction(session, admin, book_id=book.id)
    session.rollback()
    assert _events(session, EVENT_EXTRACT_RETRY) == []


# --- claim behaviour ------------------------------------------------------------------------


def test_the_claim_picks_the_oldest_uploaded_revision_first(
    session: Session, admin: User
) -> None:
    storage = MemoryStorage()
    first_book, first_revision = _seed_book(
        session, admin, _science_pdf(), storage=storage
    )
    second_book, second_revision = _seed_book(
        session, admin, _science_pdf(), storage=storage
    )
    # Force a deterministic age gap (SQLite timestamps may tie).
    from datetime import datetime, timedelta, timezone

    older = _row(session, first_revision.id)
    older.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.commit()

    claimed = book_extraction_service.book_repo.claim_next_uploaded_revision(session)
    assert claimed is not None
    assert claimed.id == first_revision.id
    session.rollback()


def test_a_revision_not_uploaded_is_never_extracted(
    session: Session, admin: User
) -> None:
    """The belt-and-braces re-check under the lock."""
    storage = MemoryStorage()
    _, revision = _seed_book(session, admin, _science_pdf(), storage=storage)
    row = _row(session, revision.id)
    row.status = BookRevisionStatus.PENDING_REVIEW.value
    session.commit()

    assert extract_due_revisions(session, limit=10, storage=storage) == 0
    assert _units(session, revision.id) == []


# --- no external transmission -------------------------------------------------------------


def test_extraction_performs_no_network_io_at_all(monkeypatch) -> None:
    """Proof the parser cannot transmit: sockets are torn out entirely."""
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("extraction attempted network I/O")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

    result = extract_book_pdf(_science_pdf())
    assert result.unit_count > 0
    # The disabled classifier is the one that ran: nothing is resolved.
    assert {
        unit.assessment_requirement for unit in result.walk()
    } == {AssessmentRequirement.UNRESOLVED.value}


# --- the sweeper's lifecycle ------------------------------------------------------------------


def _wait_until(predicate, *, timeout: float = 5.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for: {message}")


def _shrink_the_sweep_interval(monkeypatch) -> None:
    monkeypatch.setattr(
        sweeper_module,
        "get_settings",
        lambda: SimpleNamespace(
            BOOK_EXTRACT_SWEEP_INTERVAL_SECONDS=0.05,
            BOOK_EXTRACT_SWEEP_BATCH_LIMIT=1,
        ),
    )


def test_start_sweeps_immediately_and_stop_joins_the_thread(monkeypatch) -> None:
    _shrink_the_sweep_interval(monkeypatch)
    sweeper = BookExtractionSweeper()
    calls: list[float] = []
    monkeypatch.setattr(sweeper, "sweep_once", lambda: calls.append(time.monotonic()) or 0)

    sweeper.start()
    sweeper.start()  # idempotent
    _wait_until(lambda: len(calls) >= 2, message="sweeper never swept")
    assert sweeper.is_running
    thread = sweeper._thread

    sweeper.stop()
    sweeper.stop()  # idempotent
    assert not sweeper.is_running
    assert not thread.is_alive()
    stopped_at = len(calls)
    time.sleep(0.2)
    assert len(calls) == stopped_at


def test_the_first_pass_happens_before_any_wait(monkeypatch) -> None:
    sweeper = BookExtractionSweeper()
    seen: list[float] = []
    started = time.monotonic()
    monkeypatch.setattr(sweeper, "sweep_once", lambda: seen.append(time.monotonic()) or 0)
    sweeper.start()
    try:
        _wait_until(lambda: seen, message="startup sweep never ran")
        assert seen[0] - started < 1.0
    finally:
        sweeper.stop()


def test_a_failing_pass_never_kills_the_loop(monkeypatch) -> None:
    _shrink_the_sweep_interval(monkeypatch)
    sweeper = BookExtractionSweeper()
    attempts: list[int] = []
    lock = threading.Lock()

    def _flaky() -> int:
        with lock:
            attempts.append(1)
            count = len(attempts)
        if count <= 2:
            raise RuntimeError("database hiccup")
        return 0

    monkeypatch.setattr(sweeper, "sweep_once", _flaky)
    sweeper.start()
    try:
        _wait_until(lambda: len(attempts) >= 3, message="loop died after failures")
        assert sweeper.is_running
    finally:
        sweeper.stop()
    assert not sweeper.is_running


def test_sweep_once_uses_one_fresh_short_lived_session(monkeypatch) -> None:
    events: list[str] = []

    class _RecordingSession:
        def commit(self) -> None:
            events.append("commit")

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    def _factory():
        events.append("open")
        return _RecordingSession()

    seen: dict[str, object] = {}

    def _extract_due(session, *, limit, storage=None):
        seen["session"] = session
        seen["limit"] = limit
        return 3

    monkeypatch.setattr(sweeper_module, "SessionLocal", _factory)
    monkeypatch.setattr(book_extraction_service, "extract_due_revisions", _extract_due)

    assert sweeper_module.book_extraction_sweeper.sweep_once() == 3
    assert events == ["open", "commit", "close"]
    assert isinstance(seen["session"], _RecordingSession)
    assert seen["limit"] == 10  # the documented batch default


def test_sweep_once_rolls_back_and_still_closes_on_failure(monkeypatch) -> None:
    events: list[str] = []

    class _RecordingSession:
        def commit(self) -> None:
            events.append("commit")

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(sweeper_module, "SessionLocal", _RecordingSession)

    def _boom(session, *, limit, storage=None):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(book_extraction_service, "extract_due_revisions", _boom)

    with pytest.raises(RuntimeError):
        sweeper_module.book_extraction_sweeper.sweep_once()
    assert events == ["rollback", "close"]


def test_the_process_sweeper_is_stopped_between_tests() -> None:
    assert sweeper_module.book_extraction_sweeper.is_running is False


def test_the_application_lifespan_starts_and_stops_the_sweeper(monkeypatch) -> None:
    from app.main import app

    actions: list[str] = []
    monkeypatch.setattr(
        sweeper_module.book_extraction_sweeper, "start", lambda: actions.append("start")
    )
    monkeypatch.setattr(
        sweeper_module.book_extraction_sweeper, "stop", lambda **_: actions.append("stop")
    )

    async def _round_trip() -> None:
        async with app.router.lifespan_context(app):
            assert actions == ["start"]
        assert actions == ["start", "stop"]

    asyncio.run(_round_trip())


def test_the_sweep_defaults_target_at_most_five_seconds() -> None:
    from app.core.config import get_settings

    settings = get_settings()
    assert 0 < settings.BOOK_EXTRACT_SWEEP_INTERVAL_SECONDS <= 5.0
    assert settings.BOOK_EXTRACT_SWEEP_BATCH_LIMIT >= 1
