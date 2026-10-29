"""Student guided learning over administrator-published books (book P5).

The student's durable, sequential walk through ONE published edition:

- **Access** is exact-match and fail-closed (ADR-002): the caller must
  be an ACTIVE student with a student profile, and every operation names
  the canonical ``learning_context`` the book is valid for — the
  revision must be the book's CURRENT publication AND linked to exactly
  that context. An unknown book, a book with no current publication and
  a context the revision is not valid for answer the SAME 404 (L6
  existence leak); a student never learns a publication exists outside
  their selected context.
- **The walk** is the publication's document order — depth-first,
  parents before children, sibling ``display_order`` — the same order
  the review surface approved. Position = the FIRST not-yet-completed
  unit in that order that gates progression:

    * a content unit (chapter/topic/explanation/example) is completed
      by the student explicitly (``complete_unit``);
    * an ``exercise_set`` whose reviewed designation is ``required`` is
      a checkpoint: only a PASSING attempt (``record_attempt``) writes
      its completion, in the same transaction as the attempt;
    * an ``exercise_set`` that is ``not_required`` (or carries the
      extraction default ``unresolved`` — the approved P4 model gives
      such sets no assessment obligation) never blocks: it is practice
      the student may attempt once reached, and its designation is
      surfaced verbatim so the optional status stays clear.

  ``unresolved`` designations on chapter/topic units can never reach a
  publication through the approved flow (publication refuses them); if
  inconsistent legacy data is encountered anyway the walk FAILS CLOSED
  (409) rather than interpreting the designation as optional or as an
  automatic pass.
- **Visibility**: only the current available unit, already-completed
  units and REACHED optional exercise sets (every gating unit before
  them is completed) are readable; arbitrary later units answer the
  same 404 as unknown ids. Attempts only reach exercise sets that are
  at or before the walk frontier — optional assessment access can never
  bypass a required checkpoint.
- **Persistence**: progress rows are keyed
  ``(student_id, published_revision_id, book_unit_id)``; completions
  are stamped once and never rewritten. A new publication starts every
  walk fresh — nothing is copied from older editions. Progress writes
  lock the student's own rows and rely on the unique key (insert
  ignoring conflicts, then re-read) so concurrent requests can never
  double-write.

House rules, everywhere: unknown and foreign ids answer the SAME 404,
invalid state is 409, invalid content 422, and audit payloads carry
identifiers only. Nothing commits here — the API layer owns the
transaction.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql as sa_postgresql
from sqlalchemy.dialects import sqlite as sa_sqlite
from sqlalchemy.orm import Session, selectinload

from app.models.book import Book
from app.models.book_question import BookQuestion
from app.models.book_revision import BookRevision
from app.models.book_revision_context import BookRevisionContext
from app.models.book_unit import BookUnit
from app.models.enums import (
    AssessmentRequirement,
    BookRevisionStatus,
    BookUnitKind,
    UserRole,
    UserStatus,
)
from app.models.student import Student
from app.models.student_book_progress import StudentBookProgress
from app.repositories import auth_event_repository as auth_event_repo
from app.schemas.book_assessment import AssessmentAttemptRead, AssessmentAttemptSubmit
from app.schemas.student_book import (
    StudentBookDetailRead,
    StudentBookListItemRead,
    StudentBookNextRead,
    StudentBookUnitProgressRead,
    StudentBookUnitRead,
    StudentQuestionOptionRead,
    StudentQuestionRead,
)
from app.services.book_assessment_service import record_attempt
from app.services.book_review_service import _ordered_tree
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)

logger = logging.getLogger(__name__)

#: Audit ``event_type`` values (fixed, 32-char ``auth_events.event_type``).
EVENT_BOOK_UNIT_COMPLETED = "book_unit_completed"
EVENT_BOOK_CHECKPOINT_PASSED = "book_checkpoint_passed"

#: The kinds whose assessment designation the review flow resolves
#: (mirrors the publication gate's ``_DESIGNATION_KINDS`` vocabulary).
_DESIGNATED_KINDS = (
    BookUnitKind.CHAPTER.value,
    BookUnitKind.TOPIC.value,
)

_NOT_PUBLISHED = (
    "no book with id {book_id} for the supplied learning context"
)


# --- access ---------------------------------------------------------------------------


def _load_student(session: Session, user) -> Student:
    """The caller's student profile (refuses non-students, 404 if missing).

    An inactive account never reaches the guided walk: the shared auth
    layer already refuses its tokens, and the service re-checks so a
    stale session cannot record progress.
    """
    if user.role != UserRole.STUDENT.value:
        raise LearningForbiddenError("student role required for this operation")
    if user.status != UserStatus.ACTIVE.value:
        raise LearningForbiddenError("this account is not active")
    profile = session.scalar(select(Student).where(Student.user_id == user.id))
    if profile is None:
        raise LearningNotFoundError("no student profile for this account")
    return profile


def _load_access(
    session: Session, user, book_id: uuid.UUID, learning_context_id: uuid.UUID
) -> tuple[Student, BookRevision]:
    """The caller's profile and the book's CURRENT publication, context-checked.

    The revision must be the book's current publication AND valid for the
    explicitly supplied canonical context. Unknown book, missing
    publication and context mismatch answer the same 404 (L6).
    """
    student = _load_student(session, user)
    revision = session.scalar(
        select(BookRevision).where(
            BookRevision.book_id == book_id,
            BookRevision.is_current_publication.is_(True),
        )
    )
    if (
        revision is None
        or revision.status != BookRevisionStatus.PUBLISHED.value
    ):
        raise LearningNotFoundError(
            _NOT_PUBLISHED.format(book_id=book_id)
        )
    linked = session.scalar(
        select(BookRevisionContext.id).where(
            BookRevisionContext.book_revision_id == revision.id,
            BookRevisionContext.learning_context_id == learning_context_id,
        )
    )
    if linked is None:
        raise LearningNotFoundError(
            _NOT_PUBLISHED.format(book_id=book_id)
        )
    return student, revision


# --- the walk --------------------------------------------------------------------------


def _blocks_progression(unit: BookUnit) -> bool:
    """Whether this unit gates the walk until it is completed.

    Content units always do. Among exercise sets ONLY the ``required``
    designation gates — ``not_required`` practice and the extraction
    default ``unresolved`` carry no assessment obligation in the
    approved model (the P4 publication gate exempts exactly those).
    """
    if unit.kind == BookUnitKind.EXERCISE_SET.value:
        return unit.assessment_requirement == AssessmentRequirement.REQUIRED.value
    return True


def _walk(
    session: Session,
    student: Student,
    revision: BookRevision,
    *,
    lock: bool = False,
) -> tuple[list[BookUnit], set[uuid.UUID], BookUnit | None, int]:
    """One pass over the published tree: order, completion, position.

    Returns ``(units in document order, completed unit ids, the current
    gating unit (None when finished), its index (len(units) when
    finished))``. Non-blocking exercise sets are transparent to the
    position search but stay visible on the way. With ``lock=True`` the
    student's own progress rows are read FOR UPDATE so concurrent
    completions of the same student serialize.

    Fail closed: an ``unresolved`` designation on a chapter/topic unit
    of a PUBLISHED revision is inconsistent legacy data — the whole
    walk is refused rather than interpreting it as optional.
    """
    units = _ordered_tree(
        list(
            session.scalars(
                select(BookUnit)
                .where(BookUnit.book_revision_id == revision.id)
                .execution_options(populate_existing=True)
            )
        )
    )
    for unit in units:
        if (
            unit.kind in _DESIGNATED_KINDS
            and unit.assessment_requirement == AssessmentRequirement.UNRESOLVED.value
        ):
            raise LearningConflictError(
                "this publication contains an unresolved assessment "
                "designation and cannot be served until the "
                "administrator republishes a corrected revision"
            )

    stmt = select(StudentBookProgress).where(
        StudentBookProgress.student_id == student.id,
        StudentBookProgress.book_revision_id == revision.id,
    )
    if lock:
        stmt = stmt.with_for_update()
    completed = {
        row.book_unit_id
        for row in session.scalars(stmt)
    }

    frontier = len(units)
    current: BookUnit | None = None
    for index, unit in enumerate(units):
        if unit.id in completed:
            continue
        if _blocks_progression(unit):
            frontier = index
            current = unit
            break
    return units, completed, current, frontier


def _reached_exercise_set_ids(
    units: list[BookUnit], frontier: int
) -> set[uuid.UUID]:
    """Every exercise set at or before the walk frontier.

    Optional assessment access stops exactly at the frontier: sets
    beyond the first incomplete gating unit are not reachable, so an
    optional exercise set can never bypass a required checkpoint.
    """
    return {
        unit.id
        for unit in units[: frontier + 1]
        if unit.kind == BookUnitKind.EXERCISE_SET.value
    }


def _load_questions(session: Session, unit_id: uuid.UUID) -> list[BookQuestion]:
    """The exercise set's questions with options, in sibling order."""
    return list(
        session.scalars(
            select(BookQuestion)
            .where(BookQuestion.book_unit_id == unit_id)
            .options(selectinload(BookQuestion.options))
            .order_by(BookQuestion.display_order.asc(), BookQuestion.id.asc())
            .execution_options(populate_existing=True)
        )
    )


def _read_unit(
    session: Session,
    book_id: uuid.UUID,
    revision: BookRevision,
    unit: BookUnit,
    *,
    completed_at: datetime | None = None,
) -> StudentBookUnitRead:
    """One delivered unit (allowlist shape; no answer key, no paths)."""
    questions: list[StudentQuestionRead] = []
    if unit.kind == BookUnitKind.EXERCISE_SET.value:
        for question in _load_questions(session, unit.id):
            ordered_options = sorted(
                question.options,
                key=lambda option: (option.display_order, option.id),
            )
            questions.append(
                StudentQuestionRead(
                    question_id=question.id,
                    question_type=question.question_type,
                    prompt=question.prompt,
                    display_order=question.display_order,
                    options=[
                        StudentQuestionOptionRead(
                            option_id=option.id,
                            option_text=option.option_text,
                            display_order=option.display_order,
                        )
                        for option in ordered_options
                    ],
                )
            )
    return StudentBookUnitRead(
        book_id=book_id,
        published_revision_id=revision.id,
        unit_id=unit.id,
        parent_unit_id=unit.parent_unit_id,
        kind=BookUnitKind(unit.kind),
        title=unit.title,
        content=unit.content,
        display_order=unit.display_order,
        assessment_requirement=AssessmentRequirement(unit.assessment_requirement),
        passing_threshold_percent=unit.passing_threshold_percent,
        progress=(
            StudentBookUnitProgressRead(completed_at=completed_at)
            if completed_at is not None
            else None
        ),
        questions=questions,
    )


def _progress_rows(
    session: Session, student_id: uuid.UUID, revision_id: uuid.UUID
) -> dict[uuid.UUID, StudentBookProgress]:
    rows = session.scalars(
        select(StudentBookProgress).where(
            StudentBookProgress.student_id == student_id,
            StudentBookProgress.book_revision_id == revision_id,
        )
    )
    return {row.book_unit_id: row for row in rows}


def _unit_counts(
    session: Session, revision_ids: list[uuid.UUID], student_id: uuid.UUID
) -> dict[uuid.UUID, tuple[int, int]]:
    """(total, completed) unit counts per revision, two grouped queries."""
    totals: dict[uuid.UUID, int] = {}
    completions: dict[uuid.UUID, int] = {}
    if revision_ids:
        for revision_id, count in session.execute(
            select(BookUnit.book_revision_id, func.count())
            .where(BookUnit.book_revision_id.in_(revision_ids))
            .group_by(BookUnit.book_revision_id)
        ):
            totals[revision_id] = count
        for revision_id, count in session.execute(
            select(StudentBookProgress.book_revision_id, func.count())
            .where(
                StudentBookProgress.student_id == student_id,
                StudentBookProgress.book_revision_id.in_(revision_ids),
            )
            .group_by(StudentBookProgress.book_revision_id)
        ):
            completions[revision_id] = count
    return {
        revision_id: (totals.get(revision_id, 0), completions.get(revision_id, 0))
        for revision_id in revision_ids
    }


# --- reads -----------------------------------------------------------------------------


def list_books(
    session: Session, user, learning_context_id: uuid.UUID
) -> list[StudentBookListItemRead]:
    """Books whose CURRENT publication is valid for the selected context."""
    student = _load_student(session, user)
    rows = session.execute(
        select(Book, BookRevision)
        .join(
            BookRevision,
            (BookRevision.book_id == Book.id)
            & BookRevision.is_current_publication.is_(True),
        )
        .join(
            BookRevisionContext,
            BookRevisionContext.book_revision_id == BookRevision.id,
        )
        .where(
            BookRevision.status == BookRevisionStatus.PUBLISHED.value,
            BookRevisionContext.learning_context_id == learning_context_id,
        )
        .order_by(Book.title.asc(), Book.id.asc())
    )
    books: list[Book] = []
    revisions: list[BookRevision] = []
    for book, revision in rows:
        books.append(book)
        revisions.append(revision)
    counts = _unit_counts(
        session, [revision.id for revision in revisions], student.id
    )
    return [
        StudentBookListItemRead(
            book_id=book.id,
            title=book.title,
            description=book.description,
            published_revision_id=revision.id,
            revision_number=revision.revision_number,
            total_units=counts[revision.id][0],
            completed_units=counts[revision.id][1],
            created_at=book.created_at,
            updated_at=book.updated_at,
        )
        for book, revision in zip(books, revisions)
    ]


def get_book(
    session: Session, user, book_id: uuid.UUID, learning_context_id: uuid.UUID
) -> StudentBookDetailRead:
    """One accessible book's identity + progress summary (counts only)."""
    student, revision = _load_access(session, user, book_id, learning_context_id)
    total, completed = _unit_counts(session, [revision.id], student.id)[revision.id]
    book = session.get(Book, book_id)
    assert book is not None  # the revision's FK guarantees the book row
    return StudentBookDetailRead(
        book_id=book.id,
        title=book.title,
        description=book.description,
        published_revision_id=revision.id,
        revision_number=revision.revision_number,
        total_units=total,
        completed_units=completed,
        created_at=book.created_at,
        updated_at=book.updated_at,
    )


def get_next_unit(
    session: Session, user, book_id: uuid.UUID, learning_context_id: uuid.UUID
) -> StudentBookNextRead:
    """The single next available unit (or ``finished=True``)."""
    student, revision = _load_access(session, user, book_id, learning_context_id)
    _units, _completed, current, _frontier = _walk(session, student, revision)
    if current is None:
        return StudentBookNextRead(
            book_id=book_id,
            published_revision_id=revision.id,
            finished=True,
            unit=None,
        )
    return StudentBookNextRead(
        book_id=book_id,
        published_revision_id=revision.id,
        finished=False,
        unit=_read_unit(session, book_id, revision, current),
    )


def read_unit(
    session: Session,
    user,
    book_id: uuid.UUID,
    unit_id: uuid.UUID,
    learning_context_id: uuid.UUID,
) -> StudentBookUnitRead:
    """One unit the student has legitimately reached — nothing else.

    Completed units are revisitable; the current available unit is
    delivered; optional exercise sets at or before the frontier stay
    readable. Unknown, foreign and beyond-the-frontier ids answer the
    same 404 (L6).
    """
    student, revision = _load_access(session, user, book_id, learning_context_id)
    units, completed, current, frontier = _walk(session, student, revision)
    unit = next((unit for unit in units if unit.id == unit_id), None)
    if unit is None:
        raise LearningNotFoundError(f"no book unit with id {unit_id}")
    completed_at = None
    if unit.id in completed:
        completed_at = _progress_rows(session, student.id, revision.id)[
            unit.id
        ].completed_at
    elif current is not None and unit.id == current.id:
        pass
    elif (
        unit.kind == BookUnitKind.EXERCISE_SET.value
        and unit.id in _reached_exercise_set_ids(units, frontier)
    ):
        pass
    else:
        raise LearningNotFoundError(f"no book unit with id {unit_id}")
    return _read_unit(
        session, book_id, revision, unit, completed_at=completed_at
    )


# --- writes ----------------------------------------------------------------------------


def _insert_progress(
    session: Session,
    *,
    student_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    completed_at: datetime,
) -> bool:
    """Insert one completion row, ignoring a concurrent duplicate.

    The unique key ``(student_id, book_revision_id, book_unit_id)``
    decides the race at the database level; ``ON CONFLICT DO NOTHING``
    keeps the surrounding transaction (the passing attempt) intact and
    reports whether THIS call wrote the row.
    """
    values = dict(
        student_id=student_id,
        book_revision_id=revision_id,
        book_unit_id=unit_id,
        completed_at=completed_at,
    )
    if session.bind.dialect.name == "postgresql":
        stmt = sa_postgresql.insert(StudentBookProgress).values(**values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=("student_id", "book_revision_id", "book_unit_id")
        )
    else:
        stmt = sa_sqlite.insert(StudentBookProgress).values(**values)
        stmt = stmt.on_conflict_do_nothing()
    result = session.execute(stmt)
    return bool(result.rowcount)


def complete_unit(
    session: Session,
    user,
    book_id: uuid.UUID,
    unit_id: uuid.UUID,
    learning_context_id: uuid.UUID,
) -> StudentBookUnitRead:
    """Complete the CURRENT available content unit (student only).

    Only the walk's current unit may be completed, and only when it is a
    non-assessment learning unit — exercise sets complete through
    attempts, never here. Duplicate requests answer 409 and write no
    second completion event or row.
    """
    student, revision = _load_access(session, user, book_id, learning_context_id)
    units, completed, current, frontier = _walk(
        session, student, revision, lock=True
    )
    unit = next((unit for unit in units if unit.id == unit_id), None)
    if unit is None:
        raise LearningNotFoundError(f"no book unit with id {unit_id}")
    if unit.kind == BookUnitKind.EXERCISE_SET.value:
        raise LearningConflictError(
            "exercise sets are completed through passing attempts, "
            "not the completion endpoint"
        )
    if unit.id in completed:
        raise LearningConflictError("this unit is already completed")
    if current is None or unit.id != current.id:
        raise LearningConflictError(
            "this unit is not the current available learning unit"
        )

    now = datetime.now(timezone.utc)
    if not _insert_progress(
        session,
        student_id=student.id,
        revision_id=revision.id,
        unit_id=unit.id,
        completed_at=now,
    ):
        # A concurrent identical completion won the unique key.
        raise LearningConflictError("this unit is already completed")
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type=EVENT_BOOK_UNIT_COMPLETED,
        metadata_json=json.dumps(
            {
                "book_id": str(book_id),
                "book_revision_id": str(revision.id),
                "book_unit_id": str(unit.id),
            }
        ),
    )
    session.flush()
    logger.info(
        "book unit completed",
        extra={
            "student_id": str(student.id),
            "book_revision_id": str(revision.id),
            "book_unit_id": str(unit.id),
        },
    )
    return _read_unit(session, book_id, revision, unit, completed_at=now)


def submit_attempt(
    session: Session,
    user,
    book_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: AssessmentAttemptSubmit,
) -> AssessmentAttemptRead:
    """Grade one attempt through the P4 service (student only).

    The exercise set must be reached — at or before the walk frontier —
    before any submission is accepted. A passing attempt completes the
    checkpoint's progress in the SAME transaction as the attempt; a
    failing one records only the immutable attempt (retries unlimited).
    """
    student, revision = _load_access(
        session, user, book_id, payload.learning_context_id
    )
    units, _completed, _current, frontier = _walk(session, student, revision)
    unit = next((unit for unit in units if unit.id == unit_id), None)
    if unit is None:
        raise LearningNotFoundError(f"no book unit with id {unit_id}")
    if unit.kind != BookUnitKind.EXERCISE_SET.value:
        raise LearningConflictError(
            "only an exercise_set unit accepts attempts "
            f"(this unit is a {unit.kind})"
        )
    if unit.id not in _reached_exercise_set_ids(units, frontier):
        raise LearningConflictError(
            "this exercise set is not available yet"
        )

    result = record_attempt(
        session,
        user,
        book_id=book_id,
        revision_id=revision.id,
        unit_id=unit_id,
        payload=payload,
    )
    if result.passed:
        now = datetime.now(timezone.utc)
        _insert_progress(
            session,
            student_id=student.id,
            revision_id=revision.id,
            unit_id=unit.id,
            completed_at=now,
        )
        auth_event_repo.log_event(
            session,
            user_id=user.id,
            event_type=EVENT_BOOK_CHECKPOINT_PASSED,
            metadata_json=json.dumps(
                {
                    "book_id": str(book_id),
                    "book_revision_id": str(revision.id),
                    "book_unit_id": str(unit.id),
                    "book_attempt_id": str(result.attempt_id),
                }
            ),
        )
        session.flush()
        logger.info(
            "book checkpoint passed",
            extra={
                "student_id": str(student.id),
                "book_revision_id": str(revision.id),
                "book_unit_id": str(unit.id),
                "book_attempt_id": str(result.attempt_id),
            },
        )
    return result
