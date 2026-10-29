"""Unit tests: student guided learning + progression (book P5).

In-memory SQLite (CHECKs, composite FKs and the partial unique keys are
real here), no HTTP — the durable half of the student surface:

- **access** is exact-match: the current publication resolved per book,
  the canonical context checked against it, and unknown book / missing
  publication / context mismatch all answering the SAME 404;
- **the walk** delivers ONE unit at a time in document order, a new
  student starting at the first unit; completing the current content
  unit writes one progress row + one audit event, duplicate requests
  are 409 with nothing rewritten, and arbitrary later units answer the
  same 404 as unknown ids;
- **checkpoints**: a required exercise set blocks the walk until a
  passing attempt completes it (written with the attempt), optional
  practice never blocks but is only reachable once every gating unit
  before it is completed, and an unresolved chapter/topic designation
  in a published revision fails closed (409) on the whole surface;
- **fresh editions**: progress is per-revision — publishing a new
  revision resets every walk without touching historical rows.

Attempts reuse ``book_assessment_service.record_attempt`` unchanged:
equal weighting, exact threshold comparison, explanation-only feedback,
never the answer key.
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
from app.models.student import Student
from app.models.student_book_progress import StudentBookProgress
from app.models.subject import Subject
from app.models.user import User
from app.schemas.book_assessment import AssessmentAnswerSubmit, AssessmentAttemptSubmit
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)
from app.services.student_book_service import (
    EVENT_BOOK_CHECKPOINT_PASSED,
    EVENT_BOOK_UNIT_COMPLETED,
    complete_unit,
    get_book,
    get_next_unit,
    list_books,
    read_unit,
    submit_attempt,
)

PASSWORD = "correct horse battery staple"

Q1_PROMPT = "Water boils at what temperature?"
Q1_EXPLANATION = "At standard pressure water boils at 100 degrees."
Q1_OPTIONS = ("100 degrees", "0 degrees", "50 degrees")
Q1_CORRECT = 0


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
def student(session: Session) -> User:
    user = _make_user(UserRole.STUDENT.value)
    session.add(user)
    session.flush()
    session.add(Student(user_id=user.id, full_name="Walk Student"))
    session.commit()
    return user


@pytest.fixture()
def other_student(session: Session) -> User:
    user = _make_user(UserRole.STUDENT.value)
    session.add(user)
    session.flush()
    session.add(Student(user_id=user.id, full_name="Other Student"))
    session.commit()
    return user


# --- seeding --------------------------------------------------------------------------


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


def _seed_published(
    session: Session,
    admin: User,
    *,
    tree: tuple[dict, ...],
    title: str = "Science S3",
    context: LearningContext | None = None,
):
    """One book whose CURRENT revision is published, with an approved tree.

    Returns ``(book, revision, ids, context)``; ``ids`` maps tree keys to
    the committed unit ids. Units are written with explicit ids and
    reviewer identity so the walk sees exactly what publication approved.
    A ``context`` may be supplied (e.g. one distinct from another book's);
    otherwise the shared catalog context is reused.
    """
    context = context or _catalog_context(session)
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

    book = Book(title=title, created_by_user_id=admin.id)
    session.add(book)
    session.flush()
    revision = BookRevision(
        book_id=book.id,
        revision_number=1,
        file_asset_id=asset.id,
        uploaded_by_user_id=admin.id,
        status=BookRevisionStatus.PUBLISHED.value,
        extraction_summary=json.dumps(
            {
                "page_count": 2,
                "unit_count": len(tree),
                "by_kind": {},
                "warnings": [],
            }
        ),
        is_current_publication=True,
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
                review_status=BookUnitReviewStatus.APPROVED.value,
                reviewed_by_user_id=admin.id,
                reviewed_at=now,
            )
        )
    session.commit()
    return book, revision, ids, context


def _unit(
    key: str,
    kind: BookUnitKind,
    *,
    parent: str | None = None,
    order: int = 1,
    assessment: AssessmentRequirement = AssessmentRequirement.UNRESOLVED.value,
    title: str | None = None,
    content: str = "Extracted content.",
) -> dict:
    return {
        "key": key,
        "kind": kind.value,
        "title": title or f"{key} title",
        "content": content,
        "parent": parent,
        "order": order,
        "start": 1,
        "end": 1,
        "assessment": assessment,
        "warnings": None,
    }


def _add_question(
    session: Session,
    admin: User,
    revision: BookRevision,
    unit: BookUnit,
    *,
    options: tuple[str, ...] = Q1_OPTIONS,
    correct_index: int = Q1_CORRECT,
) -> tuple[BookQuestion, BookQuestionOption]:
    """One verified multiple-choice question on the exercise set."""
    question = BookQuestion(
        book_revision_id=revision.id,
        book_unit_id=unit.id,
        question_type=BookQuestionType.MULTIPLE_CHOICE.value,
        prompt=Q1_PROMPT,
        explanation=Q1_EXPLANATION,
        display_order=1,
        answer_key_verified_by_user_id=admin.id,
        answer_key_verified_at=datetime.now(timezone.utc),
    )
    session.add(question)
    session.flush()
    correct = None
    for index, text in enumerate(options, start=1):
        option = BookQuestionOption(
            book_question_id=question.id,
            option_text=text,
            display_order=index,
            is_correct=(index - 1 == correct_index),
        )
        session.add(option)
        session.flush()
        if option.is_correct:
            correct = option
    session.commit()
    return question, correct


def _unit_options(
    session: Session, question: BookQuestion
) -> list[BookQuestionOption]:
    return list(
        session.scalars(
            select(BookQuestionOption).where(
                BookQuestionOption.book_question_id == question.id
            )
        )
    )


def _wrong_submission(
    session: Session, context: LearningContext, question: BookQuestion
) -> AssessmentAttemptSubmit:
    return AssessmentAttemptSubmit(
        learning_context_id=context.id,
        answers=[
            AssessmentAnswerSubmit(
                question_id=question.id,
                option_id=next(
                    option.id
                    for option in _unit_options(session, question)
                    if not option.is_correct
                ),
            )
        ],
    )


def _correct_submission(
    context: LearningContext, question: BookQuestion, option: BookQuestionOption
) -> AssessmentAttemptSubmit:
    return AssessmentAttemptSubmit(
        learning_context_id=context.id,
        answers=[
            AssessmentAnswerSubmit(question_id=question.id, option_id=option.id)
        ],
    )


def _events(session: Session, event_type: str) -> list[AuthEvent]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuthEvent).where(AuthEvent.event_type == event_type)
        )
    )


# --- access + listing --------------------------------------------------------------------


def test_listing_returns_only_books_valid_for_the_selected_context(
    session: Session, admin: User, student: User
) -> None:
    shared = _catalog_context(session)
    other_pathway = Pathway(code=f"OP{uuid.uuid4().hex[:4].upper()}", name="O Level")
    other_level = EducationLevel(
        code=f"OL{uuid.uuid4().hex[:4].upper()}", name="Senior 2", level_number=2
    )
    other_subject = Subject(code=f"OS{uuid.uuid4().hex[:4].upper()}", name="Physics")
    session.add_all([other_pathway, other_level, other_subject])
    session.flush()
    other_context = LearningContext(
        academic_year_id=shared.academic_year_id,
        pathway_id=other_pathway.id,
        education_level_id=other_level.id,
        subject_id=other_subject.id,
        program_version_id=None,
    )
    session.add(other_context)
    session.commit()
    book, revision, _ids, context = _seed_published(
        session,
        admin,
        tree=(_unit("chapter", BookUnitKind.CHAPTER, assessment=AssessmentRequirement.NOT_REQUIRED.value),),
    )
    _seed_published(
        session,
        admin,
        tree=(_unit("chapter", BookUnitKind.CHAPTER, assessment=AssessmentRequirement.NOT_REQUIRED.value),),
        title="Other Book",
        context=other_context,
    )

    listed = list_books(session, student, context.id)
    assert [row.book_id for row in listed] == [book.id]
    assert listed[0].published_revision_id == revision.id
    assert listed[0].total_units == 1
    assert listed[0].completed_units == 0

    # An unknown context matches nothing (and reveals no ids).
    assert list_books(session, student, uuid.uuid4()) == []


def test_access_refuses_unknown_book_missing_publication_and_context_alike(
    session: Session, admin: User, student: User
) -> None:
    _book, _revision, _ids, context = _seed_published(
        session,
        admin,
        tree=(_unit("chapter", BookUnitKind.CHAPTER, assessment=AssessmentRequirement.NOT_REQUIRED.value),),
    )
    unknown = uuid.uuid4()
    with pytest.raises(LearningNotFoundError) as unknown_error:
        get_book(session, student, unknown, context.id)
    with pytest.raises(LearningNotFoundError) as mismatch:
        get_book(session, student, unknown, uuid.uuid4())
    assert str(unknown) in str(unknown_error.value)
    assert str(unknown) in str(mismatch.value)


def test_the_guided_walk_needs_a_student_profile(
    session: Session, admin: User
) -> None:
    roleless = _make_user(UserRole.TEACHER.value)
    session.add(roleless)
    session.commit()
    with pytest.raises(LearningForbiddenError):
        list_books(session, roleless, uuid.uuid4())

    profileless = _make_user(UserRole.STUDENT.value)
    session.add(profileless)
    session.commit()
    with pytest.raises(LearningNotFoundError):
        list_books(session, profileless, uuid.uuid4())


# --- the walk -----------------------------------------------------------------------------


def _checkpoint_tree() -> tuple[dict, ...]:
    """chapter(required) → topic(required) → exercise(REQUIRED) + trailing leaf.

    The exercise set is the required checkpoint: the walk sits on it
    until a passing attempt completes it.
    """
    return (
        _unit(
            "chapter",
            BookUnitKind.CHAPTER,
            assessment=AssessmentRequirement.REQUIRED.value,
        ),
        _unit(
            "topic",
            BookUnitKind.TOPIC,
            parent="chapter",
            assessment=AssessmentRequirement.REQUIRED.value,
        ),
        _unit(
            "exercise",
            BookUnitKind.EXERCISE_SET,
            parent="topic",
            assessment=AssessmentRequirement.REQUIRED.value,
        ),
        _unit(
            "closing",
            BookUnitKind.EXPLANATION,
            parent=None,
            order=2,
        ),
    )


def test_a_new_student_begins_at_the_first_unit(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_checkpoint_tree()
    )
    _question, _option = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))

    position = get_next_unit(session, student, book.id, context.id)
    assert position.finished is False
    assert position.unit is not None
    assert position.unit.unit_id == ids["chapter"]
    assert position.unit.kind == BookUnitKind.CHAPTER
    assert position.unit.progress is None


def test_completing_the_current_unit_advances_one_step_and_audits(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_checkpoint_tree()
    )
    _question, _option = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))

    completed = complete_unit(session, student, book.id, ids["chapter"], context.id)
    assert completed.progress is not None
    assert completed.progress.completed_at is not None

    rows = session.scalars(select(StudentBookProgress)).all()
    assert len(rows) == 1
    assert rows[0].book_unit_id == ids["chapter"]
    events = _events(session, EVENT_BOOK_UNIT_COMPLETED)
    assert len(events) == 1
    assert events[0].user_id == student.id
    payload = json.loads(events[0].metadata_json)
    assert set(payload) == {"book_id", "book_revision_id", "book_unit_id"}
    assert payload["book_unit_id"] == str(ids["chapter"])

    position = get_next_unit(session, student, book.id, context.id)
    assert position.unit.unit_id == ids["topic"]


def test_incomplete_content_unit_prevents_advancement(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_checkpoint_tree()
    )
    _question, _option = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))

    with pytest.raises(LearningConflictError) as skipped:
        complete_unit(session, student, book.id, ids["closing"], context.id)
    assert "not the current available learning unit" in str(skipped.value)
    assert session.scalars(select(StudentBookProgress)).all() == []


def test_arbitrary_later_units_answer_the_same_404_as_unknown_ids(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_checkpoint_tree()
    )
    _question, _option = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))

    with pytest.raises(LearningNotFoundError) as unknown:
        read_unit(session, student, book.id, uuid.uuid4(), context.id)
    with pytest.raises(LearningNotFoundError) as later:
        read_unit(session, student, book.id, ids["topic"], context.id)
    # Same status, same message shape, each carrying only the id the
    # caller itself supplied — no way to tell which units exist.
    assert str(unknown.value).startswith("no book unit with id")
    assert str(later.value).startswith("no book unit with id")
    assert str(uuid.uuid4()) not in str(unknown.value)
    assert str(ids["topic"]) in str(later.value)


def test_completed_units_can_be_revisited_without_reopening_progression(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_checkpoint_tree()
    )
    _question, _option = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))
    first = complete_unit(session, student, book.id, ids["chapter"], context.id)
    revisit = read_unit(session, student, book.id, ids["chapter"], context.id)
    assert revisit.progress is not None
    # Same instant (the SQLite round-trip sheds the tz marker that the
    # fresh insert carried; PostgreSQL refetches stay tz-aware).
    assert revisit.progress.completed_at == first.progress.completed_at.replace(
        tzinfo=None
    ) or (
        revisit.progress.completed_at == first.progress.completed_at
    )
    # The walk did not move: the current unit is still the topic.
    position = get_next_unit(session, student, book.id, context.id)
    assert position.unit.unit_id == ids["topic"]


def test_a_required_checkpoint_blocks_until_passed(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_checkpoint_tree()
    )
    question, correct = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))

    complete_unit(session, student, book.id, ids["chapter"], context.id)
    complete_unit(session, student, book.id, ids["topic"], context.id)

    # The checkpoint is now the current unit...
    position = get_next_unit(session, student, book.id, context.id)
    assert position.unit.unit_id == ids["exercise"]
    assert position.unit.kind == BookUnitKind.EXERCISE_SET
    assert position.unit.assessment_requirement == AssessmentRequirement.REQUIRED
    # ...and the completion endpoint refuses assessment units.
    with pytest.raises(LearningConflictError) as refused:
        complete_unit(session, student, book.id, ids["exercise"], context.id)
    assert "passing attempts" in str(refused.value)

    # A failing attempt records history but writes NO progress.
    failed = submit_attempt(
        session,
        student,
        book.id,
        ids["exercise"],
        _wrong_submission(session, context, question),
    )
    assert failed.passed is False
    assert failed.feedback and failed.feedback[0].explanation == Q1_EXPLANATION
    rows = session.scalars(select(StudentBookProgress)).all()
    assert {row.book_unit_id for row in rows} == {ids["chapter"], ids["topic"]}

    # The walk still sits on the checkpoint.
    position = get_next_unit(session, student, book.id, context.id)
    assert position.unit.unit_id == ids["exercise"]

    # A passing attempt completes the checkpoint...
    passed = submit_attempt(
        session,
        student,
        book.id,
        ids["exercise"],
        _correct_submission(context, question, correct),
    )
    assert passed.passed is True
    assert passed.feedback == []
    rows = session.scalars(select(StudentBookProgress)).all()
    assert {row.book_unit_id for row in rows} == {
        ids["chapter"],
        ids["topic"],
        ids["exercise"],
    }
    checkpoint_events = _events(session, EVENT_BOOK_CHECKPOINT_PASSED)
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0].user_id == student.id
    payload = json.loads(checkpoint_events[0].metadata_json)
    assert set(payload) == {
        "book_id",
        "book_revision_id",
        "book_unit_id",
        "book_attempt_id",
    }
    assert str(passed.attempt_id) == payload["book_attempt_id"]

    # ...and the walk moves on, then finishes.
    position = get_next_unit(session, student, book.id, context.id)
    assert position.unit.unit_id == ids["closing"]
    complete_unit(session, student, book.id, ids["closing"], context.id)
    assert get_next_unit(session, student, book.id, context.id).finished is True


def test_the_completion_endpoint_never_accepts_exercise_sets(
    session: Session, admin: User, student: User
) -> None:
    tree = (
        _unit(
            "exercise",
            BookUnitKind.EXERCISE_SET,
            assessment=AssessmentRequirement.NOT_REQUIRED.value,
        ),
    )
    book, revision, ids, context = _seed_published(session, admin, tree=tree)
    _question, _option = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))
    with pytest.raises(LearningConflictError) as refused:
        complete_unit(session, student, book.id, ids["exercise"], context.id)
    assert "exercise sets are completed through passing attempts" in str(refused.value)


# --- optional practice ------------------------------------------------------------------


def _optional_tree() -> tuple[dict, ...]:
    """chapter(required) with an optional exercise set + a trailing leaf."""
    return (
        _unit(
            "chapter",
            BookUnitKind.CHAPTER,
            assessment=AssessmentRequirement.REQUIRED.value,
        ),
        _unit(
            "exercise",
            BookUnitKind.EXERCISE_SET,
            parent="chapter",
            order=1,
            assessment=AssessmentRequirement.NOT_REQUIRED.value,
        ),
        _unit(
            "closing",
            BookUnitKind.EXPLANATION,
            parent=None,
            order=2,
        ),
    )


def test_an_optional_exercise_set_never_blocks_progression(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_optional_tree()
    )
    question, correct = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))

    complete_unit(session, student, book.id, ids["chapter"], context.id)
    # The optional set is skipped, not delivered as a gate.
    position = get_next_unit(session, student, book.id, context.id)
    assert position.unit.unit_id == ids["closing"]
    rows = session.scalars(select(StudentBookProgress)).all()
    assert {row.book_unit_id for row in rows} == {ids["chapter"]}

    # It stays readable and attemptable as practice.
    exercise = read_unit(session, student, book.id, ids["exercise"], context.id)
    assert exercise.assessment_requirement == AssessmentRequirement.NOT_REQUIRED
    assert [q.prompt for q in exercise.questions] == [Q1_PROMPT]

    failed = submit_attempt(
        session, student, book.id, ids["exercise"], _wrong_submission(session, context, question)
    )
    assert failed.passed is False
    rows = session.scalars(select(StudentBookProgress)).all()
    assert {row.book_unit_id for row in rows} == {ids["chapter"]}

    passed = submit_attempt(
        session,
        student,
        book.id,
        ids["exercise"],
        _correct_submission(context, question, correct),
    )
    assert passed.passed is True
    rows = session.scalars(select(StudentBookProgress)).all()
    assert {row.book_unit_id for row in rows} == {ids["chapter"], ids["exercise"]}
    # ...and the walk never depended on it.
    assert get_next_unit(session, student, book.id, context.id).finished is False


def test_optional_assessment_access_cannot_bypass_a_required_checkpoint(
    session: Session, admin: User, student: User
) -> None:
    tree = (
        _unit(
            "chapter",
            BookUnitKind.CHAPTER,
            assessment=AssessmentRequirement.REQUIRED.value,
        ),
        _unit(
            "gate",
            BookUnitKind.EXERCISE_SET,
            parent="chapter",
            order=1,
            assessment=AssessmentRequirement.REQUIRED.value,
        ),
        _unit(
            "practice",
            BookUnitKind.EXERCISE_SET,
            parent="chapter",
            order=2,
            assessment=AssessmentRequirement.NOT_REQUIRED.value,
        ),
    )
    book, revision, ids, context = _seed_published(session, admin, tree=tree)
    question, correct = _add_question(session, admin, revision, _unit_row(session, ids["gate"]))
    _practice_question, practice_option = _add_question(
        session, admin, revision, _unit_row(session, ids["practice"])
    )

    complete_unit(session, student, book.id, ids["chapter"], context.id)
    with pytest.raises(LearningConflictError) as blocked:
        submit_attempt(
            session,
            student,
            book.id,
            ids["practice"],
            _correct_submission(context, _practice_question, practice_option),
        )
    assert "not available yet" in str(blocked.value)

    # Passing the required gate makes the practice set reachable...
    submit_attempt(
        session,
        student,
        book.id,
        ids["gate"],
        _correct_submission(context, question, correct),
    )
    # ...and the practice set is now attemptable and viewable.
    practice = read_unit(session, student, book.id, ids["practice"], context.id)
    assert practice.assessment_requirement == AssessmentRequirement.NOT_REQUIRED
    assert practice.questions != []


def test_an_unresolved_exercise_set_carries_no_assessment_obligation(
    session: Session, admin: User, student: User
) -> None:
    tree = (
        _unit(
            "chapter",
            BookUnitKind.CHAPTER,
            assessment=AssessmentRequirement.REQUIRED.value,
        ),
        _unit(
            "exercise",
            BookUnitKind.EXERCISE_SET,
            parent="chapter",
            assessment=AssessmentRequirement.UNRESOLVED.value,
        ),
        _unit(
            "closing",
            BookUnitKind.EXPLANATION,
            parent=None,
            order=2,
        ),
    )
    book, revision, ids, context = _seed_published(session, admin, tree=tree)
    question, correct = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))

    complete_unit(session, student, book.id, ids["chapter"], context.id)
    # Not a checkpoint: the walk skips straight past it.
    position = get_next_unit(session, student, book.id, context.id)
    assert position.unit.unit_id == ids["closing"]

    passed = submit_attempt(
        session,
        student,
        book.id,
        ids["exercise"],
        _correct_submission(context, question, correct),
    )
    assert passed.passed is True
    rows = session.scalars(select(StudentBookProgress)).all()
    assert {row.book_unit_id for row in rows} == {ids["chapter"], ids["exercise"]}


# --- fail closed + fresh editions ----------------------------------------------------------


def test_an_unresolved_chapter_designation_fails_closed(
    session: Session, admin: User, student: User
) -> None:
    tree = (
        _unit("chapter", BookUnitKind.CHAPTER),  # extraction default: unresolved
    )
    book, _revision, ids, context = _seed_published(session, admin, tree=tree)
    with pytest.raises(LearningConflictError) as walk:
        get_next_unit(session, student, book.id, context.id)
    assert "unresolved assessment designation" in str(walk.value)
    with pytest.raises(LearningConflictError):
        read_unit(session, student, book.id, ids["chapter"], context.id)
    with pytest.raises(LearningConflictError):
        complete_unit(session, student, book.id, ids["chapter"], context.id)
    assert session.scalars(select(StudentBookProgress)).all() == []


def test_a_new_publication_starts_every_walk_fresh(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_checkpoint_tree()
    )
    _question, _option = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))
    complete_unit(session, student, book.id, ids["chapter"], context.id)
    complete_unit(session, student, book.id, ids["topic"], context.id)

    # Publish edition 2: same book, new revision, new unit rows.
    asset = FileAsset(
        uploaded_by_user_id=admin.id,
        original_filename="textbook-v2.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        checksum_sha256="feedface",
        storage_key="pending",
        validation_status=FileAssetStatus.STORED.value,
    )
    session.add(asset)
    session.flush()
    asset.storage_key = f"file_assets/{asset.id}"
    # Mirror the real publication flow: the old pointer is demoted
    # BEFORE a new one claims the partial-unique current slot.
    revision.is_current_publication = False
    new_revision = BookRevision(
        book_id=book.id,
        revision_number=2,
        file_asset_id=asset.id,
        uploaded_by_user_id=admin.id,
        status=BookRevisionStatus.PUBLISHED.value,
        is_current_publication=True,
    )
    session.add(new_revision)
    session.flush()
    session.add(
        BookRevisionContext(
            book_revision_id=new_revision.id, learning_context_id=context.id
        )
    )
    new_unit = BookUnit(
        book_revision_id=new_revision.id,
        kind=BookUnitKind.CHAPTER.value,
        title="Chapter 1 Introduction",
        content="Science studies the world.",
        display_order=1,
        source_page_start=1,
        source_page_end=1,
        assessment_requirement=AssessmentRequirement.REQUIRED.value,
        review_status=BookUnitReviewStatus.APPROVED.value,
        reviewed_by_user_id=admin.id,
        reviewed_at=datetime.now(timezone.utc),
    )
    session.add(new_unit)
    session.flush()
    session.commit()

    detail = get_book(session, student, book.id, context.id)
    assert detail.published_revision_id == new_revision.id
    assert detail.total_units == 1
    assert detail.completed_units == 0

    position = get_next_unit(session, student, book.id, context.id)
    assert position.unit.unit_id == new_unit.id

    # The historical progress stays exactly where it was earned.
    rows = session.scalars(select(StudentBookProgress)).all()
    assert {row.book_revision_id for row in rows} == {revision.id}


def test_another_students_progress_is_never_visible(
    session: Session, admin: User, student: User, other_student: User
) -> None:
    book, revision, ids, context = _seed_published(
        session, admin, tree=_checkpoint_tree()
    )
    _question, _option = _add_question(session, admin, revision, _unit_row(session, ids["exercise"]))
    complete_unit(session, student, book.id, ids["chapter"], context.id)

    mine = get_book(session, student, book.id, context.id)
    assert mine.completed_units == 1
    theirs = get_book(session, other_student, book.id, context.id)
    assert theirs.completed_units == 0
    their_position = get_next_unit(session, other_student, book.id, context.id)
    assert their_position.unit.unit_id == ids["chapter"]


def _unit_row(session: Session, unit_id: uuid.UUID) -> BookUnit:
    session.expire_all()
    return session.get(BookUnit, unit_id)
