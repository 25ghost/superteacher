"""Shared timestamp columns for ORM models.

- ``created_at``: set once by the database on INSERT.
- ``updated_at``: set on INSERT and refreshed by the database on UPDATE
  (``onupdate`` covers ORM updates; the database default covers raw SQL).

All timestamps are timezone-aware (``timestamptz`` in PostgreSQL).
"""
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """Adds created_at / updated_at columns to a model."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
