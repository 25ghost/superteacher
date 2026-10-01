"""Pydantic schemas for authentication (Phase 5G).

Boundary: account creation (user + student profile atomically), login,
token exchange and the safe identity reads (``/me``, ``/me/student``).
Enrollment data lives in the registration module; catalog data in the
catalog module.

Security rules baked into these schemas:

- ``TokenResponse`` carries the access/refresh token pair only;
- no response schema ever includes ``password_hash`` or any credential
  material (Step 5);
- ``StudentAccountCreate`` fixes the role server-side: the caller cannot
  choose teacher or admin during ordinary student registration
  (Step 20).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.student_profile import (
    _validate_country,
    _validate_full_name,
    _validate_gender,
)

#: The complete role vocabulary — mirrors ``app.models.enums.UserRole``.
RoleLiteral = Literal["student", "teacher", "admin"]


class StudentAccountCreate(BaseModel):
    """Public student account registration (Step 19/20).

    Creates the ``users`` identity (role forced to ``student``) and the
    ``students`` profile atomically, then issues tokens. Profile-field
    validation reuses the exact ``student_profile`` helper validators
    (name charset, gender vocabulary, country pattern) so the two flows
    can never drift.

    ``extra="forbid"`` rejects unknown keys with a 422 — a caller cannot
    smuggle a ``role``/``status`` field past the role forcing below.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity — email is the authentication identifier (Step 4) and is
    # therefore REQUIRED for account creation (unlike the dev-stage profile
    # creation where it was optional). Capped at the column's 255 chars so
    # an over-long value is a 422, never a database error.
    email: EmailStr = Field(max_length=255)
    password: str = Field(max_length=128)  # policy enforced by security.validate_password_policy

    # Profile (students row) — same rules as Phase 5C.
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

    @field_validator("password", mode="after")
    @classmethod
    def _password_policy(cls, value: str) -> str:
        from app.core.security import PasswordPolicyError, validate_password_policy

        try:
            return validate_password_policy(value)
        except PasswordPolicyError as exc:
            raise ValueError(str(exc)) from exc


class ChangePasswordRequest(BaseModel):
    """Authenticated password change (self-service)."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(max_length=128)

    @field_validator("new_password", mode="after")
    @classmethod
    def _new_password_policy(cls, value: str) -> str:
        from app.core.security import PasswordPolicyError, validate_password_policy

        try:
            return validate_password_policy(value)
        except PasswordPolicyError as exc:
            raise ValueError(str(exc)) from exc


class ForgotPasswordRequest(BaseModel):
    """Request a password reset email (public, rate-limited)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=255)


class ResetPasswordRequest(BaseModel):
    """Reset password with a valid reset token (public, rate-limited)."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=4096)
    new_password: str = Field(max_length=128)

    @field_validator("new_password", mode="after")
    @classmethod
    def _new_password_policy(cls, value: str) -> str:
        from app.core.security import PasswordPolicyError, validate_password_policy

        try:
            return validate_password_policy(value)
        except PasswordPolicyError as exc:
            raise ValueError(str(exc)) from exc


class AcceptInviteRequest(BaseModel):
    """Accept a teacher invitation (public, rate-limited).

    The     token comes from the invitation email link; it is single-use and
    expires after 72 hours. Accepting sets the password, activates the
    account and issues a token pair — the same shape as ``reset-password``
    plus the login that follows.

    ``extra="forbid"`` — same strictness as the reset flow.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=4096)
    new_password: str = Field(max_length=128)

    @field_validator("new_password", mode="after")
    @classmethod
    def _new_password_policy(cls, value: str) -> str:
        from app.core.security import PasswordPolicyError, validate_password_policy

        try:
            return validate_password_policy(value)
        except PasswordPolicyError as exc:
            raise ValueError(str(exc)) from exc


class DeactivateAccountRequest(BaseModel):
    """Self-service account deactivation (requires password confirmation)."""

    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=128)


class LoginRequest(BaseModel):
    """Email + password login (Step 21)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=1, max_length=128)


class RefreshTokenRequest(BaseModel):
    """Refresh-token exchange (Step 22) and the ``/auth/logout`` body."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1, max_length=4096)


class TokenResponse(BaseModel):
    """The issued token pair (Step 5)."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access-token lifetime in seconds


class AuthUserRead(BaseModel):
    """Safe current-user identity (Step 24).

    No password hash, no secrets, no database internals. Includes the
    linked student profile summary when the user is a student. ``role`` is
    restricted to the three allowed values (``student``/``teacher``/
    ``admin``) and is always read from the authenticated account, never
    from the request.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    email: str | None = None
    phone: str | None = None
    role: RoleLiteral
    status: str
    created_at: datetime
    student: "StudentSummary | None" = None


class StudentSummary(BaseModel):
    """The student profile summary embedded in identity responses."""

    model_config = ConfigDict(from_attributes=True)

    student_id: UUID
    full_name: str
    date_of_birth: date | None = None
    gender: str | None = None
    country: str | None = None


AuthUserRead.model_rebuild()
