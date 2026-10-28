"""Unit tests: administrator question authoring and publication gates (book P4).

In-memory SQLite (model CHECKs and partial unique indexes are real
here), no HTTP — the administrator question surface:

- **listing** exposes one unit's questions in sibling order with options
  and answer-key state (admin only), and is a pure read;
- **creation** validates EVERYTHING before writing (non-blank prompt and
  explanation, option count per kind, distinct non-blank texts, unique
  sibling orders, unique question ``display_order``), writes every
  option with ``is_correct=False`` — correctness is never part of
  creation — resets the owning unit to ``pending`` and audits exactly
  one identifiers-only event;
- **correction** applies only supplied fields (empty body → 422),
  replaces the WHOLE option set when ``options`` is sent (fresh rows,
  correctness cleared), clears the answer key and resets the unit; a
  refused correction leaves content, key, review state and audit intact;
- **deletion** removes the question together with its options and
  resets the owning unit;
- **verification** names exactly one correct option (an option of
  another question answers the SAME 404 as an unknown id), records the
  verifier and timestamp, and re-verification moves the key;
- **thresholds** persist 1-100 / NULL (the grading default is 70), any
  effective change resets review while setting the stored value again
  is an idempotent no-op, and only exercise sets of pending revisions
  can be configured;
- **guards**: every operation is administrator-only (403), every
  mutation refuses a non-pending revision (409) with nothing written,
  and unknown/foreign ids answer the SAME 404;
- **publication** refuses a revision whose questions are unverified or
  whose required exercise set has no questions — and only then, once
  fixed, publishes.

Audit payloads carry identifiers, counts and fixed codes only — never
question, explanation or option text — and every accepted operation
writes exactly one event whose name fits the 32-char column.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.book import Book
from app.models.book_question import BookQuestion
from app.models.book_question_option import BookQuestionOption
from app.models.book_revision import BookRevision
from app.models.book_revision_context import BookRevisionContext
from app.models.book_unit import BookUnit
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    AssessmentRequirement,
    BookQuestionType,
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
from app.schemas.book_assessment import (
    BookQuestionCorrectRequest,
    BookQuestionCreate,
    BookQuestionOptionCreate,
    BookQuestionVerifyRequest,
    BookUnitThresholdRequest,
)
from app.services.book_assessment_service import EVENT_ATTEMPT_RECORDED
from app.services.book_question_service import (
    DEFAULT_PASSING_THRESHOLD,
    EVENT_ANSWER_KEY_VERIFIED,
    EVENT_QUESTION_CORRECTED,
    EVENT_QUESTION_CREATED,
    EVENT_QUESTION_DELETED,
    EVENT_UNIT_THRESHOLD_SET,
    correct_question,
    create_question,
    delete_question,
    list_questions,
    set_unit_threshold,
    verify_question,
)
from app.services.book_review_service import (
    EVENT_REVISION_APPROVED,
    approve_revision,
    approve_unit,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)

PASSWORD = "correct horse battery staple"
PAGE_COUNT = 2

PROMPT = "Photosynthesis needs light energy"
EXPLANATION = "Chlorophyll absorbs red and blue light"
OPTION_TEXTS = ("glucose", "water", "oxygen", "carbon dioxide")


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


@pytest.fixture()
def student(session: Session) -> User:
    user = _make_user(UserRole.STUDENT.value)
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


def publish_ready_tree(
    *, exercise: str = AssessmentRequirement.NOT_REQUIRED.value
) -> tuple[dict, ...]:
    """The default tree with designations resolved and every unit approved."""
    return tuple(
        {
            **spec,
            "assessment": (
                AssessmentRequirement.REQUIRED.value
                if spec["kind"] in ("chapter", "topic")
                else exercise
                if spec["key"] == "exercise"
                else spec["assessment"]
            ),
            "review": BookUnitReviewStatus.APPROVED.value,
        }
        for spec in DEFAULT_TREE
    )


def _seed(
    session: Session,
    admin: User,
    *,
    tree: tuple[dict, ...] = DEFAULT_TREE,
    status: str = BookRevisionStatus.PENDING_REVIEW.value,
):
    """One book, one revision in ``status``, its context link and a tree.

    Returns ``(book, revision, ids)`` where ``ids`` maps tree keys to
    committed unit ids.
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


# --- row readers ----------------------------------------------------------------------


def _row(session: Session, revision_id) -> BookRevision:
    session.expire_all()
    return session.get(BookRevision, revision_id)


def _unit_row(session: Session, unit_id) -> BookUnit:
    session.expire_all()
    return session.get(BookUnit, unit_id)


def _question_row(session: Session, question_id) -> BookQuestion | None:
    session.expire_all()
    return session.get(BookQuestion, question_id)


def _option_rows(session: Session, question_id) -> list[BookQuestionOption]:
    session.expire_all()
    return list(
        session.scalars(
            select(BookQuestionOption)
            .where(BookQuestionOption.book_question_id == question_id)
            .order_by(BookQuestionOption.display_order)
        )
    )


def _question_count(session: Session, revision_id) -> int:
    session.expire_all()
    return len(
        list(
            session.scalars(
                select(BookQuestion).where(
                    BookQuestion.book_revision_id == revision_id
                )
            )
        )
    )


def _events(session: Session, event_type: str) -> list[AuthEvent]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuthEvent).where(AuthEvent.event_type == event_type)
        )
    )


def _audit_blob(session: Session) -> str:
    session.expire_all()
    return "\n".join(
        event.metadata_json or ""
        for event in session.scalars(select(AuthEvent))
    )


# --- payload builders ------------------------------------------------------------------


def _options(*texts: str) -> list[BookQuestionOptionCreate]:
    return [
        BookQuestionOptionCreate(option_text=text, display_order=index)
        for index, text in enumerate(texts, start=1)
    ]


def _mc(
    order: int = 1,
    *,
    prompt: str = PROMPT,
    explanation: str = EXPLANATION,
    texts: tuple[str, ...] = OPTION_TEXTS,
) -> BookQuestionCreate:
    return BookQuestionCreate(
        question_type=BookQuestionType.MULTIPLE_CHOICE,
        prompt=prompt,
        explanation=explanation,
        display_order=order,
        options=_options(*texts),
    )


def _tf(order: int = 1, *, texts: tuple[str, ...] = ("True", "False")):
    return BookQuestionCreate(
        question_type=BookQuestionType.TRUE_FALSE,
        prompt="Water boils at 100 degrees Celsius",
        explanation="At standard atmospheric pressure.",
        display_order=order,
        options=_options(*texts),
    )


def _create(
    session: Session, admin: User, book, revision, unit_id, payload=None
):
    return create_question(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=unit_id,
        payload=payload or _mc(),
    )


def _verify(
    session: Session, admin: User, book, revision, question_id, option_id
):
    return verify_question(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        question_id=question_id,
        payload=BookQuestionVerifyRequest(correct_option_id=option_id),
    )


# --- listing ---------------------------------------------------------------------------


def test_listing_is_empty_then_returns_every_question_in_sibling_order(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)

    assert (
        list_questions(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["exercise"],
        )
        == []
    )

    second = _create(session, admin, book, revision, ids["exercise"], _mc(order=2))
    first = _create(session, admin, book, revision, ids["exercise"], _mc(order=1))

    reads = list_questions(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
    )
    assert [read.question_id for read in reads] == [
        first.question_id,
        second.question_id,
    ]
    assert [read.display_order for read in reads] == [1, 2]
    # The answer key and the verifier state are visible to the admin —
    # still NULL / all-wrong before any verification.
    assert all(read.answer_key_verified_at is None for read in reads)
    assert all(
        option.is_correct is False for read in reads for option in read.options
    )
    # Listing is a pure read: only the two creation events exist.
    assert len(_events(session, EVENT_QUESTION_CREATED)) == 2


# --- creation --------------------------------------------------------------------------


def test_creation_persists_unverified_options_resets_the_unit_and_audits_once(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
    )

    read = _create(
        session,
        admin,
        book,
        revision,
        ids["exercise"],
        _mc(prompt=f"  {PROMPT}  ", explanation=f" {EXPLANATION} "),
    )

    row = _question_row(session, read.question_id)
    assert row.prompt == PROMPT
    assert row.explanation == EXPLANATION
    assert row.question_type == BookQuestionType.MULTIPLE_CHOICE.value
    assert row.display_order == 1
    assert row.answer_key_verified_by_user_id is None
    assert row.answer_key_verified_at is None

    options = _option_rows(session, read.question_id)
    assert [option.option_text for option in options] == list(OPTION_TEXTS)
    assert [option.display_order for option in options] == [1, 2, 3, 4]
    # Correctness is NEVER part of creation: not one option is keyed.
    assert not any(option.is_correct for option in options)

    unit = _unit_row(session, ids["exercise"])
    assert unit.review_status == BookUnitReviewStatus.PENDING.value
    assert unit.reviewed_by_user_id is None
    assert unit.reviewed_at is None

    events = _events(session, EVENT_QUESTION_CREATED)
    assert len(events) == 1
    payload = json.loads(events[0].metadata_json)
    assert payload == {
        "book_id": str(book.id),
        "book_revision_id": str(revision.id),
        "book_unit_id": str(ids["exercise"]),
        "question_id": str(read.question_id),
        "question_type": "multiple_choice",
        "option_count": 4,
    }
    assert events[0].actor_user_id == admin.id
    assert events[0].user_id == revision.uploaded_by_user_id


def test_the_audit_trail_never_carries_question_explanation_or_option_text(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    _verify(
        session,
        admin,
        book,
        revision,
        created.question_id,
        created.options[0].option_id,
    )

    blob = _audit_blob(session)
    for secret in (PROMPT, EXPLANATION, *OPTION_TEXTS, "solve", "Solve"):
        assert secret not in blob
    assert len(_events(session, EVENT_ANSWER_KEY_VERIFIED)) == 1


def test_true_false_requires_exactly_two_options(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)

    with pytest.raises(LearningValidationError, match="exactly two options"):
        _create(
            session,
            admin,
            book,
            revision,
            ids["exercise"],
            _tf(texts=("True", "False", "Maybe")),
        )
    # The schema refuses a single option before the service ever runs.
    with pytest.raises(ValidationError):
        _tf(texts=("True",))

    created = _create(
        session, admin, book, revision, ids["exercise"], _tf(order=1)
    )
    assert created.question_type == BookQuestionType.TRUE_FALSE
    assert len(created.options) == 2
    # Both refusals wrote nothing: one question, one creation event.
    assert _question_count(session, revision.id) == 1
    assert len(_events(session, EVENT_QUESTION_CREATED)) == 1


def test_creation_validates_all_content_before_writing_anything(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]

    cases = [
        (_mc(prompt="   "), "the question prompt cannot be blank"),
        (_mc(explanation="   "), "the question explanation cannot be blank"),
        (
            _mc(texts=("glucose", "  ", "oxygen", "carbon dioxide")),
            "option texts cannot be blank",
        ),
        (
            _mc(texts=("glucose", "glucose", "oxygen", "carbon dioxide")),
            "option texts must be distinct within a question",
        ),
        (
            BookQuestionCreate(
                question_type=BookQuestionType.MULTIPLE_CHOICE,
                prompt=PROMPT,
                explanation=EXPLANATION,
                display_order=1,
                options=[
                    BookQuestionOptionCreate(option_text="first", display_order=1),
                    BookQuestionOptionCreate(option_text="second", display_order=1),
                ],
            ),
            "duplicate option display_order",
        ),
    ]
    for payload, message in cases:
        with pytest.raises(LearningValidationError, match=message):
            _create(session, admin, book, revision, unit_id, payload)
    assert _question_count(session, revision.id) == 0
    assert _events(session, EVENT_QUESTION_CREATED) == []

    # A duplicate QUESTION display_order is refused after the first
    # question exists — the first one stays the only row and event.
    _create(session, admin, book, revision, unit_id, _mc(order=1))
    with pytest.raises(
        LearningValidationError, match="already exists in this exercise set"
    ):
        _create(session, admin, book, revision, unit_id, _mc(order=1))
    assert _question_count(session, revision.id) == 1
    assert len(_events(session, EVENT_QUESTION_CREATED)) == 1


# --- correction ------------------------------------------------------------------------


def test_correction_applies_only_the_supplied_field_and_clears_the_key(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    option_ids = [option.option_id for option in created.options]
    _verify(session, admin, book, revision, created.question_id, option_ids[1])

    corrected = correct_question(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        question_id=created.question_id,
        payload=BookQuestionCorrectRequest(prompt="  Why do leaves turn yellow?  "),
    )

    assert corrected.prompt == "Why do leaves turn yellow?"
    assert corrected.explanation == EXPLANATION
    assert corrected.display_order == 1
    # Options are untouched: same rows, and the key is cleared.
    assert [option.option_id for option in corrected.options] == option_ids
    assert all(option.is_correct is False for option in corrected.options)
    assert corrected.answer_key_verified_by_user_id is None
    assert corrected.answer_key_verified_at is None

    row = _question_row(session, created.question_id)
    assert row.prompt == "Why do leaves turn yellow?"
    assert row.answer_key_verified_at is None
    unit = _unit_row(session, ids["exercise"])
    assert unit.review_status == BookUnitReviewStatus.PENDING.value

    events = _events(session, EVENT_QUESTION_CORRECTED)
    assert len(events) == 1
    payload = json.loads(events[0].metadata_json)
    assert payload["fields"] == ["prompt"]
    assert payload["question_id"] == str(created.question_id)
    assert events[0].actor_user_id == admin.id


def test_correction_replaces_the_whole_option_set_with_fresh_unkeyed_rows(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    old_option_ids = [option.option_id for option in created.options]
    _verify(
        session, admin, book, revision, created.question_id, old_option_ids[0]
    )

    corrected = correct_question(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        question_id=created.question_id,
        payload=BookQuestionCorrectRequest(
            options=_options("chlorophyll", "stomata", "xylem")
        ),
    )

    assert [option.option_text for option in corrected.options] == [
        "chlorophyll",
        "stomata",
        "xylem",
    ]
    new_ids = [option.option_id for option in corrected.options]
    assert not set(new_ids) & set(old_option_ids)
    assert all(option.is_correct is False for option in corrected.options)
    assert corrected.answer_key_verified_at is None
    for old_id in old_option_ids:
        assert session.get(BookQuestionOption, old_id) is None
    assert len(_option_rows(session, created.question_id)) == 3

    payload = json.loads(
        _events(session, EVENT_QUESTION_CORRECTED)[0].metadata_json
    )
    assert payload["fields"] == ["options"]


def test_a_refused_correction_changes_nothing_at_the_transaction_boundary(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    option_ids = [option.option_id for option in created.options]
    _verify(session, admin, book, revision, created.question_id, option_ids[0])
    # Each accepted operation is its own committed request.
    session.commit()
    before = _question_row(session, created.question_id)
    before_prompt = before.prompt

    with pytest.raises(
        LearningValidationError, match="duplicate option display_order"
    ):
        correct_question(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            question_id=created.question_id,
            payload=BookQuestionCorrectRequest(
                options=[
                    BookQuestionOptionCreate(option_text="one", display_order=1),
                    BookQuestionOptionCreate(option_text="two", display_order=1),
                ]
            ),
        )
    # Validation runs before any row is written, so even the flush the
    # endpoint would roll back has nothing to undo.
    session.rollback()

    row = _question_row(session, created.question_id)
    assert row.prompt == before_prompt
    assert row.answer_key_verified_at is not None
    assert [option.id for option in _option_rows(session, created.question_id)] == (
        option_ids
    )
    assert _events(session, EVENT_QUESTION_CORRECTED) == []
    assert len(_events(session, EVENT_QUESTION_CREATED)) == 1
    assert len(_events(session, EVENT_ANSWER_KEY_VERIFIED)) == 1


def test_correction_refuses_empty_null_and_blank_bodies(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    option_ids = [option.option_id for option in created.options]
    _verify(session, admin, book, revision, created.question_id, option_ids[0])

    refusals = [
        (BookQuestionCorrectRequest(), "no correction fields supplied"),
        (BookQuestionCorrectRequest(prompt=None), "prompt cannot be null"),
        (
            BookQuestionCorrectRequest(prompt="   "),
            "the question prompt cannot be blank",
        ),
        (
            BookQuestionCorrectRequest(explanation=None),
            "explanation cannot be null",
        ),
        (
            BookQuestionCorrectRequest(display_order=None),
            "display_order cannot be null",
        ),
        (
            BookQuestionCorrectRequest(options=None),
            "options cannot be null",
        ),
    ]
    for payload, message in refusals:
        with pytest.raises(LearningValidationError, match=re.escape(message)):
            correct_question(
                session,
                admin,
                book_id=book.id,
                revision_id=revision.id,
                question_id=created.question_id,
                payload=payload,
            )

    row = _question_row(session, created.question_id)
    assert row.prompt == PROMPT
    assert row.answer_key_verified_at is not None
    assert [option.id for option in _option_rows(session, created.question_id)] == (
        option_ids
    )
    assert _events(session, EVENT_QUESTION_CORRECTED) == []


# --- deletion --------------------------------------------------------------------------


def test_deletion_removes_the_question_with_its_options_and_resets_review(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
    )
    created = _create(session, admin, book, revision, ids["exercise"])
    option_ids = [option.option_id for option in created.options]
    _verify(session, admin, book, revision, created.question_id, option_ids[0])

    delete_question(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        question_id=created.question_id,
    )

    assert _question_row(session, created.question_id) is None
    for option_id in option_ids:
        assert session.get(BookQuestionOption, option_id) is None
    assert (
        list_questions(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["exercise"],
        )
        == []
    )
    unit = _unit_row(session, ids["exercise"])
    assert unit.review_status == BookUnitReviewStatus.PENDING.value
    assert unit.reviewed_by_user_id is None

    events = _events(session, EVENT_QUESTION_DELETED)
    assert len(events) == 1
    payload = json.loads(events[0].metadata_json)
    assert payload["question_id"] == str(created.question_id)
    assert set(payload) == {
        "book_id",
        "book_revision_id",
        "book_unit_id",
        "question_id",
    }
    assert events[0].actor_user_id == admin.id


# --- verification ----------------------------------------------------------------------


def test_verification_exposes_exactly_one_correct_option_and_records_who(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    option_ids = [option.option_id for option in created.options]

    read = _verify(
        session, admin, book, revision, created.question_id, option_ids[2]
    )

    assert [option.option_id for option in read.options if option.is_correct] == [
        option_ids[2]
    ]
    assert read.answer_key_verified_by_user_id == admin.id
    assert read.answer_key_verified_at is not None

    row = _question_row(session, created.question_id)
    assert row.answer_key_verified_by_user_id == admin.id
    assert row.answer_key_verified_at is not None
    keyed = [o for o in _option_rows(session, created.question_id) if o.is_correct]
    assert [o.id for o in keyed] == [option_ids[2]]

    events = _events(session, EVENT_ANSWER_KEY_VERIFIED)
    assert len(events) == 1
    payload = json.loads(events[0].metadata_json)
    assert payload["correct_option_id"] == str(option_ids[2])
    assert payload["question_id"] == str(created.question_id)
    assert events[0].actor_user_id == admin.id


def test_reverification_moves_the_key_and_never_leaves_two(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    option_ids = [option.option_id for option in created.options]

    _verify(session, admin, book, revision, created.question_id, option_ids[0])
    read = _verify(
        session, admin, book, revision, created.question_id, option_ids[3]
    )

    assert [option.option_id for option in read.options if option.is_correct] == [
        option_ids[3]
    ]
    assert read.answer_key_verified_by_user_id == admin.id
    assert len(_events(session, EVENT_ANSWER_KEY_VERIFIED)) == 2


def test_an_option_of_another_question_answers_the_same_404_as_unknown(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    first = _create(session, admin, book, revision, ids["exercise"], _mc(order=1))
    second = _create(session, admin, book, revision, ids["exercise"], _mc(order=2))
    other_book, other_revision, other_ids = _seed(session, admin)
    foreign = _create(
        session, admin, other_book, other_revision, other_ids["exercise"]
    )
    unknown_option_id = uuid.uuid4()
    bad_options = (
        unknown_option_id,
        second.options[0].option_id,
        foreign.options[0].option_id,
    )

    messages = []
    for bad_option in bad_options:
        with pytest.raises(LearningNotFoundError) as exc:
            _verify(
                session,
                admin,
                book,
                revision,
                first.question_id,
                bad_option,
            )
        assert exc.value.status_code == 404
        message = str(exc.value)
        # Unknown, sibling-question and foreign-revision options all
        # answer the SAME non-disclosing 404 for THIS question.
        assert message.startswith("no option ")
        assert message.endswith(f"of question {first.question_id}")
        assert str(bad_option) in message
        messages.append(message)
    assert len(set(messages)) == 3

    # The failed attempts wrote nothing: still unverified, no events.
    row = _question_row(session, first.question_id)
    assert row.answer_key_verified_at is None
    assert not any(
        o.is_correct for o in _option_rows(session, first.question_id)
    )
    assert _events(session, EVENT_ANSWER_KEY_VERIFIED) == []
    assert len(_events(session, EVENT_QUESTION_CREATED)) == 3


# --- thresholds ------------------------------------------------------------------------


def test_threshold_change_persists_resets_review_and_audits_once(
    session: Session, admin: User
) -> None:
    assert DEFAULT_PASSING_THRESHOLD == 70
    book, revision, ids = _seed(session, admin)
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
    )

    read = set_unit_threshold(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
        payload=BookUnitThresholdRequest(passing_threshold_percent=85),
    )

    assert read.passing_threshold_percent == 85
    unit = _unit_row(session, ids["exercise"])
    assert unit.passing_threshold_percent == 85
    assert unit.review_status == BookUnitReviewStatus.PENDING.value
    events = _events(session, EVENT_UNIT_THRESHOLD_SET)
    assert len(events) == 1
    payload = json.loads(events[0].metadata_json)
    assert payload["passing_threshold_percent"] == 85
    assert payload["book_unit_id"] == str(ids["exercise"])
    assert events[0].actor_user_id == admin.id


def test_threshold_is_idempotent_and_null_restores_the_default(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
    )
    assert _unit_row(session, ids["exercise"]).passing_threshold_percent is None

    set_unit_threshold(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
        payload=BookUnitThresholdRequest(passing_threshold_percent=85),
    )
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
    )

    # Setting the stored value again is a no-op: no reset, no audit.
    read = set_unit_threshold(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
        payload=BookUnitThresholdRequest(passing_threshold_percent=85),
    )
    assert read.passing_threshold_percent == 85
    unit = _unit_row(session, ids["exercise"])
    assert unit.review_status == BookUnitReviewStatus.APPROVED.value
    assert unit.reviewed_by_user_id == admin.id
    assert len(_events(session, EVENT_UNIT_THRESHOLD_SET)) == 1

    # NULL restores the stored default (NULL → grading uses 70).
    read = set_unit_threshold(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
        payload=BookUnitThresholdRequest(passing_threshold_percent=None),
    )
    assert read.passing_threshold_percent is None
    assert _unit_row(session, ids["exercise"]).passing_threshold_percent is None
    assert (
        _unit_row(session, ids["exercise"]).review_status
        == BookUnitReviewStatus.PENDING.value
    )
    assert len(_events(session, EVENT_UNIT_THRESHOLD_SET)) == 2


# --- guards -----------------------------------------------------------------------------


def test_every_question_operation_requires_an_administrator(
    session: Session, admin: User, teacher: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    events_before = len(_events(session, EVENT_QUESTION_CREATED))

    for actor in (teacher, student):
        with pytest.raises(
            LearningForbiddenError, match="administrator role required"
        ):
            list_questions(
                session,
                actor,
                book_id=book.id,
                revision_id=revision.id,
                unit_id=ids["exercise"],
            )
        with pytest.raises(
            LearningForbiddenError, match="administrator role required"
        ):
            _create(session, actor, book, revision, ids["exercise"])
        with pytest.raises(
            LearningForbiddenError, match="administrator role required"
        ):
            correct_question(
                session,
                actor,
                book_id=book.id,
                revision_id=revision.id,
                question_id=created.question_id,
                payload=BookQuestionCorrectRequest(prompt="Nope"),
            )
        with pytest.raises(
            LearningForbiddenError, match="administrator role required"
        ):
            delete_question(
                session,
                actor,
                book_id=book.id,
                revision_id=revision.id,
                question_id=created.question_id,
            )
        with pytest.raises(
            LearningForbiddenError, match="administrator role required"
        ):
            _verify(
                session,
                actor,
                book,
                revision,
                created.question_id,
                created.options[0].option_id,
            )
        with pytest.raises(
            LearningForbiddenError, match="administrator role required"
        ):
            set_unit_threshold(
                session,
                actor,
                book_id=book.id,
                revision_id=revision.id,
                unit_id=ids["exercise"],
                payload=BookUnitThresholdRequest(passing_threshold_percent=50),
            )

    row = _question_row(session, created.question_id)
    assert row.prompt == PROMPT
    assert row.answer_key_verified_at is None
    assert _unit_row(session, ids["exercise"]).passing_threshold_percent is None
    assert len(_events(session, EVENT_QUESTION_CREATED)) == events_before
    assert _events(session, EVENT_QUESTION_CORRECTED) == []
    assert _events(session, EVENT_QUESTION_DELETED) == []
    assert _events(session, EVENT_ANSWER_KEY_VERIFIED) == []
    assert _events(session, EVENT_UNIT_THRESHOLD_SET) == []


def test_every_mutation_is_refused_unless_the_revision_is_pending(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    created = _create(session, admin, book, revision, ids["exercise"])
    option_id = created.options[0].option_id

    revision_row = _row(session, revision.id)
    revision_row.status = BookRevisionStatus.PUBLISHED.value
    session.commit()

    mutations = [
        lambda actor: _create(session, actor, book, revision, ids["exercise"]),
        lambda actor: correct_question(
            session,
            actor,
            book_id=book.id,
            revision_id=revision.id,
            question_id=created.question_id,
            payload=BookQuestionCorrectRequest(prompt="Nope"),
        ),
        lambda actor: delete_question(
            session,
            actor,
            book_id=book.id,
            revision_id=revision.id,
            question_id=created.question_id,
        ),
        lambda actor: _verify(
            session, actor, book, revision, created.question_id, option_id
        ),
        lambda actor: set_unit_threshold(
            session,
            actor,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["exercise"],
            payload=BookUnitThresholdRequest(passing_threshold_percent=50),
        ),
    ]
    for call in mutations:
        with pytest.raises(
            LearningConflictError, match="current status: published"
        ):
            call(admin)

    # Reads still work on published history — only writes are gated.
    assert len(
        list_questions(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["exercise"],
        )
    ) == 1
    assert _question_count(session, revision.id) == 1
    row = _question_row(session, created.question_id)
    assert row.prompt == PROMPT
    assert row.answer_key_verified_at is None
    assert _unit_row(session, ids["exercise"]).passing_threshold_percent is None
    assert len(_events(session, EVENT_QUESTION_CREATED)) == 1
    assert _events(session, EVENT_QUESTION_CORRECTED) == []
    assert _events(session, EVENT_QUESTION_DELETED) == []
    assert _events(session, EVENT_ANSWER_KEY_VERIFIED) == []
    assert _events(session, EVENT_UNIT_THRESHOLD_SET) == []

    revision_row = _row(session, revision.id)
    revision_row.status = BookRevisionStatus.REJECTED.value
    revision_row.rejection_reason = "wrong textbook"
    session.commit()
    with pytest.raises(LearningConflictError, match="current status: rejected"):
        _create(session, admin, book, revision, ids["exercise"])


def test_unknown_and_foreign_ids_answer_the_same_404(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)
    _create(session, admin, book, revision, ids["exercise"])
    other_book, other_revision, other_ids = _seed(session, admin)
    foreign = _create(
        session, admin, other_book, other_revision, other_ids["exercise"]
    )

    # An unknown question id and a REAL question of another book answer
    # the same message — only the id differs, nothing is disclosed.
    unknown_id = uuid.uuid4()
    with pytest.raises(LearningNotFoundError) as unknown_question:
        correct_question(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            question_id=unknown_id,
            payload=BookQuestionCorrectRequest(prompt="x"),
        )
    with pytest.raises(LearningNotFoundError) as foreign_question:
        correct_question(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            question_id=foreign.question_id,
            payload=BookQuestionCorrectRequest(prompt="x"),
        )
    for exc, the_id in (
        (unknown_question.value, unknown_id),
        (foreign_question.value, foreign.question_id),
    ):
        assert exc.status_code == 404
        assert str(exc).startswith("no book question with id ")
        assert str(exc).endswith(str(the_id))
    assert str(unknown_question.value).removesuffix(
        str(unknown_id)
    ) == str(foreign_question.value).removesuffix(str(foreign.question_id))

    unknown_revision_id = uuid.uuid4()
    with pytest.raises(LearningNotFoundError) as unknown_revision:
        list_questions(
            session,
            admin,
            book_id=book.id,
            revision_id=unknown_revision_id,
            unit_id=ids["exercise"],
        )
    with pytest.raises(LearningNotFoundError) as foreign_revision:
        list_questions(
            session,
            admin,
            book_id=book.id,
            revision_id=other_revision.id,
            unit_id=other_ids["exercise"],
        )
    assert unknown_revision.value.status_code == 404
    assert foreign_revision.value.status_code == 404
    assert str(unknown_revision.value).removesuffix(
        str(unknown_revision_id)
    ) == str(foreign_revision.value).removesuffix(str(other_revision.id))
    assert str(unknown_revision.value).startswith("no book revision with id ")

    with pytest.raises(LearningNotFoundError) as unknown_unit:
        list_questions(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=uuid.uuid4(),
        )
    assert str(unknown_unit.value).startswith("no book unit with id ")

    with pytest.raises(LearningNotFoundError) as unknown_book:
        _create(session, admin, other_book, revision, ids["exercise"])
    assert str(unknown_book.value).startswith("no book revision with id ")

    # A delete against a foreign question refuses without removing it.
    with pytest.raises(LearningNotFoundError):
        delete_question(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            question_id=foreign.question_id,
        )
    assert _question_row(session, foreign.question_id) is not None
    assert _question_count(session, revision.id) == 1
    assert _events(session, EVENT_QUESTION_DELETED) == []


def test_questions_and_thresholds_are_refused_off_an_exercise_set(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin)

    with pytest.raises(
        LearningValidationError, match="authored on an exercise_set unit"
    ):
        _create(session, admin, book, revision, ids["chapter"])
    with pytest.raises(
        LearningValidationError, match="authored on an exercise_set unit"
    ):
        set_unit_threshold(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["chapter"],
            payload=BookUnitThresholdRequest(passing_threshold_percent=50),
        )
    # Listing stays unit-scoped for every kind: a chapter simply has none.
    assert (
        list_questions(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=ids["chapter"],
        )
        == []
    )
    assert _question_count(session, revision.id) == 0
    assert _events(session, EVENT_QUESTION_CREATED) == []
    assert _events(session, EVENT_UNIT_THRESHOLD_SET) == []


# --- publication gates -------------------------------------------------------------------


def test_publication_requires_a_question_on_every_required_exercise_set(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(
        session,
        admin,
        tree=publish_ready_tree(
            exercise=AssessmentRequirement.REQUIRED.value
        ),
    )

    with pytest.raises(
        LearningValidationError,
        match="required exercise sets have no objective questions",
    ):
        approve_revision(
            session, admin, book_id=book.id, revision_id=revision.id
        )

    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.PENDING_REVIEW.value
    assert row.is_current_publication is False
    assert _events(session, EVENT_REVISION_APPROVED) == []


def test_publication_refuses_unverified_keys_then_accepts_them(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(
        session,
        admin,
        tree=publish_ready_tree(
            exercise=AssessmentRequirement.REQUIRED.value
        ),
    )
    created = _create(session, admin, book, revision, ids["exercise"])
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
    )

    with pytest.raises(LearningValidationError) as refused:
        approve_revision(
            session, admin, book_id=book.id, revision_id=revision.id
        )
    assert str(refused.value) == (
        "1 of 1 questions have an unverified answer key; "
        "verify every question first"
    )
    assert _row(session, revision.id).status == (
        BookRevisionStatus.PENDING_REVIEW.value
    )
    assert _events(session, EVENT_REVISION_APPROVED) == []

    _verify(
        session, admin, book, revision, created.question_id, created.options[0].option_id
    )
    detail = approve_revision(
        session, admin, book_id=book.id, revision_id=revision.id
    )

    assert detail.status == BookRevisionStatus.PUBLISHED
    assert detail.is_current_publication is True
    row = _row(session, revision.id)
    assert row.status == BookRevisionStatus.PUBLISHED.value
    assert row.is_current_publication is True
    assert len(_events(session, EVENT_REVISION_APPROVED)) == 1


def test_publication_accepts_an_exercise_set_that_carries_no_assessment(
    session: Session, admin: User
) -> None:
    book, revision, _ = _seed(session, admin, tree=publish_ready_tree())

    detail = approve_revision(
        session, admin, book_id=book.id, revision_id=revision.id
    )

    assert detail.status == BookRevisionStatus.PUBLISHED
    assert len(_events(session, EVENT_REVISION_APPROVED)) == 1


def test_publication_refuses_unverified_questions_on_an_exempt_set(
    session: Session, admin: User
) -> None:
    book, revision, ids = _seed(session, admin, tree=publish_ready_tree())
    created = _create(session, admin, book, revision, ids["exercise"])
    approve_unit(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=ids["exercise"],
    )

    with pytest.raises(LearningValidationError) as refused:
        approve_revision(
            session, admin, book_id=book.id, revision_id=revision.id
        )
    assert "unverified answer key" in str(refused.value)
    assert _events(session, EVENT_REVISION_APPROVED) == []

    _verify(
        session, admin, book, revision, created.question_id, created.options[1].option_id
    )
    detail = approve_revision(
        session, admin, book_id=book.id, revision_id=revision.id
    )
    assert detail.status == BookRevisionStatus.PUBLISHED


# --- audit vocabulary --------------------------------------------------------------------


def test_every_assessment_event_name_fits_the_audit_column() -> None:
    for name in (
        EVENT_QUESTION_CREATED,
        EVENT_QUESTION_CORRECTED,
        EVENT_QUESTION_DELETED,
        EVENT_ANSWER_KEY_VERIFIED,
        EVENT_UNIT_THRESHOLD_SET,
        EVENT_ATTEMPT_RECORDED,
    ):
        assert len(name) <= 32, name
