"""Learning enrollments: join a marketplace offering, read, then leave.

Business rules (Phase A Option-A split):

- only an **active** offering accepts enrollments — a paused or archived
  offer is a 409, an unknown id a 404;
- one *active* enrollment per (student, learning context): enrolling in a
  second offering of the context the student is already in is refused with
  a message that names the remedy — leave first. That is the product's
  explicit "leave before you switch" step, backed by the partial unique
  index ``uq_learning_enrollments_student_context_active_key``;
- leaving is the only status change: ``active → ended`` with a timestamp,
  a second leave is a 409, and an enrollment that exists but belongs to
  another student 404s exactly like an unknown id (L6 existence leak);
- each transition writes exactly one ``auth_events`` row about the
  student (``actor_user_id`` None: they act on their own record).

Nothing commits here — the API layer owns the transaction.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import LearningEnrollmentStatus
from app.models.learning_enrollment import LearningEnrollment
from app.models.student import Student
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import learning_enrollment_repository as enrollment_repo
from app.repositories import teaching_offering_repository as offering_repo
from app.realtime.event_bus import get_event_bus
from app.schemas.learning import LearningEnrollmentRead
from app.services import learning_context_service
from app.services.learning_context_service import (
    LearningConflictError,
    LearningNotFoundError,
)

logger = logging.getLogger(__name__)


def _read(enrollment: LearningEnrollment) -> LearningEnrollmentRead:
    """Build the enrollment summary from its eager-loaded relations."""
    offering = enrollment.teaching_offering
    return LearningEnrollmentRead(
        **learning_context_service.read_context(enrollment.learning_context).model_dump(),
        enrollment_id=enrollment.id,
        student_id=enrollment.student_id,
        status=enrollment.status,
        started_at=enrollment.started_at,
        ended_at=enrollment.ended_at,
        created_at=enrollment.created_at,
        offering_id=offering.id,
        offering_status=offering.status,
        offering_description=offering.description,
        teacher_name=offering.teacher.full_name,
    )


def enroll(
    session: Session,
    student: Student,
    offering_id: uuid.UUID,
) -> LearningEnrollmentRead:
    """Enroll the caller into one active offering (then audit it).

    Order of operations: offering exists (404) → offering active (409) →
    no active enrollment in that context yet (409) → insert → audit. The
    duplicate pre-check mirrors the partial unique index so the 409 reads
    like a domain rule; the index stays the final protection.
    """
    offering = offering_repo.get_by_id(session, offering_id)
    if offering is None:
        raise LearningNotFoundError(f"no teaching offering with id {offering_id}")
    if offering.status != "active":
        raise LearningConflictError(
            f"teaching offering is {offering.status!r} and is not "
            "accepting enrollments"
        )

    context_id = offering.learning_context_id
    existing = enrollment_repo.find_active_for_student_context(
        session, student.id, context_id
    )
    if existing is not None:
        raise LearningConflictError(
            "you already have an active enrollment in this learning context — "
            "leave it before enrolling in another offering"
        )

    try:
        enrollment = enrollment_repo.create(
            session,
            student_id=student.id,
            teaching_offering_id=offering.id,
            learning_context_id=context_id,
        )
    except IntegrityError as exc:
        # Lost the race on uq_learning_enrollments_student_context_active_key.
        raise LearningConflictError(
            "you already have an active enrollment in this learning context — "
            "leave it before enrolling in another offering"
        ) from exc

    auth_event_repo.log_event(
        session,
        user_id=student.user_id,
        event_type="learning_enrollment_created",
        metadata_json=json.dumps(
            {
                "learning_enrollment_id": str(enrollment.id),
                "teaching_offering_id": str(offering.id),
                "learning_context_id": str(context_id),
            }
        ),
    )
    session.flush()
    logger.info(
        "learning enrollment created",
        extra={"student_id": str(student.id), "enrollment_id": str(enrollment.id)},
    )
    return _read(enrollment_repo.reload_with_relations(session, enrollment.id))


def list_my_enrollments(
    session: Session, student: Student
) -> list[LearningEnrollmentRead]:
    """The student's learning history, newest first."""
    return [
        _read(enrollment)
        for enrollment in enrollment_repo.list_for_student(session, student.id)
    ]


def _owned_enrollment(
    session: Session, student: Student, enrollment_id: uuid.UUID
) -> LearningEnrollment:
    """One of the student's enrollments — a foreign id 404s like an unknown one."""
    enrollment = enrollment_repo.get_by_id_for_update(session, enrollment_id)
    if enrollment is None or enrollment.student_id != student.id:
        raise LearningNotFoundError(f"no learning enrollment with id {enrollment_id}")
    return enrollment


def get_my_enrollment(
    session: Session, student: Student, enrollment_id: uuid.UUID
) -> LearningEnrollmentRead:
    """Read one enrollment (404 for unknown *or* foreign)."""
    return _read(_owned_enrollment(session, student, enrollment_id))


def leave(
    session: Session, student: Student, enrollment_id: uuid.UUID
) -> LearningEnrollmentRead:
    """End one active enrollment (the explicit step before switching).

    The row is locked while the decision is made, so two concurrent leaves
    cannot both succeed — the loser sees ``ended`` and gets 409. The
    offering is *not* touched: leaving is a property of the student's own
    membership, and the teacher keeps their row. Leaving also publishes
    ``connection.revoke`` (§29) in the same transaction: any live
    classroom socket of this student in this offering is closed by the
    worker that owns it, their open attendance segment is finalized, and
    nobody else is told anything (no ids, no reasons, over the wire).
    """
    enrollment = _owned_enrollment(session, student, enrollment_id)
    if enrollment.status != LearningEnrollmentStatus.ACTIVE.value:
        raise LearningConflictError(
            f"learning enrollment is already {enrollment.status!r}"
        )

    previous_status = enrollment.status
    enrollment.status = LearningEnrollmentStatus.ENDED.value
    enrollment.ended_at = datetime.now(timezone.utc)
    session.flush()

    # §29: leaving revokes live classroom access, not just future reads.
    # Published on the caller's transaction so the revoke reaches other
    # workers if and only if this commit lands; the endpoint that serves
    # the socket does the closing (it owns the transport).
    get_event_bus().publish(
        session,
        {
            "type": "connection.revoke",
            "class_id": None,
            "student_id": str(student.id),
            "teaching_offering_id": str(enrollment.teaching_offering_id),
        },
    )

    auth_event_repo.log_event(
        session,
        user_id=student.user_id,
        event_type="learning_enrollment_ended",
        metadata_json=json.dumps(
            {
                "learning_enrollment_id": str(enrollment.id),
                "status": enrollment.status,
                "previous_status": previous_status,
            }
        ),
    )
    session.flush()
    logger.info(
        "learning enrollment ended",
        extra={"student_id": str(student.id), "enrollment_id": str(enrollment.id)},
    )
    return _read(enrollment_repo.reload_with_relations(session, enrollment.id))
