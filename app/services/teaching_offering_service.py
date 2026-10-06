"""Teaching offerings: publish, read and maintain one teacher's offers.

Business rules live here (Phase A Option-A split); the endpoint stays thin
and the repository only writes rows:

- only an **approved** teacher may publish — an account that can log in is
  not enough, so vetting stays meaningful after invitation acceptance;
- one *active* offer per (teacher, context): a duplicate is a 409 before
  the INSERT and a partial-unique-index violation under a race;
- status moves active → paused → archived (either side may archive);
  ``archived`` is terminal and a no-op transition is a 409, never a
  silent 200;
- every mutation writes exactly one ``auth_events`` row about the *teacher*
  (``actor_user_id`` is None: the teacher acts on their own record).

Nothing commits here — the API layer owns the transaction.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import TeacherVerificationStatus, TeachingOfferingStatus, UserRole
from app.models.teacher import Teacher
from app.models.teaching_offering import TeachingOffering
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import teaching_offering_repository as offering_repo
from app.schemas.learning import (
    TeachingOfferingCreate,
    TeachingOfferingRead,
    TeachingOfferingUpdate,
)
from app.services import learning_context_service
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)

logger = logging.getLogger(__name__)

#: Allowed status moves, as ``current -> frozenset(next ...)``. Anything
#: outside this map (including every move out of ``archived``) is refused.
_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    TeachingOfferingStatus.ACTIVE.value: frozenset(
        {TeachingOfferingStatus.PAUSED.value, TeachingOfferingStatus.ARCHIVED.value}
    ),
    TeachingOfferingStatus.PAUSED.value: frozenset(
        {TeachingOfferingStatus.ACTIVE.value, TeachingOfferingStatus.ARCHIVED.value}
    ),
    TeachingOfferingStatus.ARCHIVED.value: frozenset(),
}


def _load_profile(session: Session, user: User) -> Teacher:
    """The caller's teacher profile (404 when the account has none)."""
    if user.role != UserRole.TEACHER.value:
        raise LearningForbiddenError("teacher role required for this operation")
    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    if profile is None:
        raise LearningNotFoundError("no teacher profile for this account")
    return profile


def _require_approved(profile: Teacher) -> None:
    """Veto every publishing attempt by a teacher who is not approved."""
    if profile.verification_status != TeacherVerificationStatus.APPROVED.value:
        raise LearningForbiddenError(
            f"teacher verification is {profile.verification_status!r}; "
            "only an approved teacher may publish teaching offerings"
        )


def _read(offering: TeachingOffering) -> TeachingOfferingRead:
    """Build the offering summary from its eager-loaded relations."""
    return TeachingOfferingRead(
        **learning_context_service.read_context(offering.learning_context).model_dump(),
        offering_id=offering.id,
        teacher_id=offering.teacher_id,
        teacher_name=offering.teacher.full_name,
        description=offering.description,
        status=offering.status,
        created_at=offering.created_at,
        updated_at=offering.updated_at,
    )


def create_offering(
    session: Session, user: User, payload: TeachingOfferingCreate
) -> TeachingOfferingRead:
    """Publish one offering (context resolved/created first), then audit it."""
    profile = _load_profile(session, user)
    _require_approved(profile)

    context = learning_context_service.resolve_context(session, payload)

    existing = offering_repo.find_active_for_teacher_context(
        session, profile.id, context.id
    )
    if existing is not None:
        raise LearningConflictError(
            "you already have an active offering for this learning context"
        )

    try:
        offering = offering_repo.create(
            session,
            teacher_id=profile.id,
            learning_context_id=context.id,
            description=payload.description,
        )
    except IntegrityError as exc:
        # Lost the race on uq_teaching_offerings_teacher_context_active_key.
        raise LearningConflictError(
            "you already have an active offering for this learning context"
        ) from exc

    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type="teaching_offering_created",
        metadata_json=json.dumps(
            {
                "offering_id": str(offering.id),
                "learning_context_id": str(context.id),
            }
        ),
    )
    session.flush()
    logger.info(
        "teaching offering created",
        extra={"user_id": str(user.id), "offering_id": str(offering.id)},
    )
    return _read(offering_repo.reload_with_relations(session, offering.id))


def list_marketplace_offerings(
    session: Session,
    *,
    academic_year_id: uuid.UUID | None = None,
    pathway_id: uuid.UUID | None = None,
    education_level_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[TeachingOfferingRead]:
    """Discoverable offerings: active, taught by an approved teacher.

    The verification filter lives in the repository query, so suspending a
    teacher hides their offerings on the next read without mutating rows.
    """
    return [
        _read(offering)
        for offering in offering_repo.list_active_for_marketplace(
            session,
            academic_year_id=academic_year_id,
            pathway_id=pathway_id,
            education_level_id=education_level_id,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
    ]


def list_my_offerings(session: Session, user: User) -> list[TeachingOfferingRead]:
    """Every offering the caller owns, newest first."""
    profile = _load_profile(session, user)
    return [
        _read(offering)
        for offering in offering_repo.list_for_teacher(session, profile.id)
    ]


def _owned_offering(
    session: Session, profile: Teacher, offering_id: uuid.UUID
) -> TeachingOffering:
    """One of the caller's offerings — a foreign id 404s like an unknown one.

    UUIDs are identifiers, not authorization: answering 403 for someone
    else's id would confirm that it exists.
    """
    offering = offering_repo.get_by_id_for_update(session, offering_id)
    if offering is None or offering.teacher_id != profile.id:
        raise LearningNotFoundError(f"no teaching offering with id {offering_id}")
    return offering


def get_my_offering(
    session: Session, user: User, offering_id: uuid.UUID
) -> TeachingOfferingRead:
    """Read one of the caller's offerings (404 for unknown *or* foreign)."""
    profile = _load_profile(session, user)
    return _read(_owned_offering(session, profile, offering_id))


def update_my_offering(
    session: Session,
    user: User,
    offering_id: uuid.UUID,
    payload: TeachingOfferingUpdate,
) -> TeachingOfferingRead:
    """Amend description and/or status of one of the caller's offerings.

    The row is locked while the change is decided, so two concurrent
    transitions cannot both win. ``status`` (when supplied) must name a
    different, reachable value; ``description`` alone may clear the text.
    Exactly one audit event is written: ``teaching_offering_status_changed``
    when the status drives the call, otherwise ``teaching_offering_updated``.
    """
    profile = _load_profile(session, user)
    offering = _owned_offering(session, profile, offering_id)

    sets_status = "status" in payload.model_fields_set
    previous_status = offering.status

    if sets_status:
        new_status = payload.status.value
        if new_status == previous_status:
            raise LearningConflictError(f"teaching offering is already {new_status!r}")
        allowed = _STATUS_TRANSITIONS.get(previous_status, frozenset())
        if new_status not in allowed:
            raise LearningConflictError(
                f"teaching offering cannot move from {previous_status!r} "
                f"to {new_status!r}"
            )
        offering.status = new_status

    if "description" in payload.model_fields_set:
        offering.description = payload.description

    session.flush()
    auth_event_repo.log_event(
        session,
        user_id=profile.user_id,
        event_type=(
            "teaching_offering_status_changed"
            if sets_status
            else "teaching_offering_updated"
        ),
        metadata_json=json.dumps(
            {
                "offering_id": str(offering.id),
                "status": offering.status,
                "previous_status": previous_status,
            }
        ),
    )
    session.flush()
    logger.info(
        "teaching offering updated",
        extra={"user_id": str(user.id), "offering_id": str(offering.id)},
    )
    return _read(offering_repo.reload_with_relations(session, offering.id))
