"""Pydantic schemas for administrative user management (Phase B, slice 4).

Boundary: creating/listing teacher accounts, resending invitations,
activating/deactivating them, and changing an account's role.

Security rules baked into these schemas:

- no request field can set ``role``, ``status`` or ``password_hash`` on a
  teacher creation — those are fixed server-side (``teacher`` / ``pending``
  / none until the invitation is accepted);
- ``RoleChangeRequest`` is the *only* endpoint that accepts a role, and it
  is reachable solely by administrators;
- no response schema includes ``password_hash`` or any token material.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import UserRole
from app.schemas.student_profile import _validate_full_name


class TeacherCreate(BaseModel):
    """Administrative teacher creation (account + profile + invitation)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=255)
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    school_id: UUID | None = None
    subject: str | None = Field(default=None, max_length=120)

    @field_validator("full_name")
    @classmethod
    def _full_name_valid(cls, value: str) -> str:
        return _validate_full_name(value)


class TeacherRead(BaseModel):
    """One teacher account as the administration sees it."""

    user_id: UUID
    teacher_id: UUID
    email: str | None = None
    full_name: str
    phone: str | None = None
    school_id: UUID | None = None
    subject: str | None = None
    role: str
    status: str
    created_at: datetime
    updated_at: datetime


class AdminUserRead(BaseModel):
    """The safe projection of an account after a role change."""

    user_id: UUID
    email: str | None = None
    role: str
    status: str
    created_at: datetime


class AdminUserListRead(BaseModel):
    """One row of the administrative account list (``GET /admin/users``).

    Deliberately excludes ``password_hash`` and ``phone``: an identifier a
    caller cannot authenticate with is enough for administrative triage,
    and the lockout columns are what the list exists to surface.
    """

    id: UUID
    email: str | None = None
    role: str
    status: str
    locked_until: datetime | None = None
    failed_login_count: int
    created_at: datetime


class RoleChangeRequest(BaseModel):
    """The single request body that may choose an account's role."""

    model_config = ConfigDict(extra="forbid")

    role: UserRole


class TeacherMeRead(BaseModel):
    """The teacher's own profile (``GET /me/teacher``)."""

    user_id: UUID
    teacher_id: UUID
    email: str | None = None
    full_name: str
    phone: str | None = None
    school_id: UUID | None = None
    subject: str | None = None
    role: str
    status: str
    created_at: datetime
    updated_at: datetime


class TeacherMeUpdate(BaseModel):
    """Self-service profile update (``PATCH /me/teacher``).

    Deliberately narrow: only ``full_name``, ``phone`` and ``subject`` —
    there is no way to widen the boundary from a request body. ``school_id``
    is an administrative assignment, and role/status never appear here.
    An explicit ``null`` clears ``phone``/``subject``; ``full_name`` cannot
    be cleared. Only supplied fields change.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    subject: str | None = Field(default=None, max_length=120)

    @field_validator("full_name")
    @classmethod
    def _full_name_valid(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("full_name must not be cleared")
        return _validate_full_name(value)
