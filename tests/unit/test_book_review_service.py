"""Unit tests: inspection, correction and per-unit approval (book P3).

In-memory SQLite (model CHECKs and partial unique indexes are real
here), no HTTP — the durable half of the review surface:

- **inspection** exposes the complete unit tree in reading order with
  review state, warnings and designations, never a storage path, and
  answers unknown and foreign ids with the SAME 404;
- **correction** applies only the supplied fields, validates EVERYTHING
  before touching a column (so a refused correction changes neither
  content, review state nor audit trail), resets the corrected unit to
  ``pending``, resets a whole moved subtree on structural changes, and
  resolves ``unresolved_structure`` only when kind/parent is re-asserted
  — approval never erases a warning;
- **per-unit approval** records reviewer identity + timestamp, refuses a
  chapter/topic whose designation is still ``unresolved``, refuses a
  double approval (409), and every operation is administrator-only.

Publication, rejection and correction revisions live in
``test_book_publication_service.py``.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
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
from app.schemas.book import BookUnitCorrectRequest
from app.services.book_extraction import WARNING_UNRESOLVED_STRUCTURE
from app.services.book_review_service import (
    EVENT_REVISION_APPROVED,
    EVENT_REVISION_CORRECTED,
    EVENT_REVISION_REJECTED,
    EVENT_UNIT_APPROVED,
    EVENT_UNIT_CORRECTED,
    approve_unit,
    correct_unit,
    get_revision_detail,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)

PASSWORD = "correct horse battery staple"
PAGE_COUNT = 2


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


#: The default extracted tree, in reading order:
#: chapter → topic → leaf (nested) + a sibling exercise set at the root.
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


def _seed(
    session: Session,
    admin: User,
    *,
    tree: tuple[dict, ...] = DEFAULT_TREE,
    status: str = BookRevisionStatus.PENDING_REVIEW.value,
):
    """One book, one revision in ``status``, its context link and a tree.

    Returns ``(book, revision, ids)`` where ``ids`` maps the tree keys to
    the committed unit ids. Units are written with explicit ids so parent
    remapping never depends on flush order.
    """
    context = _catalog_context(session)
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

    book = Book(title="Science S3", created_by_user_id=admin.id)
    session.add(book)
    session.flush()
    revision = BookRevision(
        book_id=book.id,
        revision_number=1,
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
        is_current_publication=False,
        # The model CHECK refuses a rejected revision without a reason —
        # a seeded rejected row must already satisfy it.
        rejection_reason=(
            "seeded rejection reason"
            if status == BookRevisionStatus.REJECTED.value
            else None
        ),
    )
    session.add(revision)
    session.flush()
    session.add(
        BookRevisionContext(
            book_revision_id=revision.id, learning_context_id=context.id
        )
    )

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
    session.commit()
    return book, revision, ids


def _row(session: Session, revision_id) -> BookRevision:
    session.expire_all()
    return session.get(BookRevision, revision_id)


def _unit_row(session: Session, unit_id) -> BookUnit:
    session.expire_all()
    return session.get(BookUnit, unit_id)


def _events(session: Session, event_type: str) -> list[AuthEvent]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuthEvent).where(AuthEvent.event_type == event_type)
        )
    )


def _approved_tree() -> tuple[dict, ...]:
    """The default tree with every designation resolved and unit approved."""
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


# --- inspection ----------------------------------------------------------------------


def test_inspection_exposes_the_tree_in_reading_order_with_state(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)

    detail = get_revision_detail(
        session, admin, book_id=book.id, revision_id=revision.id
    )

    assert detail.status == BookRevisionStatus.PENDING_REVIEW
    assert detail.is_current_publication is False
    assert detail.rejection_reason is None
    # Parents always precede their children; sibling order preserved.
    assert [unit.title for unit in detail.units] == [
        "Chapter 1 Introduction",
        "1.1 Water Cycle",
        "Evaporation",
        "Exercises",
    ]
    assert [unit.unit_id for unit in detail.units] == [
        ids["chapter"],
        ids["topic"],
        ids["leaf"],
        ids["exercise"],
    ]
    chapter = detail.units[0]
    topic = detail.units[1]
    assert topic.parent_unit_id == chapter.unit_id
    assert detail.units[3].parent_unit_id is None
    assert all(
        unit.review_status == BookUnitReviewStatus.PENDING
        for unit in detail.units
    )
    assert chapter.extraction_warnings == []  # default tree is clean
    assert chapter.assessment_requirement == AssessmentRequirement.UNRESOLVED
    assert chapter.source_page_start == 1 and chapter.source_page_end == 1
    # Inspection is a read: nothing was written.
    assert _events(session, EVENT_UNIT_APPROVED) == []
    assert _events(session, EVENT_REVISION_APPROVED) == []


def test_inspection_never_exposes_storage_paths_or_keys(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(session, admin)

    detail = get_revision_detail(
        session, admin, book_id=book.id, revision_id=revision.id
    )
    dump = json.dumps(detail.model_dump(mode="json"))
    session.expire_all()
    real_key = str(
        session.get(BookRevision, revision.id).file_asset.storage_key
    )

    assert "storage_key" not in dump
    assert real_key not in dump
    assert "file_assets/" not in dump
    # The asset's human metadata IS exposed (filename, size, checksum)…
    assert "textbook.pdf" in dump
    # …while the storage key itself is structurally absent.


def test_unknown_and_foreign_revision_ids_answer_the_same_404(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(session, admin)
    other_book, other_revision, _ = _seed(session, admin)

    with pytest.raises(LearningNotFoundError) as unknown:
        get_revision_detail(
            session, admin, book_id=book.id, revision_id=uuid.uuid4()
        )
    with pytest.raises(LearningNotFoundError) as foreign:
        # The revision EXISTS — just not under this book. The caller must
        # not be able to tell the difference.
        get_revision_detail(
            session, admin, book_id=book.id, revision_id=other_revision.id
        )
    with pytest.raises(LearningNotFoundError) as no_book:
        get_revision_detail(
            session, admin, book_id=uuid.uuid4(), revision_id=revision.id
        )

    for exc in (unknown.value, foreign.value):
        assert exc.status_code == 404
        assert str(exc).startswith("no book revision with id ")
    assert str(no_book.value).startswith("no book with id ")
    assert other_book.id != book.id


def test_every_review_operation_requires_an_administrator(
    session: Session, admin: User, teacher: User
) -> None:
    book, revision, ids = _seed(session, admin)

    with pytest.raises(LearningForbiddenError):
        get_revision_detail(
            session, teacher, book_id=book.id, revision_id=revision.id
        )
    with pytest.raises(LearningForbiddenError):
        correct_unit(
            session,
            teacher,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["topic"],
            payload=BookUnitCorrectRequest(title="Nope"),
        )
    with pytest.raises(LearningForbiddenError):
        approve_unit(
            session,
            teacher,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["topic"],
        )


# --- correction ----------------------------------------------------------------------


def test_correction_applies_only_supplied_fields_and_resets_the_unit(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    correct_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["topic"],
        payload=BookUnitCorrectRequest(
            assessment_requirement=AssessmentRequirement.REQUIRED.value
        ),
    )
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["topic"],
    )
    before = _unit_row(session, ids["topic"])
    assert before.review_status == BookUnitReviewStatus.APPROVED.value

    result = correct_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["topic"],
        payload=BookUnitCorrectRequest(title="1.1 The Water Cycle"),
    )

    row = _unit_row(session, ids["topic"])
    assert row.title == "1.1 The Water Cycle"
    assert row.content == before.content  # untouched: not supplied
    assert row.display_order == before.display_order
    assert row.parent_unit_id == before.parent_unit_id
    # Approval never survives a correction of the approved content.
    assert row.review_status == BookUnitReviewStatus.PENDING.value
    assert row.reviewed_by_user_id is None
    assert row.reviewed_at is None
    assert result.review_status == BookUnitReviewStatus.PENDING

    events = _events(session, EVENT_UNIT_CORRECTED)
    payload = json.loads(events[-1].metadata_json)
    assert payload["fields"] == ["title"]
    assert payload["structural"] is False
    assert payload["unit_id"] == str(ids["topic"])
    assert events[-1].actor_user_id == admin.id
    assert events[-1].user_id == revision.uploaded_by_user_id
    # Identifiers and fixed codes only — never extracted text.
    assert "Water" not in events[-1].metadata_json


@pytest.mark.parametrize(
    "fields,fragment",
    [
        ({}, "no correction fields"),
        ({"content": None}, "must not be null"),
        ({"title": "   "}, "blank"),
        ({"parent_unit_id": "8ac14a4e-0000-4000-8000-000000000000"},
         "does not belong"),
        ({"source_page_start": 2, "source_page_end": 1}, "page range"),
        ({"source_page_end": 5}, "within the document's 2 pages"),
    ],
)
def test_invalid_corrections_change_nothing(
    session: Session, admin: User, fields: dict, fragment: str
) -> None:
    book, revision, ids = _seed(session, admin)
    before = _unit_row(session, ids["topic"])

    with pytest.raises(LearningValidationError, match=fragment):
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["topic"],
            payload=BookUnitCorrectRequest.model_validate(fields),
        )

    row = _unit_row(session, ids["topic"])
    assert row.title == before.title
    assert row.content == before.content
    assert row.parent_unit_id == before.parent_unit_id
    assert row.display_order == before.display_order
    assert row.review_status == before.review_status
    # Not one byte of content, review state or audit trail moved.
    assert _events(session, EVENT_UNIT_CORRECTED) == []
    assert _row(session, revision.id).status == BookRevisionStatus.PENDING_REVIEW.value


def test_the_designation_field_is_refused_on_a_non_designation_unit(
    session: Session, admin: User
) -> None:
    """An explanation carries no chapter/topic designation decision."""
    book, revision, ids = _seed(session, admin)
    before = _unit_row(session, ids["leaf"])

    with pytest.raises(LearningValidationError, match="chapter/topic units only"):
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["leaf"],
            payload=BookUnitCorrectRequest(
                assessment_requirement=AssessmentRequirement.REQUIRED.value
            ),
        )
    assert (
        _unit_row(session, ids["leaf"]).assessment_requirement
        == before.assessment_requirement
    )
    assert _events(session, EVENT_UNIT_CORRECTED) == []


def test_a_unit_cannot_be_its_own_parent(session: Session, admin: User) -> None:
    book, revision, ids = _seed(session, admin)

    with pytest.raises(LearningValidationError, match="own parent"):
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["topic"],
            payload=BookUnitCorrectRequest(parent_unit_id=ids["topic"]),
        )
    assert _events(session, EVENT_UNIT_CORRECTED) == []


def test_reparenting_into_a_descendant_is_refused_as_a_cycle(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)

    # chapter → topic would make topic its own ancestor.
    with pytest.raises(LearningValidationError, match="cycle"):
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["chapter"],
            payload=BookUnitCorrectRequest(parent_unit_id=ids["topic"]),
        )
    assert _unit_row(session, ids["chapter"]).parent_unit_id is None
    assert _events(session, EVENT_UNIT_CORRECTED) == []


def test_a_parent_only_move_is_checked_against_the_new_siblings(
    session: Session, admin: User
) -> None:
    """The topic keeps display_order 1 — but the roots already hold it."""
    book, revision, ids = _seed(session, admin)

    with pytest.raises(LearningValidationError, match="display_order 1"):
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["topic"],
            payload=BookUnitCorrectRequest(parent_unit_id=None),
        )
    assert _unit_row(session, ids["topic"]).parent_unit_id == ids["chapter"]


def test_display_order_must_be_unique_among_siblings(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)  # exercise holds root order 2

    with pytest.raises(LearningValidationError, match="display_order 1"):
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["exercise"],
            payload=BookUnitCorrectRequest(display_order=1),
        )


def test_a_unit_may_be_moved_to_the_root(session: Session, admin: User) -> None:
    book, revision, ids = _seed(session, admin)  # roots: chapter(1), exercise(2)

    result = correct_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["leaf"],
        payload=BookUnitCorrectRequest(parent_unit_id=None, display_order=3),
    )

    row = _unit_row(session, ids["leaf"])
    assert row.parent_unit_id is None
    assert row.display_order == 3
    assert row.review_status == BookUnitReviewStatus.PENDING.value
    assert result.parent_unit_id is None
    payload = json.loads(_events(session, EVENT_UNIT_CORRECTED)[0].metadata_json)
    assert payload["structural"] is True
    assert set(payload["fields"]) == {"parent_unit_id", "display_order"}


def test_structural_correction_resets_the_subtree_but_not_siblings(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    # Resolve the two designation units, then approve the whole tree.
    for key in ("chapter", "topic"):
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids[key],
            payload=BookUnitCorrectRequest(
                assessment_requirement=AssessmentRequirement.REQUIRED.value
            ),
        )
    for key in ("chapter", "topic", "leaf", "exercise"):
        approve_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids[key],
        )

    correct_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["topic"],
        payload=BookUnitCorrectRequest(kind=BookUnitKind.CHAPTER.value),
    )

    assert _unit_row(session, ids["topic"]).review_status == (
        BookUnitReviewStatus.PENDING.value
    )
    assert _unit_row(session, ids["leaf"]).review_status == (
        BookUnitReviewStatus.PENDING.value
    )
    # Ancestors and unrelated siblings keep their approval.
    assert _unit_row(session, ids["chapter"]).review_status == (
        BookUnitReviewStatus.APPROVED.value
    )
    assert _unit_row(session, ids["exercise"]).review_status == (
        BookUnitReviewStatus.APPROVED.value
    )


def test_structural_correction_resolves_the_structural_warning(
    session: Session, admin: User
) -> None:
    tree = tuple(
        {
            **spec,
            "warnings": WARNING_UNRESOLVED_STRUCTURE
            if spec["key"] == "chapter"
            else spec["warnings"],
        }
        for spec in DEFAULT_TREE
    )
    book, revision, ids = _seed(session, admin, tree=tree)

    # Approval ALONE never erases where the parser struggled.
    correct_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["chapter"],
        payload=BookUnitCorrectRequest(
            assessment_requirement=AssessmentRequirement.REQUIRED.value
        ),
    )
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["chapter"],
    )
    assert _unit_row(session, ids["chapter"]).extraction_warnings == (
        WARNING_UNRESOLVED_STRUCTURE
    )

    # Re-asserting the structure IS the resolution.
    correct_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["chapter"],
        payload=BookUnitCorrectRequest(kind=BookUnitKind.CHAPTER.value),
    )
    assert _unit_row(session, ids["chapter"]).extraction_warnings is None


def test_a_content_correction_keeps_the_warning_codes_visible(
    session: Session, admin: User
) -> None:
    tree = tuple(
        {
            **spec,
            "warnings": WARNING_UNRESOLVED_STRUCTURE
            if spec["key"] == "chapter"
            else spec["warnings"],
        }
        for spec in DEFAULT_TREE
    )
    book, revision, ids = _seed(session, admin, tree=tree)

    correct_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["chapter"],
        payload=BookUnitCorrectRequest(content="Rewritten introduction."),
    )
    assert _unit_row(session, ids["chapter"]).extraction_warnings == (
        WARNING_UNRESOLVED_STRUCTURE
    )


def test_correction_of_an_unknown_or_foreign_unit_is_the_same_404(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    other_book, other_revision, other_ids = _seed(session, admin)

    with pytest.raises(LearningNotFoundError) as unknown:
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=uuid.uuid4(),
            payload=BookUnitCorrectRequest(title="x"),
        )
    with pytest.raises(LearningNotFoundError) as foreign:
        correct_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=other_ids["topic"],  # exists — in the OTHER revision
            payload=BookUnitCorrectRequest(title="x"),
        )
    for exc in (unknown.value, foreign.value):
        assert exc.status_code == 404
        assert str(exc).startswith("no book unit with id ")
    assert other_book.id != book.id
    assert _events(session, EVENT_UNIT_CORRECTED) == []


def test_correction_of_a_nonpending_revision_is_409(
    session: Session, admin: User
) -> None:
    for status in (
        BookRevisionStatus.PUBLISHED.value,
        BookRevisionStatus.REJECTED.value,
        BookRevisionStatus.UPLOADED.value,
    ):
        book, revision, ids = _seed(session, admin, status=status)
        with pytest.raises(LearningConflictError, match="pending_review"):
            correct_unit(
                session,
                admin,
                book_id=book.id,
                revision_id=revision.id,
                unit_id=ids["topic"],
                payload=BookUnitCorrectRequest(title="x"),
            )
        assert _unit_row(session, ids["topic"]).title == "1.1 Water Cycle"


# --- per-unit approval ----------------------------------------------------------------


def test_unit_approval_records_reviewer_and_timestamp(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)

    result = approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["leaf"],
    )

    row = _unit_row(session, ids["leaf"])
    assert row.review_status == BookUnitReviewStatus.APPROVED.value
    assert row.reviewed_by_user_id == admin.id
    assert row.reviewed_at is not None
    assert result.reviewed_by_user_id == admin.id

    events = _events(session, EVENT_UNIT_APPROVED)
    assert len(events) == 1
    assert events[0].actor_user_id == admin.id
    assert json.loads(events[0].metadata_json)["unit_id"] == str(ids["leaf"])


def test_a_double_unit_approval_is_409(session: Session, admin: User) -> None:
    book, revision, ids = _seed(session, admin)
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["leaf"],
    )

    with pytest.raises(LearningConflictError, match="already approved"):
        approve_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["leaf"],
        )
    assert len(_events(session, EVENT_UNIT_APPROVED)) == 1


def test_a_chapter_or_topic_with_an_unresolved_designation_cannot_be_approved(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)

    with pytest.raises(LearningValidationError, match="assessment designation"):
        approve_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["chapter"],
        )
    assert _unit_row(session, ids["chapter"]).review_status == (
        BookUnitReviewStatus.PENDING.value
    )
    assert _events(session, EVENT_UNIT_APPROVED) == []

    # Resolving the designation first unblocks the approval.
    correct_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["chapter"],
        payload=BookUnitCorrectRequest(
            assessment_requirement=AssessmentRequirement.REQUIRED.value
        ),
    )
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["chapter"],
    )
    assert _unit_row(session, ids["chapter"]).review_status == (
        BookUnitReviewStatus.APPROVED.value
    )


def test_leaf_units_bypass_the_designation_gate(
    session: Session, admin: User
) -> None:
    """Only chapter/topic units carry a designation decision to review."""
    tree = tuple(
        {
            **spec,
            "assessment": (
                AssessmentRequirement.UNRESOLVED.value
                if spec["key"] == "leaf"
                else spec["assessment"]
            ),
        }
        for spec in DEFAULT_TREE
    )
    book, revision, ids = _seed(session, admin, tree=tree)

    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["leaf"],
    )
    assert _unit_row(session, ids["leaf"]).review_status == (
        BookUnitReviewStatus.APPROVED.value
    )


def test_approving_an_unknown_or_foreign_unit_is_404(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    _, _, other_ids = _seed(session, admin)

    with pytest.raises(LearningNotFoundError):
        approve_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=uuid.uuid4(),
        )
    with pytest.raises(LearningNotFoundError):
        approve_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=other_ids["topic"],
        )
    assert _events(session, EVENT_UNIT_APPROVED) == []


def test_unit_approval_is_409_while_the_revision_is_not_pending(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(
        session, admin, status=BookRevisionStatus.PUBLISHED.value
    )
    with pytest.raises(LearningConflictError, match="pending_review"):
        approve_unit(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["leaf"],
        )


# --- audit contract --------------------------------------------------------------------


def test_all_event_names_fit_the_audit_column() -> None:
    for name in (
        EVENT_UNIT_CORRECTED,
        EVENT_UNIT_APPROVED,
        EVENT_REVISION_APPROVED,
        EVENT_REVISION_REJECTED,
        EVENT_REVISION_CORRECTED,
    ):
        assert len(name) <= 32, name
        assert name.replace("_", "").isalnum()
