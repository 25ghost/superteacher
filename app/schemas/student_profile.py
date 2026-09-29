"""Pydantic schemas for the student profile API.

Boundary (Phase 5C): a *student profile* is the personal identity data in
``students`` — full name, date of birth, gender, country. System identity
lives in ``users`` and is created atomically with the profile by the
service layer. School/pathway/program/enrollment data deliberately has no
place here (it belongs to the registration and catalog modules), and
learning data (XP, streaks, progress) belongs to future modules.

Validation mirrors the database's existing constraints — it never
duplicates them with new rules:

- ``students.full_name`` is NOT NULL → required, non-blank, and (hardening)
  restricted to letters/spaces/hyphens/apostrophes/periods so control
  characters, markup and digits can never reach the database,
- ``users.email`` / ``users.phone`` are UNIQUE → duplicates surface as a
  conflict (409), not an integrity-error traceback; email is capped at the
  column's 255 characters so an over-long value is a 422, never a database
  error, and phones are canonicalised to E.164-with-``+`` so format
  variants cannot mint duplicate identities,
- ``users.role`` is fixed to ``student`` by this API; callers cannot create
  teacher or admin identities through it,
- an email is REQUIRED for administrative profile creation: phone-only
  identities could never authenticate or recover a password (login and
  password reset are email-based), so they are refused at the boundary.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# Gender vocabulary is a small, non-discriminatory set held at the schema
# level AND mirrored by the ``students_gender_check`` database CHECK
# constraint (migration 0004), so both API callers and direct database
# writes are confined to the same vocabulary. Extending it is a schema +
# migration change, not a schema-only one.
GENDER_VALUES = ("female", "male", "other", "undisclosed")

# Phone validation: E.164-like pattern (optional +, 1-15 digits).
_PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{1,14}$")

# Country validation: letters, spaces, hyphens, periods, apostrophes only.
_COUNTRY_PATTERN = re.compile(r"^[a-zA-ZÀ-ɏ\s\-'.]+$")

# Characters allowed inside a full name, in addition to Unicode letters.
_NAME_PUNCTUATION = set(" -.'")

#: Sentinel distinguishing "field not supplied" from an explicit ``None``
#: (which means *clear this nullable field*). Shared by the schema, service
#: and repository layers so PATCH updates can tell omission from clearing.
#: Always identity-compared (``field is UNSET``) — never called or valued.
UNSET = object()


# ---------------------------------------------------------------------------
# Shared validators reused by StudentProfileCreate / StudentProfileUpdate /
# StudentAccountCreate (one definition — the flows can never drift).
# ---------------------------------------------------------------------------


def _validate_full_name(value: str) -> str:
    """Strip, reject blank names, and enforce the name character set.

    Allowed: Unicode letters, spaces, hyphens, apostrophes and periods —
    at least one letter must be present. Control characters (including NUL
    and newlines), digits, markup and other symbols are refused here so a
    bad value becomes a readable 422 instead of a database error or stored
    garbage.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("full_name must not be empty")
    if not any(ch.isalpha() for ch in stripped):
        raise ValueError("full_name must contain at least one letter")
    for ch in stripped:
        if ch.isalpha() or ch in _NAME_PUNCTUATION:
            continue
        raise ValueError(
            "full_name may only contain letters, spaces, hyphens, "
            "apostrophes, and periods"
        )
    return stripped


def _validate_full_name_optional(value: str | None) -> str | None:
    """Strip and reject blank full names, pass through None."""
    if value is None:
        return None
    return _validate_full_name(value)


def _validate_gender(value: str | None) -> str | None:
    """Normalise gender to lowercase and enforce vocabulary."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in GENDER_VALUES:
        raise ValueError(
            f"gender must be one of {', '.join(GENDER_VALUES)}"
        )
    return normalized


def _validate_phone(value: str | None) -> str | None:
    """Validate and canonicalise to E.164-with-``+``; collapse blanks to None.

    Canonical form (leading ``+``, digits after it) means ``250700000001``
    and ``+250700000001`` resolve to the same identity instead of creating
    duplicate accounts.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if not _PHONE_PATTERN.match(stripped):
        raise ValueError(
            "phone must be in E.164 format (e.g. +250700000001)"
        )
    if not stripped.startswith("+"):
        stripped = f"+{stripped}"
    return stripped


def _validate_country(value: str | None) -> str | None:
    """Validate country format, collapse blanks to None."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if not _COUNTRY_PATTERN.match(stripped):
        raise ValueError(
            "country must contain only letters, spaces, hyphens, "
            "periods, or apostrophes"
        )
    return stripped


class StudentProfileCreate(BaseModel):
    """Request body for creating a user identity + student profile pair.

    Administrative creation (``POST /admin/students``): an email is REQUIRED —
    phone remains an optional secondary contact. Phone-only identities are
    refused because login and password reset are email-based and such an
    account could never authenticate.
    """

    # Identity (users row) — email required (see class docstring).
    email: EmailStr = Field(max_length=255)
    phone: str | None = Field(default=None, max_length=32)

    # Profile (students row)
    full_name: str = Field(min_length=1, max_length=200)
    date_of_birth: date
    gender: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=80)

    @field_validator("full_name")
    @classmethod
    def _full_name_valid(cls, value: str) -> str:
        return _validate_full_name(value)

    @field_validator("gender")
    @classmethod
    def _gender_in_vocabulary(cls, value: str | None) -> str | None:
        return _validate_gender(value)

    @field_validator("phone")
    @classmethod
    def _phone_valid_format(cls, value: str | None) -> str | None:
        return _validate_phone(value)

    @field_validator("country")
    @classmethod
    def _country_valid_format(cls, value: str | None) -> str | None:
        return _validate_country(value)


class StudentProfileSelfCreate(BaseModel):
    """Request body for self-service profile creation (``POST /me/student``).

    The caller's account already exists — identity anchors (email, phone)
    live on the ``users`` row and are deliberately NOT accepted here; this
    endpoint attaches the *profile* half of the pair to the authenticated
    student. Same validators as the administrative and registration paths
    (shared helpers — the three create flows can never drift).

    ``extra="forbid"`` rejects unknown keys with a 422 instead of silently
    ignoring them.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=200)
    date_of_birth: date
    gender: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=80)

    @field_validator("full_name")
    @classmethod
    def _full_name_valid(cls, value: str) -> str:
        return _validate_full_name(value)

    @field_validator("gender")
    @classmethod
    def _gender_in_vocabulary(cls, value: str | None) -> str | None:
        return _validate_gender(value)

    @field_validator("country")
    @classmethod
    def _country_valid_format(cls, value: str | None) -> str | None:
        return _validate_country(value)


class StudentProfileUpdate(BaseModel):
    """Request body for PATCH profile updates (Phase 5G).

    All fields optional — only *supplied* fields change (the endpoint reads
    ``model_fields_set``). An explicit ``null`` for ``gender`` or
    ``country`` CLEARS that nullable field; omitting the field leaves it
    untouched. ``full_name`` is NOT NULL and cannot be cleared — supplying
    ``null`` for it is a validation error.

    ``extra="forbid"``: unknown keys — including the identity anchors
    ``email``/``phone``/``date_of_birth`` — are a 422 instead of being
    silently dropped, so a client typo can never masquerade as a successful
    no-op.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    gender: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=80)

    @field_validator("full_name")
    @classmethod
    def _full_name_not_blank(cls, value: str | None) -> str | None:
        return _validate_full_name_optional(value)

    @field_validator("gender")
    @classmethod
    def _gender_in_vocabulary(cls, value: str | None) -> str | None:
        return _validate_gender(value)

    @field_validator("country")
    @classmethod
    def _country_valid_format(cls, value: str | None) -> str | None:
        return _validate_country(value)

    @model_validator(mode="after")
    def _full_name_cannot_be_cleared(self) -> Self:
        if "full_name" in self.model_fields_set and self.full_name is None:
            raise ValueError(
                "full_name cannot be cleared — supply a non-empty string"
            )
        return self


class StudentProfileRead(BaseModel):
    """Safe read model for a student profile pair."""

    model_config = ConfigDict(from_attributes=True)

    student_id: UUID
    user_id: UUID
    email: str | None = None
    phone: str | None = None
    role: str
    full_name: str
    date_of_birth: date | None = None
    gender: str | None = None
    country: str | None = None
    created_at: datetime
    updated_at: datetime


class StudentProfileHistoryRead(BaseModel):
    """Safe read model for a profile change history record.

    ``changed_by`` is the acting user's id; ``changed_by_email`` resolves it
    to a human-readable address for administrative review (None when the
    actor was removed — the FK is ``ON DELETE SET NULL``).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    student_id: UUID
    field_name: str
    old_value: str | None = None
    new_value: str | None = None
    changed_by: UUID | None = None
    changed_by_email: str | None = None
    change_type: str
    created_at: datetime
