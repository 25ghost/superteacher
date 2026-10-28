"""Unit tests: objective grading, attempts and feedback (book P4).

In-memory SQLite (model CHECKs and the composite FKs are real here),
no HTTP — the durable half of the assessment surface:

- **grading** is equal-weighted; the pass decision compares the EXACT
  correct-count/total-count fraction against the set's threshold
  (default 70) with INTEGER arithmetic, so 2/3 fails 70 but passes 66;
- **persistence** writes the attempt, one answer row per question and
  one audit event in the same flush — attributed to the STUDENT with
  no invented actor — and an accepted attempt is immutable; retries
  always produce a NEW attempt;
- **feedback** carries the authored explanation for each miss and
  never the correct option id, an ``is_correct`` flag or any other
  answer-key field;
- **validity is fail-closed**: an unverified question, a set with no
  questions, a tampered key, a non-published / non-current revision, a
  foreign learning context or unit, and any incomplete or inconsistent
  submission are refused (409 / 404 / 422) BEFORE anything is written;
- **access** is active-student-only with a real student profile
  (403 / 404), administrator and teacher roles never pass.

Audit payloads carry identifiers, counts and the frozen pass flag —
never question, explanation or option text — and the event name fits
the 32-char ``auth_events.event_type`` column.
"""
from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.auth_event import AuthEvent
from app.models.book_attempt import BookAttempt
from app.models.book_attempt_answer import BookAttemptAnswer
from app.models.book_question_option import BookQuestionOption
from app.models.enums import (
    BookQuestionType,
    BookRevisionStatus,
    UserRole,
    UserStatus,
)
from app.models.student import Student
from app.models.user import User
from app.schemas.book_assessment import (
    AssessmentAnswerSubmit,
    AssessmentAttemptSubmit,
    BookQuestionCreate,
    BookQuestionVerifyRequest,
    BookUnitThresholdRequest,
)
from app.services.book_assessment_service import (
    EVENT_ATTEMPT_RECORDED,
    record_attempt,
)
from app.services.book_question_service import (
    create_question,
    set_unit_threshold,
    verify_question,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)
from tests.unit.test_book_question_service import (
    _catalog_context,
    _events,
    _make_user,
    _options,
    _row,
    _seed,
)


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
    session.flush()
    session.add(Student(user_id=user.id, full_name="Test Student"))
    session.commit()
    return user


# --- scene builders --------------------------------------------------------------------


def _exercise_payload(order: int) -> BookQuestionCreate:
    return BookQuestionCreate(
        question_type=BookQuestionType.MULTIPLE_CHOICE,
        prompt=f"Question {order}: what is two plus two?",
        explanation=f"Explanation {order}: two plus two equals four.",
        display_order=order,
        options=_options(f"right-{order}", f"wrong-{order}"),
    )


def _author(
    session: Session,
    admin: User,
    book,
    revision,
    unit_id,
    total: int,
    *,
    verified_upto: int | None = None,
) -> list:
    """``total`` multiple-choice questions; option 0 is the keyed one.

    ``verified_upto=None`` (the default) verifies every question;
    ``verified_upto=n`` leaves everything after order ``n`` unverified.
    """
    reads = []
    for order in range(1, total + 1):
        read = create_question(
            session,
            admin,
            book_id=book.id,
            revision_id=revision.id,
            unit_id=unit_id,
            payload=_exercise_payload(order),
        )
        if verified_upto is None or order <= verified_upto:
            verify_question(
                session,
                admin,
                book_id=book.id,
                revision_id=revision.id,
                question_id=read.question_id,
                payload=BookQuestionVerifyRequest(
                    correct_option_id=read.options[0].option_id
                ),
            )
        reads.append(read)
    return reads


def _publish(session: Session, revision, *, current: bool = True) -> None:
    """Flip a revision to published/current, as publication would leave it."""
    row = _row(session, revision.id)
    row.status = BookRevisionStatus.PUBLISHED.value
    row.is_current_publication = current
    session.commit()


def _context_id(session: Session) -> uuid.UUID:
    return _catalog_context(session).id


def _submission(context_id, reads, *, correct_count: int, answers=None):
    if answers is None:
        answers = []
        for index, read in enumerate(reads):
            option = read.options[0] if index < correct_count else read.options[1]
            answers.append(
                AssessmentAnswerSubmit(
                    question_id=read.question_id, option_id=option.option_id
                )
            )
    return AssessmentAttemptSubmit(
        learning_context_id=context_id, answers=answers
    )


def _submit(session, user, book, revision, unit_id, payload):
    return record_attempt(
        session,
        user,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=unit_id,
        payload=payload,
    )


def _attempt_rows(session: Session) -> list[BookAttempt]:
    session.expire_all()
    return list(session.scalars(select(BookAttempt)))


def _answer_rows(session: Session) -> list[BookAttemptAnswer]:
    session.expire_all()
    return list(session.scalars(select(BookAttemptAnswer)))


def _assert_nothing_written(session: Session) -> None:
    assert _attempt_rows(session) == []
    assert _answer_rows(session) == []
    assert _events(session, EVENT_ATTEMPT_RECORDED) == []


def _snapshot(session: Session, attempt_id) -> dict:
    session.expire_all()
    row = session.get(BookAttempt, attempt_id)
    return {
        name: getattr(row, name)
        for name in (
            "student_id",
            "book_revision_id",
            "book_unit_id",
            "total_questions",
            "correct_answers",
            "score_percent",
            "passed",
            "created_at",
        )
    }


# --- grading & persistence ---------------------------------------------------------------


def test_a_complete_attempt_is_graded_persisted_and_audited(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=4)
    context_id = _context_id(session)
    _publish(session, revision)

    read = _submit(
        session,
        student,
        book,
        revision,
        unit_id,
        _submission(context_id, reads, correct_count=3),
    )

    assert read.attempt_id is not None
    assert read.book_revision_id == revision.id
    assert read.book_unit_id == unit_id
    assert read.total_questions == 4
    assert read.correct_answers == 3
    assert read.score_percent == 75.0
    assert read.passed is True
    assert read.submitted_at is not None
    # Exactly the missed question carries corrective feedback.
    assert [entry.question_id for entry in read.feedback] == [reads[3].question_id]
    assert read.feedback[0].explanation == "Explanation 4: two plus two equals four."

    attempts = _attempt_rows(session)
    assert len(attempts) == 1
    attempt = attempts[0]
    profile = session.scalar(select(Student).where(Student.user_id == student.id))
    assert attempt.id == read.attempt_id
    assert attempt.student_id == profile.id
    assert attempt.book_revision_id == revision.id
    assert attempt.book_unit_id == unit_id
    assert attempt.total_questions == 4
    assert attempt.correct_answers == 3
    assert attempt.passed is True

    answers = _answer_rows(session)
    assert len(answers) == 4
    assert {answer.book_attempt_id for answer in answers} == {attempt.id}
    assert {answer.book_question_id for answer in answers} == {
        read_question.question_id for read_question in reads
    }
    expected_keys = {
        answer.book_question_id: answer.is_correct
        for answer in answers
    }
    for index, read_question in enumerate(reads):
        assert expected_keys[read_question.question_id] is (index < 3)

    events = _events(session, EVENT_ATTEMPT_RECORDED)
    assert len(events) == 1
    payload = json.loads(events[0].metadata_json)
    assert payload == {
        "book_id": str(book.id),
        "book_revision_id": str(revision.id),
        "book_unit_id": str(unit_id),
        "book_attempt_id": str(attempt.id),
        "total_questions": 4,
        "correct_answers": 3,
        "passed": True,
    }
    # The attempt is about the STUDENT; no invented administrator actor.
    assert events[0].user_id == student.id
    assert events[0].actor_user_id is None
    # Content never reaches the audit trail.
    blob = "\n".join(
        event.metadata_json or ""
        for event in session.scalars(select(AuthEvent))
    )
    for read_question in reads:
        assert read_question.prompt not in blob
        assert read_question.explanation not in blob


@pytest.mark.parametrize(
    "total,threshold,correct,expected",
    [
        (10, 70, 7, True),
        (10, 70, 6, False),
        (3, 70, 2, False),
        (3, 66, 2, True),
        (3, 67, 2, False),
        (10, 90, 8, False),
    ],
)
def test_the_pass_decision_compares_the_exact_fraction_against_the_threshold(
    session: Session,
    admin: User,
    student: User,
    total: int,
    threshold: int,
    correct: int,
    expected: bool,
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=total)
    set_unit_threshold(
        session,
        admin,
        book_id=book.id,
        revision_id=revision.id,
        unit_id=unit_id,
        payload=BookUnitThresholdRequest(passing_threshold_percent=threshold),
    )
    context_id = _context_id(session)
    _publish(session, revision)

    read = _submit(
        session,
        student,
        book,
        revision,
        unit_id,
        _submission(context_id, reads, correct_count=correct),
    )

    # Exact stored fraction — no rounding is involved anywhere.
    assert read.score_percent == correct / total * 100
    assert read.passed is expected
    assert (correct * 100 >= total * threshold) is expected
    attempts = _attempt_rows(session)
    assert len(attempts) == 1
    assert attempts[0].passed is expected
    assert attempts[0].correct_answers == correct
    assert len(_answer_rows(session)) == total


def test_every_retry_is_a_new_immutable_attempt(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=3)
    context_id = _context_id(session)
    _publish(session, revision)

    first = _submit(
        session,
        student,
        book,
        revision,
        unit_id,
        _submission(context_id, reads, correct_count=1),
    )
    assert first.passed is False
    before = _snapshot(session, first.attempt_id)

    second = _submit(
        session,
        student,
        book,
        revision,
        unit_id,
        _submission(context_id, reads, correct_count=3),
    )

    assert second.attempt_id != first.attempt_id
    assert second.passed is True
    attempts = _attempt_rows(session)
    assert len(attempts) == 2
    assert {attempt.id for attempt in attempts} == {
        first.attempt_id,
        second.attempt_id,
    }
    # The first attempt is frozen: same verdict, scores and timestamp.
    assert _snapshot(session, first.attempt_id) == before
    assert len(_answer_rows(session)) == 6
    answers = _answer_rows(session)
    assert len(
        [a for a in answers if a.book_attempt_id == first.attempt_id]
    ) == 3
    assert len(
        [a for a in answers if a.book_attempt_id == second.attempt_id]
    ) == 3
    assert len(_events(session, EVENT_ATTEMPT_RECORDED)) == 2


def test_feedback_explains_every_miss_without_exposing_the_key(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=4)
    context_id = _context_id(session)
    _publish(session, revision)

    read = _submit(
        session,
        student,
        book,
        revision,
        unit_id,
        _submission(context_id, reads, correct_count=2),
    )

    assert [entry.question_id for entry in read.feedback] == [
        reads[2].question_id,
        reads[3].question_id,
    ]
    assert [entry.explanation for entry in read.feedback] == [
        "Explanation 3: two plus two equals four.",
        "Explanation 4: two plus two equals four.",
    ]
    for entry in read.feedback:
        assert set(entry.model_dump()) == {"question_id", "explanation"}

    dump = json.dumps(read.model_dump(mode="json"))
    assert "is_correct" not in dump
    assert "correct_option" not in dump
    assert "option_text" not in dump
    # No keyed option id leaks — including for questions answered right.
    for read_question in reads:
        assert str(read_question.options[0].option_id) not in dump
        assert read_question.prompt not in dump

    # A flawless attempt needs no corrective feedback at all.
    flawless = _submit(
        session,
        student,
        book,
        revision,
        unit_id,
        _submission(context_id, reads, correct_count=4),
    )
    assert flawless.passed is True
    assert flawless.feedback == []
    assert len(_attempt_rows(session)) == 2


# --- fail-closed validity -------------------------------------------------------------


def test_an_unverified_question_refuses_the_whole_attempt(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(
        session, admin, book, revision, unit_id, total=4, verified_upto=3
    )
    context_id = _context_id(session)
    _publish(session, revision)

    with pytest.raises(LearningValidationError, match="are unverified"):
        _submit(
            session,
            student,
            book,
            revision,
            unit_id,
            _submission(context_id, reads, correct_count=4),
        )
    _assert_nothing_written(session)


def test_an_exercise_set_without_questions_cannot_be_graded(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    context_id = _context_id(session)
    _publish(session, revision)

    # The payload is well-formed; the SET is what is ungradeable.
    payload = AssessmentAttemptSubmit(
        learning_context_id=context_id,
        answers=[
            AssessmentAnswerSubmit(
                question_id=uuid.uuid4(), option_id=uuid.uuid4()
            )
        ],
    )
    with pytest.raises(
        LearningValidationError, match="no questions to grade"
    ):
        _submit(session, student, book, revision, ids["exercise"], payload)
    _assert_nothing_written(session)


def test_a_verified_question_must_hold_exactly_one_key(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=2)
    context_id = _context_id(session)
    _publish(session, revision)

    # Tamper: keep the verified timestamp, withdraw every key flag.
    session.expire_all()
    for question_id in (reads[0].question_id, reads[1].question_id):
        for option in session.scalars(
            select(BookQuestionOption).where(
                BookQuestionOption.book_question_id == question_id
            )
        ):
            option.is_correct = False
    session.commit()

    with pytest.raises(
        LearningValidationError, match="exactly one correct option"
    ):
        _submit(
            session,
            student,
            book,
            revision,
            unit_id,
            _submission(context_id, reads, correct_count=2),
        )
    _assert_nothing_written(session)


def test_attempts_are_confined_to_the_current_published_revision(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=2)
    context_id = _context_id(session)

    with pytest.raises(LearningConflictError, match="not published"):
        _submit(
            session,
            student,
            book,
            revision,
            unit_id,
            _submission(context_id, reads, correct_count=2),
        )
    _assert_nothing_written(session)

    _publish(session, revision, current=False)
    with pytest.raises(
        LearningConflictError, match="no longer the current publication"
    ):
        _submit(
            session,
            student,
            book,
            revision,
            unit_id,
            _submission(context_id, reads, correct_count=2),
        )
    _assert_nothing_written(session)

    _publish(session, revision, current=True)
    unknown_book_id = uuid.uuid4()
    with pytest.raises(LearningNotFoundError) as unknown:
        record_attempt(
            session,
            student,
            book_id=unknown_book_id,
            revision_id=revision.id,
            unit_id=unit_id,
            payload=_submission(context_id, reads, correct_count=2),
        )
    other_book, other_revision, _ = _seed(session, admin)
    with pytest.raises(LearningNotFoundError) as foreign:
        record_attempt(
            session,
            student,
            book_id=other_book.id,
            revision_id=revision.id,
            unit_id=unit_id,
            payload=_submission(context_id, reads, correct_count=2),
        )
    for exc in (unknown.value, foreign.value):
        assert exc.status_code == 404
        assert str(exc).startswith("no book revision with id ")
    _assert_nothing_written(session)

    # The revision now accepts attempts again.
    accepted = _submit(
        session,
        student,
        book,
        revision,
        unit_id,
        _submission(context_id, reads, correct_count=2),
    )
    assert accepted.passed is True
    assert len(_attempt_rows(session)) == 1


def test_the_learning_context_must_match_exactly(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=2)
    _publish(session, revision)

    with pytest.raises(
        LearningNotFoundError,
        match="not valid for the supplied learning context",
    ):
        _submit(
            session,
            student,
            book,
            revision,
            unit_id,
            _submission(uuid.uuid4(), reads, correct_count=2),
        )
    _assert_nothing_written(session)


def test_only_the_revision_s_own_exercise_set_accepts_attempts(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    reads = _author(session, admin, book, revision, ids["exercise"], total=2)
    context_id = _context_id(session)
    _publish(session, revision)

    with pytest.raises(
        LearningValidationError, match="only an exercise_set unit"
    ):
        _submit(
            session,
            student,
            book,
            revision,
            ids["chapter"],
            _submission(context_id, reads, correct_count=2),
        )
    _assert_nothing_written(session)

    with pytest.raises(LearningNotFoundError, match="no book unit with id"):
        _submit(
            session,
            student,
            book,
            revision,
            uuid.uuid4(),
            _submission(context_id, reads, correct_count=2),
        )
    _assert_nothing_written(session)

    # A foreign revision's unit answers the SAME 404 as an unknown one.
    _, other_revision, other_ids = _seed(session, admin)
    _publish(session, other_revision, current=False)
    with pytest.raises(LearningNotFoundError, match="no book unit with id"):
        _submit(
            session,
            student,
            book,
            revision,
            other_ids["exercise"],
            _submission(context_id, reads, correct_count=2),
        )
    _assert_nothing_written(session)


def test_a_submission_must_answer_every_question_exactly_once(
    session: Session, admin: User, student: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=3)
    context_id = _context_id(session)
    _publish(session, revision)
    full = _submission(context_id, reads, correct_count=3)

    unknown_question = AssessmentAnswerSubmit(
        question_id=uuid.uuid4(), option_id=reads[0].options[0].option_id
    )
    duplicate = AssessmentAnswerSubmit(
        question_id=reads[0].question_id,
        option_id=reads[0].options[1].option_id,
    )
    omitted = [
        AssessmentAnswerSubmit(
            question_id=read_question.question_id,
            option_id=read_question.options[0].option_id,
        )
        for read_question in reads[:2]
    ]
    foreign_option = AssessmentAnswerSubmit(
        question_id=reads[0].question_id,
        option_id=reads[1].options[0].option_id,
    )
    other_book, other_revision, other_ids = _seed(session, admin)
    other_reads = _author(
        session, admin, other_book, other_revision, other_ids["exercise"], total=2
    )
    cross_revision = AssessmentAnswerSubmit(
        question_id=reads[0].question_id,
        option_id=other_reads[0].options[0].option_id,
    )

    cases = [
        (list(full.answers) + [unknown_question], "is not part of this exercise set"),
        (
            [duplicate] + list(full.answers),
            "was answered more than once",
        ),
        (omitted, "were not answered"),
        (
            [foreign_option] + list(full.answers[1:]),
            "is not an option of question",
        ),
        (
            [cross_revision] + list(full.answers[1:]),
            "is not an option of question",
        ),
    ]
    for answers, message in cases:
        payload = AssessmentAttemptSubmit(
            learning_context_id=context_id, answers=answers
        )
        with pytest.raises(LearningValidationError, match=message):
            _submit(session, student, book, revision, unit_id, payload)
        _assert_nothing_written(session)

    with pytest.raises(ValidationError):
        AssessmentAttemptSubmit(learning_context_id=context_id, answers=[])


def test_attempt_recording_is_for_active_students_with_a_profile(
    session: Session, admin: User, teacher: User
) -> None:
    book, revision, ids = _seed(session, admin)
    unit_id = ids["exercise"]
    reads = _author(session, admin, book, revision, unit_id, total=2)
    context_id = _context_id(session)
    _publish(session, revision)
    payload = _submission(context_id, reads, correct_count=2)

    for actor in (admin, teacher):
        with pytest.raises(
            LearningForbiddenError, match="student role required"
        ):
            _submit(session, actor, book, revision, unit_id, payload)
    _assert_nothing_written(session)

    suspended = _make_user(UserRole.STUDENT.value)
    suspended.status = UserStatus.SUSPENDED.value
    session.add(suspended)
    session.flush()
    session.add(Student(user_id=suspended.id, full_name="Suspended Student"))
    session.commit()
    with pytest.raises(LearningForbiddenError, match="not active"):
        _submit(session, suspended, book, revision, unit_id, payload)
    _assert_nothing_written(session)

    profileless = _make_user(UserRole.STUDENT.value)
    session.add(profileless)
    session.commit()
    with pytest.raises(
        LearningNotFoundError, match="no student profile"
    ):
        _submit(session, profileless, book, revision, unit_id, payload)
    _assert_nothing_written(session)

    # A real active student with a profile sails through.
    session.add(Student(user_id=profileless.id, full_name="Late Student"))
    session.commit()
    accepted = _submit(session, profileless, book, revision, unit_id, payload)
    assert accepted.total_questions == 2
    assert len(_attempt_rows(session)) == 1
