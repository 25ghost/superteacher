"""Unified teaching-authorization checks (teacher verification policy).

Every path that lets a teacher *act* — publish an offering, author
content, schedule a class, enter a classroom — and every path that lets a
student *use* teaching content must reach the same durable database state:

**eligibility** — ``teachers.verification_status == 'approved'`` AND
``teachers.verification_category`` is set AND the teacher has a completed
``approved`` application. The JWT claim is never consulted; the profile
row is re-read on every request.

**scope** — a row in ``teacher_approved_contexts`` for the exact canonical
learning context. Approved status alone grants nothing: no row, no
teaching that context. This is the canonical approved teaching scope.

Refusals reuse ``LearningForbiddenError`` (403) so existing error mapping
in offerings/materials/curriculum/online-class/classroom endpoints keeps
working; classroom WebSocket paths that need a transport-neutral boolean
use the ``*_is_*`` predicates instead of the raising variants.

Nothing commits here — the API layer owns the transaction.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    TeacherApplicationStatus,
    TeacherVerificationStatus,
)
from app.models.teacher import Teacher
from app.models.teacher_application import TeacherApplication
from app.models.teacher_approved_context import TeacherApprovedContext
from app.models.teaching_offering import TeachingOffering
from app.services.learning_context_service import LearningForbiddenError


def has_approved_application(
    session: Session, teacher_id: uuid.UUID, *, category: str | None = None
) -> bool:
    """True when the teacher has at least one completed ``approved`` application.

    When ``category`` is given it must also match the application's claimed
    category (used to confirm the profile's approved category is backed by
    the application that granted it).
    """
    stmt = select(TeacherApplication.id).where(
        TeacherApplication.teacher_id == teacher_id,
        TeacherApplication.status == TeacherApplicationStatus.APPROVED.value,
    )
    if category is not None:
        stmt = stmt.where(TeacherApplication.category == category)
    return session.scalar(stmt) is not None


def is_eligible_teacher(session: Session, profile: Teacher) -> bool:
    """Boolean eligibility: approved status + category + approved application."""
    if profile.verification_status != TeacherVerificationStatus.APPROVED.value:
        return False
    if not profile.verification_category:
        return False
    return has_approved_application(
        session, profile.id, category=profile.verification_category
    )


def require_eligible_teacher(session: Session, profile: Teacher, *, action: str) -> None:
    """Raise ``LearningForbiddenError`` unless the teacher is eligible.

    ``action`` is a short human-readable phrase ('publish teaching
    offerings', 'create teaching content', ...) used in the refusal message.
    """
    if profile.verification_status != TeacherVerificationStatus.APPROVED.value:
        raise LearningForbiddenError(
            f"teacher verification is {profile.verification_status!r}; "
            f"only an approved teacher may {action}"
        )
    if not profile.verification_category:
        raise LearningForbiddenError(
            f"teacher verification is {profile.verification_status!r} but has no "
            f"verification category; only a category-approved teacher may {action}"
        )
    if not has_approved_application(
        session, profile.id, category=profile.verification_category
    ):
        raise LearningForbiddenError(
            "teacher has no approved verification application; "
            f"only a category-approved teacher may {action}"
        )


def has_approved_context(
    session: Session, teacher_id: uuid.UUID, learning_context_id: uuid.UUID
) -> bool:
    """True when this exact (teacher, canonical context) scope row exists."""
    stmt = select(TeacherApprovedContext.id).where(
        TeacherApprovedContext.teacher_id == teacher_id,
        TeacherApprovedContext.learning_context_id == learning_context_id,
    )
    return session.scalar(stmt) is not None


def require_context_in_scope(
    session: Session,
    teacher_id: uuid.UUID,
    learning_context_id: uuid.UUID,
    *,
    action: str,
) -> None:
    """Raise unless the teacher's approved scope covers this context."""
    if not has_approved_context(session, teacher_id, learning_context_id):
        raise LearningForbiddenError(
            f"this learning context is not in your approved teaching scope; "
            f"only approved contexts may be used to {action}"
        )


def offering_is_teachable(session: Session, offering: TeachingOffering) -> bool:
    """Student/marketplace predicate: this offering may currently be taught.

    Combines teacher eligibility with per-context scope for the offering's
    canonical context. Used on enrollment, student-visible reads and any
    path that gates a *student* action on the offering still being live.
    """
    teacher = offering.teacher
    if teacher is None:
        return False
    if not is_eligible_teacher(session, teacher):
        return False
    return has_approved_context(
        session, teacher.id, offering.learning_context_id
    )
