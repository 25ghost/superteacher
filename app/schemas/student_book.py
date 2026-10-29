"""Pydantic schemas for book P5 — student guided learning + progression.

Boundary: the *student's own* guided walk through an
administrator-published book revision. The tree behind a publication is
deliberately NOT a listing surface — the student sees one unit at a
time (the current position), the units they already completed, and the
optional exercise sets they have reached; everything else answers 404.

Security rules baked into these schemas:

- no request field can select a *student*, a *revision* or an
  assessment outcome: identity comes from the Bearer token, the
  revision is always the book's CURRENT publication resolved
  server-side, and completion/pass verdicts come from the service;
- ``extra="forbid"`` rejects unknown keys with a 422;
- no payload ever exposes a storage key, filesystem path, question
  ``explanation`` before an attempt, option ``is_correct`` flags or any
  other answer-key field — those stay on the administrator surface;
- the reviewed ``assessment_requirement`` designation IS exposed (that
  is what makes the optional status clear), but never as free text or
  an inferred flag.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import AssessmentRequirement, BookQuestionType, BookUnitKind


class StudentBookListItemRead(BaseModel):
    """One book in the student's guided-learning library for a context."""

    book_id: UUID
    title: str
    description: str | None = None
    published_revision_id: UUID
    revision_number: int
    total_units: int
    completed_units: int
    created_at: datetime
    updated_at: datetime


class StudentBookDetailRead(BaseModel):
    """One book's identity and the student's progress summary.

    Counts only — the unit tree itself is never part of this shape.
    """

    book_id: UUID
    title: str
    description: str | None = None
    published_revision_id: UUID
    revision_number: int
    total_units: int
    completed_units: int
    created_at: datetime
    updated_at: datetime


class StudentQuestionOptionRead(BaseModel):
    """One answer option of one question (student surface).

    ``is_correct`` is the answer key and is never part of this shape —
    it only ever arrives implicitly through attempt feedback.
    """

    option_id: UUID
    option_text: str
    display_order: int


class StudentQuestionRead(BaseModel):
    """One objective question of an exercise set (student surface).

    ``explanation`` is excluded on purpose: it is the per-question
    coaching material the P4 attempt feedback reveals for wrong answers,
    after the attempt is graded — not part of the pre-attempt payload.
    """

    question_id: UUID
    question_type: BookQuestionType
    prompt: str
    display_order: int
    options: list[StudentQuestionOptionRead]


class StudentBookUnitProgressRead(BaseModel):
    """The caller's completion record for one unit (when completed)."""

    completed_at: datetime


class StudentBookUnitRead(BaseModel):
    """ONE delivered learning unit — the current step or a revisit.

    ``assessment_requirement`` is the reviewed designation, verbatim
    from the published revision: ``required`` on an ``exercise_set``
    marks a checkpoint that must be passed before the walk moves on;
    ``not_required`` marks optional practice that never blocks. Content
    units carry the designation their review recorded (never inferred).

    ``questions`` is populated only for ``exercise_set`` units, shaped
    for attempt submission (ids and option text, no answer key).
    """

    book_id: UUID
    published_revision_id: UUID
    unit_id: UUID
    parent_unit_id: UUID | None = None
    kind: BookUnitKind
    title: str
    content: str
    display_order: int
    assessment_requirement: AssessmentRequirement
    passing_threshold_percent: int | None = None
    progress: StudentBookUnitProgressRead | None = None
    questions: list[StudentQuestionRead] = []


class StudentBookNextRead(BaseModel):
    """The student's current position in the guided walk.

    ``unit`` is the single next available learning unit, or ``None``
    when every unit of the published revision is completed
    (``finished=True``).
    """

    book_id: UUID
    published_revision_id: UUID
    finished: bool
    unit: StudentBookUnitRead | None = None
