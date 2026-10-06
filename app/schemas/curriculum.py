"""Pydantic schemas for Phase 2 slice 2A — topics and lessons.

Boundary: the teacher's own curriculum structure beneath a teaching
offering. Topics are outline sections; lessons are teachable steps inside
a topic. Both stay inside the offering's educational context — the
Admin-owned catalog (``learning_contexts``) is never re-declared here.

Security rules baked into these schemas:

- no request field can select a *teacher*, *offering* or *topic*: both
  come from the URL path and the Bearer token, so a body can only ever
  describe the thing being created (title, description, position);
- ``extra="forbid"`` rejects unknown keys with a 422 — including
  ``teacher_id``, ``offering_id``, ``topic_id`` and anything role-shaped;
- no response schema includes credential material.

Published/materials behaviour (slice 2B/2C) is intentionally absent:
these are teacher-authored outline rows, not moderated content.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TopicCreate(BaseModel):
    """Create a topic under one of the caller's offerings.

    ``display_order`` is optional: when omitted the service assigns the
    next free position in the offering; when supplied it must be a
    position nobody else holds (409 otherwise).
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    display_order: int | None = Field(default=None, ge=1)


class TopicUpdate(BaseModel):
    """Amend one of the caller's topics.

    At least one field must be supplied. A ``display_order`` that collides
    with another topic of the same offering is a 409 — the unique index
    keeps the stored sequence stable rather than silently reordering.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    display_order: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "TopicUpdate":
        supplied = self.model_fields_set
        if not supplied:
            raise ValueError("supply at least one of: title, description, display_order")
        if "title" in supplied and self.title is None:
            raise ValueError("title must not be null")
        if "display_order" in supplied and self.display_order is None:
            raise ValueError("display_order must not be null")
        return self


class LessonCreate(BaseModel):
    """Create a lesson under one of the caller's topics.

    Same optional-position rule as :class:`TopicCreate`.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    display_order: int | None = Field(default=None, ge=1)


class LessonUpdate(BaseModel):
    """Amend one of the caller's lessons.

    At least one field must be supplied; a colliding ``display_order`` is
    a 409 for the same reason as :class:`TopicUpdate`.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    display_order: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "LessonUpdate":
        supplied = self.model_fields_set
        if not supplied:
            raise ValueError("supply at least one of: title, description, display_order")
        if "title" in supplied and self.title is None:
            raise ValueError("title must not be null")
        if "display_order" in supplied and self.display_order is None:
            raise ValueError("display_order must not be null")
        return self


class TopicRead(BaseModel):
    """One topic — the shape both the teacher's own list and detail read."""

    topic_id: UUID
    offering_id: UUID
    title: str
    description: str | None = None
    display_order: int
    created_at: datetime
    updated_at: datetime


class LessonRead(BaseModel):
    """One lesson — carries its topic and offering ids for client routing."""

    lesson_id: UUID
    topic_id: UUID
    offering_id: UUID
    title: str
    description: str | None = None
    display_order: int
    created_at: datetime
    updated_at: datetime
