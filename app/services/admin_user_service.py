"""Administrative account lifecycle (Phase B, slice 4).

Service layer for the ``/admin/teachers`` and ``/admin/users/{id}/role``
routes: create a teacher account (pending, invited), list teachers, re-send
or cancel invitations, activate/deactivate accounts, and change roles.

Invariants enforced here (never in a request body):

- the role is fixed to ``teacher`` on creation and can only change through
  :func:`change_user_role`, which requires an administrator actor;
- a new account starts ``pending`` with no password — it cannot
  authenticate (``_AUTHENTICATABLE_STATUSES`` is ``{active}``) until the
  invitation is accepted;
- an administrator can never change their own role (self-lockout guard);
- every mutation writes an ``auth_events`` row naming the *acting*
  administrator (``actor_user_id``) as well as the subject;
- status changes and role changes revoke every outstanding refresh session,
  so an old token cannot outlive the decision.

Raises the shared ``auth_service.AuthError`` family, which the endpoint
layer maps to HTTP status codes (404/409/403/422).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import security
from app.models.enums import UserStatus, UserRole
from app.models.invite_token import InviteToken
from app.models.teacher import Teacher
from app.models.user import User
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import user_repository as user_repo
from app.schemas.teacher_admin import AdminUserRead, TeacherCreate, TeacherRead
from app.services.auth_service import (
    AuthConflictError,
    AuthForbiddenError,
    AuthNotFoundError,
    revoke_all_sessions,
)

logger = logging.getLogger(__name__)

#: Invitation links are valid for 72 hours (stated in the email and README).
INVITE_TTL = timedelta(hours=72)

_CREATE_TAKEN_DETAIL = "email cannot be used to create an account"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- read helpers --------------------------------------------------------------------


def _load_teacher_user(session: Session, user_id: uuid.UUID) -> User:
    """The teacher account for ``user_id`` (404 when unknown or not a teacher)."""
    user = session.get(User, user_id)
    if user is None or user.role != UserRole.TEACHER.value:
        raise AuthNotFoundError("teacher account not found")
    return user


def _build_teacher_read(user: User, profile: Teacher | None) -> TeacherRead:
    """Map an account + its profile row onto ``TeacherRead``.

    The profile is mandatory data — the API always creates both rows
    together — so its absence means the rows were edited outside the
    API, exactly like the single-row read has always treated it.
    """
    if profile is None:  # pragma: no cover - only reachable on hand-edited data
        raise AuthNotFoundError("teacher profile not found")
    return TeacherRead(
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
        updated_at=user.updated_at,
    )


def _read_teacher(session: Session, user: User) -> TeacherRead:
    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    return _build_teacher_read(user, profile)


def _read_user(user: User) -> AdminUserRead:
    return AdminUserRead(
        user_id=user.id,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
    )


# --- invitations ---------------------------------------------------------------------


def _issue_invite(session: Session, user: User, *, actor: User) -> None:
    """Mint a fresh single-use invite token and email it (best-effort).

    Previous unused tokens for the same account are marked used, so only
    the newest link works. The raw token leaves the process through the
    email call alone: the database keeps the SHA-256 digest.
    """
    now = _now()
    stale = session.scalars(
        select(InviteToken).where(
            InviteToken.user_id == user.id,
            InviteToken.used_at.is_(None),
        )
    ).all()
    for row in stale:
        row.used_at = now

    raw_token = security.create_invite_token(user.id)
    session.add(
        InviteToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=now + INVITE_TTL,
        )
    )
    session.flush()

    try:
        from app.core.config import get_settings
        from app.core.email import send_teacher_invite_email

        send_teacher_invite_email(
            to_email=str(user.email),
            invite_token=raw_token,
            frontend_url=get_settings().FRONTEND_URL,
        )
    except Exception:
        # Best effort, same as the reset email: never leak send failures to
        # the caller, never log the token itself.
        logger.exception(
            "failed to send teacher invitation email", extra={"user_id": str(user.id)}
        )

    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="teacher_invite_sent",
        actor_user_id=actor.id,
    )


# --- create / list -------------------------------------------------------------------


def create_teacher(
    session: Session, payload: TeacherCreate, *, actor: User
) -> TeacherRead:
    """Create a ``pending`` teacher account + profile, then send the invite.

    The caller commits; a raise after any insert is rolled back by the
    endpoint, so no half-created account survives.
    """
    email = str(payload.email).strip().lower()
    if user_repo.get_by_email(session, email) is not None:
        raise AuthConflictError(_CREATE_TAKEN_DETAIL)

    user = User(
        email=email,
        role=UserRole.TEACHER.value,
        status=UserStatus.PENDING.value,
        password_hash=None,
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        logger.warning("duplicate email race lost for a new teacher account")
        raise AuthConflictError(_CREATE_TAKEN_DETAIL) from exc

    session.add(
        Teacher(
            user_id=user.id,
            full_name=payload.full_name,
            phone=payload.phone,
            school_id=payload.school_id,
            subject=payload.subject,
        )
    )
    session.flush()

    _issue_invite(session, user, actor=actor)
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="teacher_created",
        actor_user_id=actor.id,
    )
    session.flush()
    logger.info("teacher account created", extra={"user_id": str(user.id)})
    return _read_teacher(session, user)


def list_teachers(
    session: Session, *, limit: int = 50, offset: int = 0
) -> list[TeacherRead]:
    """All teacher accounts, newest first (administrative read).

    One joined statement fetches account and profile together — the
    per-row profile lookup of :func:`_read_teacher` used to cost one
    extra query per teacher, so the list grew linearly. The
    ``outerjoin`` preserves the single-row contract: a teacher account
    whose profile row is missing (hand-edited data) is an error, never
    a silently skipped row.
    """
    rows = session.execute(
        select(User, Teacher)
        .outerjoin(Teacher, Teacher.user_id == User.id)
        .where(User.role == UserRole.TEACHER.value)
        .order_by(User.created_at.desc(), User.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [_build_teacher_read(user, profile) for user, profile in rows]


# --- invite / activate / deactivate --------------------------------------------------


def resend_invite(session: Session, user_id: uuid.UUID, *, actor: User) -> TeacherRead:
    """Issue a new invitation link for an account still awaiting acceptance."""
    user = _load_teacher_user(session, user_id)
    if user.status != UserStatus.PENDING.value:
        raise AuthConflictError(
            "only an account awaiting invitation can be re-invited"
        )
    _issue_invite(session, user, actor=actor)
    session.flush()
    return _read_teacher(session, user)


def activate_teacher(session: Session, user_id: uuid.UUID, *, actor: User) -> TeacherRead:
    """pending/suspended → active (the account may now authenticate)."""
    user = _load_teacher_user(session, user_id)
    if user.status == UserStatus.ACTIVE.value:
        raise AuthConflictError("account is already active")
    user.status = UserStatus.ACTIVE.value
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="teacher_activated",
        actor_user_id=actor.id,
    )
    session.flush()
    logger.info(
        "teacher account activated",
        extra={"user_id": str(user.id), "actor_id": str(actor.id)},
    )
    return _read_teacher(session, user)


def deactivate_teacher(
    session: Session, user_id: uuid.UUID, *, actor: User
) -> TeacherRead:
    """active/pending → suspended, with every refresh session revoked."""
    user = _load_teacher_user(session, user_id)
    if user.status == UserStatus.SUSPENDED.value:
        raise AuthConflictError("account is already suspended")
    user.status = UserStatus.SUSPENDED.value
    revoked = revoke_all_sessions(session, user.id)
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type="teacher_deactivated",
        actor_user_id=actor.id,
        metadata_json=json.dumps({"revoked_sessions": revoked}),
    )
    session.flush()
    logger.info(
        "teacher account deactivated",
        extra={"user_id": str(user.id), "actor_id": str(actor.id)},
    )
    return _read_teacher(session, user)


# --- lockout --------------------------------------------------------------------------


def unlock_user(session: Session, user_id: uuid.UUID, *, actor: User) -> AdminUserRead:
    """Clear a login lockout early (administrative, Phase B slice 7).

    Resets ``failed_login_count`` and ``locked_until`` so the account gets
    a fresh budget immediately. 404 unknown id; 409 when the account is
    not locked (the endpoint never invents work).
    """
    target = session.get(User, user_id)
    if target is None:
        raise AuthNotFoundError("user not found")
    if not target.failed_login_count and target.locked_until is None:
        raise AuthConflictError("account is not locked")

    target.failed_login_count = 0
    target.locked_until = None
    auth_event_repo.log_event(
        session,
        user_id=target.id,
        event_type="user_unlocked",
        actor_user_id=actor.id,
    )
    session.flush()
    logger.info(
        "user unlocked",
        extra={"user_id": str(target.id), "actor_id": str(actor.id)},
    )
    return _read_user(target)


# --- role change -------------------------------------------------------------------------


def change_user_role(
    session: Session,
    user_id: uuid.UUID,
    *,
    role: str,
    actor: User,
) -> AdminUserRead:
    """Change one account's role and revoke its sessions.

    The administrator cannot change their own role: a self-demotion would
    lock the last administrator out of the console with no way back in.
    """
    if user_id == actor.id:
        raise AuthForbiddenError("you cannot change your own role")

    target = session.get(User, user_id)
    if target is None:
        raise AuthNotFoundError("user not found")
    if target.role == role:
        raise AuthConflictError(f"role is already {role!r}")

    previous = target.role
    target.role = role
    revoked = revoke_all_sessions(session, target.id)
    auth_event_repo.log_event(
        session,
        user_id=target.id,
        event_type="user_role_changed",
        actor_user_id=actor.id,
        metadata_json=json.dumps(
            {"old_role": previous, "new_role": role, "revoked_sessions": revoked}
        ),
    )
    session.flush()
    logger.info(
        "user role changed",
        extra={
            "user_id": str(target.id),
            "actor_id": str(actor.id),
            "old_role": previous,
            "new_role": role,
        },
    )
    return _read_user(target)
