"""Data access for ``student_profile_history`` (profile change audit trail).

The vocabulary enforced here mirrors the database CHECK constraints added
in migration 0004: ``change_type`` ∈ {create, update} and ``field_name``
∈ {profile, full_name, gender, country}. A violation is a programming
error (500), not a client error.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.student_profile_history import StudentProfileHistory

#: Allowed change types (mirrors ``student_profile_history_change_type_check``).
CHANGE_TYPES = ("create", "update")

#: Allowed audited fields (mirrors ``student_profile_history_field_name_check``).
#: ``profile`` marks whole-profile creation events.
FIELD_NAMES = ("profile", "full_name", "gender", "country")


def log_change(
    session: Session,
    *,
    student_id: uuid.UUID,
    field_name: str,
    old_value: str | None,
    new_value: str | None,
    changed_by: uuid.UUID | None,
    change_type: str,
) -> StudentProfileHistory:
    """Record a profile change event (flushed, not committed).

    Raises ``ValueError`` on a vocabulary violation — only service-level
    callers reach this module, so a bad value is an internal defect.
    """
    if change_type not in CHANGE_TYPES:
        raise ValueError(
            f"change_type must be one of {', '.join(CHANGE_TYPES)}"
        )
    if field_name not in FIELD_NAMES:
        raise ValueError(
            f"field_name must be one of {', '.join(FIELD_NAMES)}"
        )
    record = StudentProfileHistory(
        student_id=student_id,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        changed_by=changed_by,
        change_type=change_type,
    )
    session.add(record)
    session.flush()
    return record


def list_for_student(
    session: Session,
    student_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[StudentProfileHistory]:
    """History records for one student, newest first (flushed, not committed).

    The ``id`` tiebreaker makes pagination deterministic: rows sharing a
    timestamp (possible within one transaction) keep a stable order across
    ``limit``/``offset`` pages instead of shifting between requests.
    """
    return list(
        session.scalars(
            select(StudentProfileHistory)
            .where(StudentProfileHistory.student_id == student_id)
            .order_by(
                StudentProfileHistory.created_at.desc(),
                StudentProfileHistory.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    )


def get_by_id(
    session: Session,
    record_id: uuid.UUID,
) -> StudentProfileHistory | None:
    """A single history record by id (or None)."""
    return session.get(StudentProfileHistory, record_id)
