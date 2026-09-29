"""Data access for ``users`` (system identity).

Read/insert helpers only — no HTTP, no auth, no business rules. The
service layer decides *when* a user row is created; this module decides
*how*.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import UserRole
from app.models.user import User


def get_by_email(session: Session, email: str) -> User | None:
    """Lookup by email, case-insensitively.

    Callers pass an already-lowercased address; the ``lower()`` comparison
    matches the functional unique index ``uq_users_email_ci_key`` so any
    differently-cased row written outside the API can never slip past the
    duplicate pre-check.
    """
    return session.scalar(select(User).where(func.lower(User.email) == email))


def get_by_phone(session: Session, phone: str) -> User | None:
    return session.scalar(select(User).where(User.phone == phone))


def create_student_user(
    session: Session,
    email: str | None,
    phone: str | None,
    password_hash: str | None = None,
) -> User:
    """Insert a user row with the fixed ``student`` role (not committed).

    ``password_hash`` is an already-hashed PHC Argon2id string (never a
    plaintext password — hashing happens in ``app.core.security``).
    """
    user = User(
        email=email,
        phone=phone,
        role=UserRole.STUDENT.value,
        status="active",
        password_hash=password_hash,
    )
    session.add(user)
    session.flush()  # assign the PK so the caller can link the profile
    return user
