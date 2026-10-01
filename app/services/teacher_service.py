"""Teacher self-service (Phase B, slice 5) — ``GET|PATCH /me/teacher``.

The teacher's own profile only: the identity comes from the Bearer token,
never from the path or body, so a teacher can only ever read or update
their own row (a student or administrator gets 403 from the router guard
before any handler runs).

Boundary: ``full_name``, ``phone``, ``subject``. ``school_id`` is an
administrative assignment; role and status are not writable here — role
changes belong to ``PATCH /admin/users/{id}/role``, status to the admin
teacher endpoints.

Raises the shared ``auth_service.AuthError`` family (404 when the caller
has no teacher profile, which the router maps to HTTP).
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.teacher import Teacher
from app.models.user import User
from app.schemas.teacher_admin import TeacherMeRead, TeacherMeUpdate
from app.services.auth_service import AuthNotFoundError

logger = logging.getLogger(__name__)


def _load_profile(session: Session, user: User) -> Teacher:
    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    if profile is None:
        raise AuthNotFoundError(
            "no teacher profile for this account — ask an administrator "
            "to create one"
        )
    return profile


def _read(session: Session, user: User, profile: Teacher) -> TeacherMeRead:
    return TeacherMeRead(
        user_id=user.id,
        teacher_id=profile.id,
        email=user.email,
        full_name=profile.full_name,
        phone=profile.phone,
        school_id=profile.school_id,
        subject=profile.subject,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        updated_at=profile.updated_at,
    )


def read_teacher_profile(session: Session, user: User) -> TeacherMeRead:
    """The authenticated teacher's profile (404 when none exists)."""
    return _read(session, user, _load_profile(session, user))


def update_teacher_profile(
    session: Session, user: User, payload: TeacherMeUpdate
) -> TeacherMeRead:
    """Apply only the supplied fields to the caller's own profile.

    An explicit ``null`` on ``phone``/``subject`` clears that column;
    ``full_name`` cannot be cleared (schema-level 422). The caller commits.
    """
    profile = _load_profile(session, user)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(profile, field, value)
    session.flush()
    if changes:
        logger.info(
            "teacher profile updated",
            extra={"user_id": str(user.id), "fields": sorted(changes)},
        )
    return _read(session, user, profile)
