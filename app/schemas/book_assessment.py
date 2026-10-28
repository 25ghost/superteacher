"""Pydantic schemas for administrator objective assessment (book P4).

Boundary: administrator-only authoring, verification and threshold
configuration, plus the internal attempt-recording surface used by the
student flow (P5 adds the student route; the service and its schemas
exist now). No request field can select a reviewer, a correctness flag
or a status — those come from the URL path, the Bearer token or the
verification action. No response schema ever includes a storage key,
filesystem path or the answer key of a question on any student-shaped
payload.

Grading contract (see ``book_assessment_service``): equal weighting
(one point per question), the pass decision compares the EXACT
correct-count/total-count against the exercise set's threshold without
rounding, retries are unlimited, and a recorded attempt is immutable.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BookQuestionType


class BookQuestionOptionRead(BaseModel):
    """One answer option of one question (administrator surface).

    ``is_correct`` is the answer key and is only ever exposed on the
    administrator surface — never on a student payload.
    """

    option_id: UUID
    option_text: str
    display_order: int
    is_correct: bool
    created_at: datetime
    updated_at: datetime


class BookQuestionRead(BaseModel):
    """One objective question with its options (administrator surface)."""

    question_id: UUID
    book_revision_id: UUID
    book_unit_id: UUID
    question_type: BookQuestionType
    prompt: str
    explanation: str
    display_order: int
    answer_key_verified_by_user_id: UUID | None = None
    answer_key_verified_at: datetime | None = None
    options: list[BookQuestionOptionRead]
    created_at: datetime
    updated_at: datetime


class BookQuestionOptionCreate(BaseModel):
    """One new answer option: the text and its sibling order."""

    model_config = ConfigDict(extra="forbid")

    option_text: str = Field(min_length=1, max_length=500)
    display_order: int = Field(ge=1)


class BookQuestionCreate(BaseModel):
    """Creation of ONE question on ONE exercise-set unit.

    Options are supplied inline (2-10 for ``multiple_choice``, exactly
    2 for ``true_false`` — enforced by the service, since a CHECK cannot
    count sibling rows). Correctness is NOT part of creation: it stays
    unverified until an explicit ``verify`` action names the correct
    option.
    """

    model_config = ConfigDict(extra="forbid")

    question_type: BookQuestionType
    prompt: str = Field(min_length=1, max_length=2000)
    explanation: str = Field(min_length=1, max_length=2000)
    display_order: int = Field(ge=1)
    options: list[BookQuestionOptionCreate] = Field(min_length=2, max_length=10)


class BookQuestionCorrectRequest(BaseModel):
    """Partial correction of ONE question of a ``pending_review`` revision.

    Every field is optional and means "leave unchanged" when absent —
    an empty body is a 422 (there is nothing to correct). Supplying
    ``options`` REPLACES the whole option list (fresh rows). Any
    correction clears the question's answer key — verification state
    and option flags — and resets the owning exercise set's review
    status to ``pending``.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    explanation: str | None = Field(default=None, min_length=1, max_length=2000)
    display_order: int | None = Field(default=None, ge=1)
    options: list[BookQuestionOptionCreate] | None = Field(
        default=None, min_length=2, max_length=10
    )


class BookQuestionVerifyRequest(BaseModel):
    """Explicit answer-key verification: name THE correct option.

    The option must belong to this question (else 404); exactly one
    option of the question becomes correct (the others are cleared —
    the partial unique index also refuses two correct options at the
    database level).
    """

    model_config = ConfigDict(extra="forbid")

    correct_option_id: UUID


class BookUnitThresholdRequest(BaseModel):
    """Configure ONE exercise set's passing threshold (percent, 1-100).

    ``null`` restores the default 70. Only meaningful on an
    ``exercise_set`` unit of a ``pending_review`` revision; any change
    resets the unit's review status to ``pending``.
    """

    model_config = ConfigDict(extra="forbid")

    passing_threshold_percent: int | None = Field(default=None, ge=1, le=100)


class AssessmentAnswerSubmit(BaseModel):
    """One submitted answer: the option the student selected."""

    model_config = ConfigDict(extra="forbid")

    question_id: UUID
    option_id: UUID


class AssessmentAttemptSubmit(BaseModel):
    """A complete attempt submission at ONE exercise set.

    Exactly one answer per question of the set — no unknowns, no
    duplicates, no omissions (all refused 422 BEFORE anything is
    persisted). Retries are unlimited: each accepted submission is a
    new immutable attempt.
    """

    model_config = ConfigDict(extra="forbid")

    learning_context_id: UUID
    answers: list[AssessmentAnswerSubmit] = Field(min_length=1)


class AssessmentQuestionFeedback(BaseModel):
    """Corrective feedback for ONE wrongly answered question.

    Carries the admin-authored ``explanation`` only — never the correct
    option id, never option correctness flags, never the answer key.
    """

    question_id: UUID
    explanation: str


class AssessmentAttemptRead(BaseModel):
    """The immutable result of one accepted attempt (student surface).

    Equal weighting: one point per question. ``score_percent`` is the
    exact unrounded fraction; ``passed`` is the frozen comparison of
    that exact fraction against the threshold. ``feedback`` explains
    each wrong answer without exposing the key.
    """

    attempt_id: UUID
    book_revision_id: UUID
    book_unit_id: UUID
    total_questions: int
    correct_answers: int
    score_percent: float
    passed: bool
    submitted_at: datetime
    feedback: list[AssessmentQuestionFeedback]
