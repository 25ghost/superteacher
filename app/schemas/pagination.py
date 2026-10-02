"""Pagination envelope shared by the administrative list endpoints.

Every list response that supports ``limit``/``offset`` and reports a
``total`` uses this one shape::

    {"items": [...], "total": 123, "limit": 20, "offset": 40}

Generic over the item schema (``Page[StudentProfileRead]``), so each
endpoint keeps its own row contract while the paging counters stay
identical everywhere. Endpoints whose contract predates this envelope
(e.g. ``GET /admin/teachers``) deliberately keep returning a bare array.
"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """One page of results plus the counters needed to page through them."""

    items: list[T]
    total: int = Field(ge=0, description="Rows matching the query, ignoring limit/offset")
    limit: int = Field(ge=1, description="Page size that was applied")
    offset: int = Field(ge=0, description="Rows skipped before the first returned item")
