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


def _escape_like(needle: str) -> str:
    """Escape the LIKE wildcards (``%``, ``_``) and the escape character itself.

    The escaped string is embedded as ``%<escaped>%`` with
    ``escape="\\\\"`` so user input matches *literally*: a search for
    ``100%`` is not "match everything" and ``_`` is not "any character".
    The pattern always travels as a bound parameter — SQL text is never
    string-built from input.
    """
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _page_filters(*, role: str | None, status: str | None, q: str | None) -> list:
    """Where-clauses for the administrative account list.

    ``q`` case-insensitively matches ``users.email`` (same treatment the
    student list gives its searchable columns); ``role``/``status`` are
    plain equality against values the *service* already validated against
    the vocabulary, so an unknown value can never reach this layer.
    """
    conditions = []
    if role is not None:
        conditions.append(User.role == role)
    if status is not None:
        conditions.append(User.status == status)
    if q is not None:
        pattern = f"%{_escape_like(q).lower()}%"
        conditions.append(func.lower(User.email).like(pattern, escape="\\"))
    return conditions


def count_page(
    session: Session, *, role: str | None = None, status: str | None = None,
    q: str | None = None,
) -> int:
    """Total accounts matching the filters (ignores limit/offset)."""
    stmt = (
        select(func.count())
        .select_from(User)
        .where(*_page_filters(role=role, status=status, q=q))
    )
    return session.scalar(stmt) or 0


def list_page(
    session: Session, *, role: str | None = None, status: str | None = None,
    q: str | None = None, limit: int = 20, offset: int = 0,
) -> list[User]:
    """One page of accounts, newest first.

    Ordering is deterministic (``created_at`` descending with the unique
    ``id`` as tiebreaker) so offset paging stays stable even when a whole
    batch shares one ``created_at`` (rows inserted in one transaction do).
    """
    stmt = (
        select(User)
        .where(*_page_filters(role=role, status=status, q=q))
        .order_by(User.created_at.desc(), User.id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))


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


def create_admin_user(
    session: Session,
    *,
    email: str,
    password_hash: str,
    status: str = "active",
) -> User:
    """Insert a user row with the ``admin`` role (not committed).

    Deliberately separate from :func:`create_student_user`, whose role is
    hard-coded to ``student`` by design: the bootstrap path must not share
    a helper that silently forces a role, and the student path must not
    grow a role parameter. No student profile row is created — an
    administrator has no profile, and ``role``/``status`` are decided here,
    never by a request payload.

    ``password_hash`` is an already-hashed PHC Argon2id string; callers
    validate the plaintext against the password policy before hashing.
    """
    user = User(
        email=email,
        role=UserRole.ADMIN.value,
        status=status,
        password_hash=password_hash,
    )
    session.add(user)
    session.flush()  # assign the PK so the caller can log the audit event
    return user
