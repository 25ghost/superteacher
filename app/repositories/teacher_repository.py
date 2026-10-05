"""Data access for ``teachers`` (teacher profile rows).

Kept separate from :mod:`app.repositories.user_repository`: that module
answers questions about *accounts* (role/status paging, the admin list),
this one about the profile row an account is paired with. The school is
joined here because every consumer of the profile needs the assignment
resolved in the same round-trip (``school_id`` alone would mean a second
query, per row, on every read).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.school import School
from app.models.teacher import Teacher


def get_profile_with_school(
    session: Session, user_id: uuid.UUID
) -> tuple[Teacher, School | None] | None:
    """The teacher profile for ``user_id`` with its assigned school.

    One statement whatever the answer: the outer join yields
    ``(profile, None)`` when no school is assigned and ``None`` when the
    profile row is missing (the API always creates account and profile
    together, so a miss means the rows were edited outside the API).
    """
    stmt = (
        select(Teacher, School)
        .outerjoin(School, School.id == Teacher.school_id)
        .where(Teacher.user_id == user_id)
    )
    return session.execute(stmt).first()
