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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.student import Student
from app.schemas.student_profile import UNSET


def get_by_id(session: Session, student_id: uuid.UUID) -> Student | None:
    return session.get(Student, student_id)


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
