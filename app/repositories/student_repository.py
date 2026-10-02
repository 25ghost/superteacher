"""Data access for ``students`` (student profile).

Read/insert/update helpers only — no HTTP, no auth, no enrollment rules.
The ``students.user_id`` UNIQUE constraint (``uq_students_user_id_key``) is
the database's guard against duplicate profiles; the service translates a
violation into the API conflict convention.

Update semantics use the shared ``UNSET`` sentinel: a parameter left at
``UNSET`` is untouched, an explicit ``None`` clears that nullable column
(``full_name`` is NOT NULL and never accepts ``None``).
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.student import Student
from app.models.user import User
from app.schemas.student_profile import UNSET


def get_by_id(session: Session, student_id: uuid.UUID) -> Student | None:
    return session.get(Student, student_id)


def _escape_like(needle: str) -> str:
    """Escape the LIKE wildcards (``%``, ``_``) and the escape character itself.

    The escaped string is embedded as ``%<escaped>%`` with
    ``escape="\\\\"``, so user input is matched *literally* — a search for
    ``100%`` never turns into a "match everything" pattern, and ``_``
    never means "any single character". The pattern is always passed as a
    bound parameter; SQL text is never string-built from input.
    """
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_predicates(q: str | None) -> list:
    """Case-insensitive match of ``q`` on users.email and students.full_name."""
    if q is None:
        return []
    pattern = f"%{_escape_like(q).lower()}%"
    return [
        or_(
            func.lower(User.email).like(pattern, escape="\\"),
            func.lower(Student.full_name).like(pattern, escape="\\"),
        )
    ]


def _page_filters(
    *, q: str | None, gender: str | None, country: str | None
) -> list:
    conditions = _search_predicates(q)
    if gender is not None:
        conditions.append(Student.gender == gender)
    if country is not None:
        conditions.append(Student.country == country)
    return conditions


def count_page(
    session: Session, *, q: str | None = None, gender: str | None = None,
    country: str | None = None,
) -> int:
    """Total students matching the filters (independent of limit/offset)."""
    stmt = (
        select(func.count())
        .select_from(Student)
        .join(User, Student.user_id == User.id)
        .where(*_page_filters(q=q, gender=gender, country=country))
    )
    return session.scalar(stmt) or 0


def list_page(
    session: Session, *, q: str | None = None, gender: str | None = None,
    country: str | None = None, limit: int = 20, offset: int = 0,
) -> list[tuple[Student, User]]:
    """One page of ``(student, user)`` rows, newest first.

    The single page query joins ``users`` so email/name data arrives in
    the same round-trip (no per-row lookups). Ordering is deterministic:
    ``created_at`` descending with the unique ``students.id`` as the
    tiebreaker, so offset paging stays stable even when a whole batch
    shares one ``created_at`` (rows inserted in one transaction do).
    """
    stmt = (
        select(Student, User)
        .join(User, Student.user_id == User.id)
        .where(*_page_filters(q=q, gender=gender, country=country))
        .order_by(Student.created_at.desc(), Student.id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(stmt))


def get_by_user_id(session: Session, user_id: uuid.UUID) -> Student | None:
    return session.scalar(select(Student).where(Student.user_id == user_id))


def create(
    session: Session,
    user_id: uuid.UUID,
    full_name: str,
    date_of_birth,
    gender: str | None,
    country: str | None,
) -> Student:
    """Insert a student profile row (not committed)."""
    student = Student(
        user_id=user_id,
        full_name=full_name,
        date_of_birth=date_of_birth,
        gender=gender,
        country=country,
    )
    session.add(student)
    session.flush()
    return student


def update_profile(
    session: Session,
    student: Student,
    full_name=UNSET,
    gender=UNSET,
    country=UNSET,
) -> Student:
    """Apply supplied mutable profile fields (date_of_birth is immutable —
    it anchors identity records; changes are a future reviewed decision).

    ``UNSET`` means "not supplied, leave alone"; ``None`` on the nullable
    ``gender``/``country`` clears them. ``full_name`` is NOT NULL: a
    ``None`` value is ignored rather than persisted.
    """
    if full_name is not UNSET and full_name is not None:
        student.full_name = full_name
    if gender is not UNSET:
        student.gender = gender
    if country is not UNSET:
        student.country = country
    session.flush()
    return student
