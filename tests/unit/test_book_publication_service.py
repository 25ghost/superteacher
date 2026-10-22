"""Unit tests: publication, rejection and correction revisions (book P3).

In-memory SQLite — the model CHECKs and the partial unique publication
index are REAL here, so the database rules are proven, not mocked:

- **publication** (``approve_revision``) validates the whole tree first
  (every unit approved, every chapter/topic designation resolved, no
  unresolved structural warning, a valid non-empty tree — anything
  incomplete → 422 with the reason) and only then moves the book's
  current-publication pointer in ONE transaction: the previous current
  flag is cleared and flushed BEFORE the new one is set, previous
  published revisions keep ``status='published'``, and the partial
  unique index makes a second current row impossible even if a future
  writer bypasses the service;
- **rejection** stores the mandatory stripped reason (blank → 422) plus
  its audit event, after which the revision refuses every other
  decision (409);
- **correction revisions** clone a rejected/published source into the
  next edition: fresh unit ids, remapped parents, review state reset,
  contexts and summary shared — the source stays byte-identical.

True ``SELECT ... FOR UPDATE`` serialization of two administrators
racing to publish is covered in the PostgreSQL integration suite.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.book import Book
from app.models.book_revision import BookRevision
from app.models.book_revision_context import BookRevisionContext
from app.models.book_unit import BookUnit
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    AssessmentRequirement,
    BookRevisionStatus,
    BookUnitKind,
    BookUnitReviewStatus,
    FileAssetStatus,
    UserRole,
    UserStatus,
)
from app.models.file_asset import FileAsset
from app.models.learning_context import LearningContext
from app.models.pathway import Pathway
from app.models.subject import Subject
from app.models.user import User
from app.schemas.book import BookRevisionRejectRequest
from app.services.book_review_service import (
    EVENT_REVISION_APPROVED,
    EVENT_REVISION_CORRECTED,
    EVENT_REVISION_REJECTED,
    EVENT_UNIT_APPROVED,
    EVENT_UNIT_CORRECTED,
    approve_revision,
    create_correction_revision,
    reject_revision,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)

PASSWORD = "correct horse battery staple"
PAGE_COUNT = 2
SEEDED_REASON = "seeded rejection reason"


# --- fixtures ------------------------------------------------------------------------


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


def _make_user(role: str) -> User:
    return User(
        email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
        role=role,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )


@pytest.fixture()
def admin(session: Session) -> User:
    user = _make_user(UserRole.ADMIN.value)
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def teacher(session: Session) -> User:
    user = _make_user(UserRole.TEACHER.value)
    session.add(user)
    session.commit()
    return user


# --- seeding -------------------------------------------------------------------------


def _catalog_context(session: Session) -> LearningContext:
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
    return context


#: chapter → topic → leaf (nested) + a root exercise set — reading order.
DEFAULT_TREE = (
    {
        "key": "chapter",
        "kind": BookUnitKind.CHAPTER.value,
        "title": "Chapter 1 Introduction",
        "content": "Science studies the world.",
        "parent": None,
        "order": 1,
        "start": 1,
        "end": 1,
        "assessment": AssessmentRequirement.UNRESOLVED.value,
        "warnings": None,
        "review": "pending",
    },
    {
        "key": "topic",
        "kind": BookUnitKind.TOPIC.value,
        "title": "1.1 Water Cycle",
        "content": "Water evaporates.",
        "parent": "chapter",
        "order": 1,
        "start": 1,
        "end": 2,
        "assessment": AssessmentRequirement.UNRESOLVED.value,
        "warnings": None,
        "review": "pending",
    },
    {
        "key": "leaf",
        "kind": BookUnitKind.EXPLANATION.value,
        "title": "Evaporation",
        "content": "Details about evaporation.",
        "parent": "topic",
        "order": 1,
        "start": 2,
        "end": 2,
        "assessment": AssessmentRequirement.NOT_REQUIRED.value,
        "warnings": None,
        "review": "pending",
    },
    {
        "key": "exercise",
        "kind": BookUnitKind.EXERCISE_SET.value,
        "title": "Exercises",
        "content": "1. Solve it.",
        "parent": None,
        "order": 2,
        "start": 2,
        "end": 2,
        "assessment": AssessmentRequirement.NOT_REQUIRED.value,
        "warnings": None,
        "review": "pending",
    },
)


def approved_tree() -> tuple[dict, ...]:
    """The default tree with designations resolved and every unit approved."""
    return tuple(
        {
            **spec,
            "assessment": (
                AssessmentRequirement.REQUIRED.value
                if spec["kind"] in ("chapter", "topic")
                else spec["assessment"]
            ),
            "review": BookUnitReviewStatus.APPROVED.value,
        }
        for spec in DEFAULT_TREE
    )


def _asset(session: Session, admin: User) -> FileAsset:
    asset = FileAsset(
        uploaded_by_user_id=admin.id,
        original_filename="textbook.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        checksum_sha256="deadbeef",
        storage_key="pending",
        validation_status=FileAssetStatus.STORED.value,
    )
    session.add(asset)
    session.flush()
    asset.storage_key = f"file_assets/{asset.id}"
    return asset


def _add_revision(
    session: Session,
    admin: User,
    book: Book,
    *,
    number: int,
    tree: tuple[dict, ...] = DEFAULT_TREE,
    status: str = BookRevisionStatus.PENDING_REVIEW.value,
    current: bool = False,
    rejection_reason: str | None = None,
    asset: FileAsset | None = None,
) -> tuple[BookRevision, dict[str, uuid.UUID]]:
    asset = asset or _asset(session, admin)
    revision = BookRevision(
        book_id=book.id,
        revision_number=number,
        file_asset_id=asset.id,
        uploaded_by_user_id=admin.id,
        status=status,
        extraction_summary=json.dumps(
            {
                "page_count": PAGE_COUNT,
                "unit_count": len(tree),
                "by_kind": {},
                "warnings": [],
            }
        ),
        is_current_publication=current,
        rejection_reason=rejection_reason,
    )
    session.add(revision)
    session.flush()

    ids = {spec["key"]: uuid.uuid4() for spec in tree}
    now = datetime.now(timezone.utc)
    for spec in tree:
        approved = spec["review"] == BookUnitReviewStatus.APPROVED.value
        session.add(
            BookUnit(
                id=ids[spec["key"]],
                book_revision_id=revision.id,
                parent_unit_id=ids[spec["parent"]] if spec["parent"] else None,
                kind=spec["kind"],
                title=spec["title"],
                content=spec["content"],
                display_order=spec["order"],
                source_page_start=spec["start"],
                source_page_end=spec["end"],
                extraction_warnings=spec["warnings"],
                assessment_requirement=spec["assessment"],
                review_status=spec["review"],
                reviewed_by_user_id=admin.id if approved else None,
                reviewed_at=now if approved else None,
            )
        )
    return revision, ids


def _seed(
    session: Session,
    admin: User,
    **kwargs,
) -> tuple[Book, BookRevision, dict[str, uuid.UUID]]:
    """One book with ONE revision; returns ``(book, revision, unit ids)``."""
    context = _catalog_context(session)
    book = Book(title="Science S3", created_by_user_id=admin.id)
    session.add(book)
    session.flush()
    revision, ids = _add_revision(
        session, admin, book, number=1, **kwargs
    )
    session.add(
        BookRevisionContext(
            book_revision_id=revision.id, learning_context_id=context.id
        )
    )
    session.commit()
    return book, revision, ids


def _row(session: Session, revision_id) -> BookRevision:
    session.expire_all()
    return session.get(BookRevision, revision_id)


def _events(session: Session, event_type: str) -> list[AuthEvent]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuthEvent).where(AuthEvent.event_type == event_type)
        )
    )


# --- publication ---------------------------------------------------------------------


def test_full_approval_publishes_the_revision_and_moves_the_pointer(
    session: Session, admin: User
) -> None:
    context = _catalog_context(session)
    book = Book(title="Science S3", created_by_user_id=admin.id)
    session.add(book)
    session.flush()
    asset = _asset(session, admin)
    first, _ = _add_revision(
        session,
        admin,
        book,
        number=1,
        tree=approved_tree(),
        status=BookRevisionStatus.PUBLISHED.value,
        current=True,
        asset=asset,
    )
    second, _ = _add_revision(
        session, admin, book, number=2, tree=approved_tree(), asset=asset
    )
    session.add(
        BookRevisionContext(
            book_revision_id=second.id, learning_context_id=context.id
        )
    )
    session.commit()

    result = approve_revision(
        session, admin, book_id=book.id, revision_id=second.id
    )
    session.commit()

    assert result.status == BookRevisionStatus.PUBLISHED
    assert result.is_current_publication is True
    assert all(
        unit.review_status == BookUnitReviewStatus.APPROVED
        for unit in result.units
    )

    old = _row(session, first.id)
    new = _row(session, second.id)
    # History is never rewritten: the old edition stays published, only
    # the CURRENT pointer moved.
    assert old.status == BookRevisionStatus.PUBLISHED.value
    assert old.is_current_publication is False
    assert new.status == BookRevisionStatus.PUBLISHED.value
    assert new.is_current_publication is True

    events = _events(session, EVENT_REVISION_APPROVED)
    assert len(events) == 1
    payload = json.loads(events[0].metadata_json)
    assert payload["previous_published_revision_id"] == str(first.id)
    assert payload["revision_number"] == 2
    assert payload["unit_count"] == 4
    assert events[0].actor_user_id == admin.id
    assert "Water" not in events[0].metadata_json


def test_the_first_publication_has_no_previous_pointer(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(session, admin, tree=approved_tree())

    approve_revision(session, admin, book_id=book.id, revision_id=revision.id)

    payload = json.loads(_events(session, EVENT_REVISION_APPROVED)[0].metadata_json)
    assert payload["previous_published_revision_id"] is None
    assert _row(session, revision.id).is_current_publication is True


def test_publication_requires_every_unit_approved(
    session: Session, admin: User
) -> None:
    tree = tuple(
        {
            **spec,
            "review": (
                BookUnitReviewStatus.PENDING.value
                if spec["key"] == "exercise"
                else BookUnitReviewStatus.APPROVED.value
            ),
            "assessment": (
                AssessmentRequirement.REQUIRED.value
                if spec["kind"] in ("chapter", "topic")
                else spec["assessment"]
            ),
        }
        for spec in DEFAULT_TREE
    )
    book, revision, _ = _seed(session, admin, tree=tree)

    with pytest.raises(LearningValidationError, match="still unapproved"):
        approve_revision(
            session, admin, book_id=book.id, revision_id=revision.id
        )

    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.PENDING_REVIEW.value
    assert row.is_current_publication is False
    assert _events(session, EVENT_REVISION_APPROVED) == []


def test_publication_requires_resolved_designations(
    session: Session, admin: User
) -> None:
    # Fully "approved" rows whose chapter designation was never decided.
    tree = tuple(
        {
            **spec,
            "assessment": (
                AssessmentRequirement.UNRESOLVED.value
                if spec["key"] == "chapter"
                else spec["assessment"]
            ),
            "review": BookUnitReviewStatus.APPROVED.value,
        }
        for spec in approved_tree()
    )
    book, revision, _ = _seed(session, admin, tree=tree)

    with pytest.raises(LearningValidationError, match="assessment designation"):
        approve_revision(
            session, admin, book_id=book.id, revision_id=revision.id
        )
    assert _row(session, revision.id).status == (
        BookRevisionStatus.PENDING_REVIEW.value
    )
    assert _events(session, EVENT_REVISION_APPROVED) == []


def test_publication_refuses_unresolved_structural_warnings(
    session: Session, admin: User
) -> None:
    from app.services.book_extraction import WARNING_UNRESOLVED_STRUCTURE

    tree = tuple(
        {
            **spec,
            "warnings": WARNING_UNRESOLVED_STRUCTURE
            if spec["key"] == "chapter"
            else spec["warnings"],
        }
        for spec in approved_tree()
    )
    book, revision, _ = _seed(session, admin, tree=tree)

    with pytest.raises(LearningValidationError, match="structural warning"):
        approve_revision(
            session, admin, book_id=book.id, revision_id=revision.id
        )
    assert _events(session, EVENT_REVISION_APPROVED) == []


def test_publication_refuses_an_empty_revision(session: Session, admin: User) -> None:
    book, revision, _ = _seed(session, admin, tree=())

    with pytest.raises(LearningValidationError, match="no extracted units"):
        approve_revision(
            session, admin, book_id=book.id, revision_id=revision.id
        )
    assert _row(session, revision.id).is_current_publication is False


def test_publication_is_409_unless_the_revision_is_pending(
    session: Session, admin: User
) -> None:
    for status in (
        BookRevisionStatus.PUBLISHED.value,
        BookRevisionStatus.REJECTED.value,
        BookRevisionStatus.UPLOADED.value,
    ):
        book, revision, _ = _seed(
            session,
            admin,
            tree=approved_tree(),
            status=status,
            rejection_reason=SEEDED_REASON
            if status == BookRevisionStatus.REJECTED.value
            else None,
        )
        with pytest.raises(LearningConflictError, match="pending_review"):
            approve_revision(
                session, admin, book_id=book.id, revision_id=revision.id
            )


def test_publication_unknown_ids_answer_404(session: Session, admin: User) -> None:
    book, revision, _ = _seed(session, admin, tree=approved_tree())
    _, other, _ = _seed(session, admin, tree=approved_tree())

    with pytest.raises(LearningNotFoundError, match="no book with id"):
        approve_revision(
            session, admin, book_id=uuid.uuid4(), revision_id=revision.id
        )
    with pytest.raises(LearningNotFoundError, match="no book revision with id"):
        approve_revision(
            session, admin, book_id=book.id, revision_id=uuid.uuid4()
        )
    with pytest.raises(LearningNotFoundError, match="no book revision with id"):
        approve_revision(
            session, admin, book_id=book.id, revision_id=other.id
        )


def test_the_database_itself_allows_only_one_current_publication(
    session: Session, admin: User
) -> None:
    """The partial unique index — not just the service — enforces the rule."""
    book, revision, _ = _seed(
        session, admin, tree=approved_tree(), current=True
    )

    with pytest.raises(IntegrityError):
        # A second CURRENT row is refused by the index at INSERT time —
        # no service code ran between the two writes.
        _add_revision(
            session,
            admin,
            book,
            number=2,
            tree=approved_tree(),
            status=BookRevisionStatus.PUBLISHED.value,
            current=True,  # deliberately wrong: two current rows
        )
    session.rollback()

    # After the rollback the legitimate single current row still stands.
    assert _row(session, revision.id).is_current_publication is True
    assert (
        session.query(BookRevision)
        .filter(BookRevision.is_current_publication.is_(True))
        .count()
        == 1
    )


def test_the_database_refuses_a_rejected_revision_without_a_reason(
    session: Session, admin: User
) -> None:
    context = _catalog_context(session)
    book = Book(title="Science S3", created_by_user_id=admin.id)
    session.add(book)
    session.flush()
    session.add(
        BookRevision(
            book_id=book.id,
            revision_number=1,
            file_asset_id=_asset(session, admin).id,
            uploaded_by_user_id=admin.id,
            status=BookRevisionStatus.REJECTED.value,
            rejection_reason=None,  # the CHECK must refuse this row
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    assert context.id  # the catalog row is unaffected


def test_the_database_refuses_an_approved_unit_without_a_reviewer(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(session, admin)
    session.add(
        BookUnit(
            book_revision_id=revision.id,
            kind=BookUnitKind.TOPIC.value,
            title="Ghost",
            content="",
            display_order=99,
            source_page_start=1,
            source_page_end=1,
            assessment_requirement=AssessmentRequirement.NOT_REQUIRED.value,
            review_status=BookUnitReviewStatus.APPROVED.value,
            reviewed_by_user_id=None,
            reviewed_at=None,  # approved without WHO/WHEN: refused
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --- rejection -------------------------------------------------------------------------


def test_rejection_records_a_stripped_reason_and_audits(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(session, admin)

    result = reject_revision(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        payload=BookRevisionRejectRequest(reason="  pages 12-15 are scanned  "),
    )

    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.REJECTED.value
    assert row.rejection_reason == "pages 12-15 are scanned"
    assert row.is_current_publication is False
    assert result.rejection_reason == "pages 12-15 are scanned"
    assert result.units  # the tree is still inspectable after rejection

    events = _events(session, EVENT_REVISION_REJECTED)
    assert len(events) == 1
    payload = json.loads(events[0].metadata_json)
    assert payload["reason"] == "pages 12-15 are scanned"
    assert events[0].actor_user_id == admin.id
    assert events[0].user_id == revision.uploaded_by_user_id


def test_rejection_requires_a_nonblank_reason(session: Session, admin: User) -> None:
    book, revision, _ = _seed(session, admin)

    with pytest.raises(LearningValidationError, match="rejection reason"):
        reject_revision(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            payload=BookRevisionRejectRequest(reason="   "),
        )
    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.PENDING_REVIEW.value
    assert row.rejection_reason is None
    assert _events(session, EVENT_REVISION_REJECTED) == []


def test_rejection_is_409_unless_the_revision_is_pending(
    session: Session, admin: User
) -> None:
    for status in (
        BookRevisionStatus.PUBLISHED.value,
        BookRevisionStatus.REJECTED.value,
    ):
        book, revision, _ = _seed(
            session,
            admin,
            status=status,
            rejection_reason=SEEDED_REASON
            if status == BookRevisionStatus.REJECTED.value
            else None,
        )
        with pytest.raises(LearningConflictError, match="pending_review"):
            reject_revision(
                session,
                admin,
                book_id=book.id,
                revision_id=revision.id,
                payload=BookRevisionRejectRequest(reason="again"),
            )


def test_a_rejected_revision_refuses_every_other_decision(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(session, admin)

    reject_revision(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        payload=BookRevisionRejectRequest(reason="wrong edition"),
    )
    assert _row(session, revision.id).status == (
        BookRevisionStatus.REJECTED.value
    )

    with pytest.raises(LearningConflictError, match="pending_review"):
        approve_revision(
            session, admin, book_id=book.id, revision_id=revision.id
        )
    with pytest.raises(LearningConflictError, match="pending_review"):
        reject_revision(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            payload=BookRevisionRejectRequest(reason="second"),
        )
    # Rejection moved the revision exactly once, with one audit row.
    assert len(_events(session, EVENT_REVISION_REJECTED)) == 1
    assert _events(session, EVENT_REVISION_APPROVED) == []
    assert _row(session, revision.id).is_current_publication is False


def test_everything_after_rejection_stays_409_at_the_service_layer(
    session: Session, admin: User
) -> None:
    from app.services.book_review_service import approve_unit, correct_unit
    from app.schemas.book import BookUnitCorrectRequest

    book, revision, ids = _seed(session, admin)
    reject_revision(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        payload=BookRevisionRejectRequest(reason="no good"),
    )

    with pytest.raises(LearningConflictError, match="pending_review"):
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["topic"],
            payload=BookUnitCorrectRequest(title="x"),
        )
    with pytest.raises(LearningConflictError, match="pending_review"):
        approve_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["leaf"],
        )
    assert _events(session, EVENT_UNIT_CORRECTED) == []
    assert _events(session, EVENT_UNIT_APPROVED) == []


# --- correction revisions ----------------------------------------------------------------


def test_a_correction_revision_clones_the_tree_and_contexts(
    session: Session, admin: User
) -> None:
    book, source, source_ids = _seed(
        session,
        admin,
        status=BookRevisionStatus.REJECTED.value,
        rejection_reason="unit 1.1 mis-scoped",
    )
    source_before = {
        unit.id: (unit.parent_unit_id, unit.review_status)
        for unit in _tree(session, source.id)
    }

    created = create_correction_revision(
        session, admin, book_id=book.id, revision_id=source.id
    )
    session.commit()

    # The new EDITION, pending, sharing the same source document.
    assert created.revision_number == 2
    assert created.status == BookRevisionStatus.PENDING_REVIEW
    assert created.is_current_publication is False
    assert created.rejection_reason is None
    assert created.extraction_summary == json.loads(
        _row(session, source.id).extraction_summary
    )
    assert created.file_asset.file_asset_id == _row(
        session, source.id
    ).file_asset_id

    # The tree is cloned: same shape and content, brand-new ids.
    new_rows = _tree(session, created.revision_id)
    assert sorted(u.title for u in created.units) == sorted(
        u.title for u in _tree(session, source.id)
    )
    assert len(new_rows) == len(source_before)
    assert {u.id for u in new_rows}.isdisjoint(source_before)
    by_title = {u.title: u for u in new_rows}
    topic = by_title["1.1 Water Cycle"]
    chapter = by_title["Chapter 1 Introduction"]
    assert topic.parent_unit_id == chapter.id  # parents remapped INSIDE the copy
    assert chapter.parent_unit_id is None
    # Every unit starts over: pending, no reviewer, warnings/pages kept.
    for unit in new_rows:
        assert unit.review_status == BookUnitReviewStatus.PENDING.value
        assert unit.reviewed_by_user_id is None
        assert unit.reviewed_at is None
        assert unit.source_page_start >= 1
    assert by_title["Chapter 1 Introduction"].extraction_warnings is None

    # Contexts are shared with the copy (same book, same eligibility).
    source_contexts = _context_ids(session, source.id)
    assert source_contexts == _context_ids(session, created.revision_id)
    assert source_contexts  # non-empty

    # The source revision and its units are byte-identical afterwards.
    source_after = _row(session, source.id)
    assert source_after.status == BookRevisionStatus.REJECTED.value
    assert source_after.rejection_reason == "unit 1.1 mis-scoped"
    assert {
        u.id: (u.parent_unit_id, u.review_status)
        for u in _tree(session, source.id)
    } == source_before

    events = _events(session, EVENT_REVISION_CORRECTED)
    payload = json.loads(events[0].metadata_json)
    assert payload["source_revision_id"] == str(source.id)
    assert payload["book_revision_id"] == str(created.revision_id)
    assert payload["unit_count"] == len(source_before)


def test_correction_revision_numbering_advances_per_book(
    session: Session, admin: User
) -> None:
    book, source, _ = _seed(
        session,
        admin,
        status=BookRevisionStatus.REJECTED.value,
        rejection_reason="first rejection",
    )

    first = create_correction_revision(
        session, admin, book_id=book.id, revision_id=source.id
    )
    second = create_correction_revision(
        session, admin, book_id=book.id, revision_id=source.id
    )
    session.commit()

    assert (first.revision_number, second.revision_number) == (2, 3)


def test_a_published_revision_can_be_corrected_without_moving_the_pointer(
    session: Session, admin: User
) -> None:
    book, source, _ = _seed(
        session,
        admin,
        tree=approved_tree(),
        status=BookRevisionStatus.PUBLISHED.value,
        current=True,
    )

    created = create_correction_revision(
        session, admin, book_id=book.id, revision_id=source.id
    )
    session.commit()

    source_row = _row(session, source.id)
    assert source_row.status == BookRevisionStatus.PUBLISHED.value
    assert source_row.is_current_publication is True  # untouched
    assert created.is_current_publication is False
    assert created.status == BookRevisionStatus.PENDING_REVIEW.value
    # The clone starts over — publication never carries across editions.
    assert all(
        u.review_status == BookUnitReviewStatus.PENDING.value
        for u in _tree(session, created.revision_id)
    )


def test_only_rejected_or_published_sources_can_be_corrected(
    session: Session, admin: User
) -> None:
    for status in (
        BookRevisionStatus.PENDING_REVIEW.value,
        BookRevisionStatus.UPLOADED.value,
        BookRevisionStatus.EXTRACTING.value,
    ):
        book, revision, _ = _seed(session, admin, status=status)
        with pytest.raises(LearningConflictError, match="corrected"):
            create_correction_revision(
                session, admin, book_id=book.id, revision_id=revision.id
            )
    assert _events(session, EVENT_REVISION_CORRECTED) == []


def test_correction_revision_unknown_ids_answer_404(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(
        session,
        admin,
        status=BookRevisionStatus.REJECTED.value,
        rejection_reason="no good",
    )
    _, other, _ = _seed(
        session,
        admin,
        status=BookRevisionStatus.REJECTED.value,
        rejection_reason="also no good",
    )

    with pytest.raises(LearningNotFoundError, match="no book with id"):
        create_correction_revision(
            session, admin, book_id=uuid.uuid4(), revision_id=revision.id
        )
    with pytest.raises(LearningNotFoundError, match="no book revision with id"):
        create_correction_revision(
            session, admin, book_id=book.id, revision_id=uuid.uuid4()
        )
    with pytest.raises(LearningNotFoundError, match="no book revision with id"):
        create_correction_revision(
            session, admin, book_id=book.id, revision_id=other.id
        )


# --- guards & audit contract -------------------------------------------------------------


def test_every_decision_is_administrator_only(
    session: Session, admin: User, teacher: User
) -> None:
    book, revision, _ = _seed(
        session,
        admin,
        status=BookRevisionStatus.REJECTED.value,
        rejection_reason=SEEDED_REASON,
    )

    with pytest.raises(LearningForbiddenError):
        approve_revision(
            session, teacher, book_id=book.id, revision_id=revision.id
        )
    with pytest.raises(LearningForbiddenError):
        reject_revision(
            session,
            teacher,
            book_id=book.id,
            revision_id=revision.id,
            payload=BookRevisionRejectRequest(reason="nope"),
        )
    with pytest.raises(LearningForbiddenError):
        create_correction_revision(
            session, teacher, book_id=book.id, revision_id=revision.id
        )
    assert _events(session, EVENT_REVISION_CORRECTED) == []


def test_all_event_names_fit_the_audit_column() -> None:
    for name in (
        EVENT_UNIT_CORRECTED,
        EVENT_UNIT_APPROVED,
        EVENT_REVISION_APPROVED,
        EVENT_REVISION_REJECTED,
        EVENT_REVISION_CORRECTED,
    ):
        assert len(name) <= 32, name


# --- helpers -----------------------------------------------------------------------------


def _tree(session: Session, revision_id) -> list[BookUnit]:
    session.expire_all()
    return list(
        session.scalars(
            select(BookUnit)
            .where(BookUnit.book_revision_id == revision_id)
            .order_by(BookUnit.display_order, BookUnit.created_at)
        )
    )


def _context_ids(session: Session, revision_id) -> set[uuid.UUID]:
    session.expire_all()
    return {
        row.learning_context_id
        for row in session.scalars(
            select(BookRevisionContext).where(
                BookRevisionContext.book_revision_id == revision_id
            )
        )
    }
