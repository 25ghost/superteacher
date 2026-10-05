"""Administrative account lifecycle (Phase B, slice 4).

Service layer for the ``/admin/teachers``, ``/admin/users`` and
``/admin/users/{id}/role`` routes: create a teacher account (pending,
invited), list teachers, list accounts, re-send or cancel invitations,
activate/deactivate accounts, and change roles.

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
from app.repositories import auth_session_repository as auth_session_repo
from app.repositories import user_repository as user_repo
from app.schemas.pagination import Page
from app.schemas.teacher_admin import (
    AdminUserDetailRead,
    AdminUserEventRead,
    AdminUserListRead,
    AdminUserRead,
    TeacherCreate,
    TeacherRead,
)
from app.services.auth_service import (
    AuthConflictError,
    AuthForbiddenError,
    AuthNotFoundError,
    AuthValidationError,
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


def _list_item(user: User) -> AdminUserListRead:
    """The list projection of one account — lockout state included, no secrets."""
    return AdminUserListRead(
        id=user.id,
        email=user.email,
        role=user.role,
        status=user.status,
        locked_until=user.locked_until,
        failed_login_count=user.failed_login_count,
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


# --- administrative account list ------------------------------------------------------


def list_users(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    role: str | None = None,
    status: str | None = None,
    q: str | None = None,
) -> Page[AdminUserListRead]:
    """One page of every account for ``GET /admin/users``.

    ``role`` and ``status`` are confined to the shared vocabularies — an
    unknown value is a 422 domain error, never a silently empty page.
    ``q`` matches the email case-insensitively with wildcards treated
    literally (the repository binds the pattern, it never builds SQL).

    Exactly one count query plus one page query run, both independent of
    how many rows come back, so the response costs the same statements
    for one account or a full page. Read-only: nothing is committed here.
    """
    if role is not None and role not in {member.value for member in UserRole}:
        raise AuthValidationError(
            f"role must be one of {', '.join(member.value for member in UserRole)}"
        )
    if status is not None and status not in {member.value for member in UserStatus}:
        raise AuthValidationError(
            "status must be one of "
            + ", ".join(member.value for member in UserStatus)
        )
    total = user_repo.count_page(session, role=role, status=status, q=q)
    rows = user_repo.list_page(
        session, role=role, status=status, q=q, limit=limit, offset=offset
    )
    return Page[AdminUserListRead](
        items=[_list_item(user) for user in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_user_detail(session: Session, user_id: uuid.UUID) -> AdminUserDetailRead:
    """One account with its profiles, live sessions and newest audit rows.

    Exactly three statements run for every account — the identity plus
    both optional profiles and the assigned school come from a single
    joined query, the session count is one aggregate, and the audit trail
    is one ``LIMIT 10`` query with the actor already resolved. Unknown id
    → 404. Read-only: nothing is committed here, and nothing sensitive
    leaks: no password hash, no token digest, no event metadata blob.
    """
    row = user_repo.get_detail_row(session, user_id)
    if row is None:
        raise AuthNotFoundError("user not found")
    user, student, teacher, school = row
    active_sessions = auth_session_repo.count_active_for_user(
        session, user.id, now=_now()
    )
    events = auth_event_repo.list_recent_for_user(session, user.id, limit=10)
    return AdminUserDetailRead(
        id=user.id,
        email=user.email,
        role=user.role,
        status=user.status,
        locked_until=user.locked_until,
        failed_login_count=user.failed_login_count,
        created_at=user.created_at,
        student_profile_id=student.id if student is not None else None,
        teacher_profile_id=teacher.id if teacher is not None else None,
        school_id=teacher.school_id if teacher is not None else None,
        school_code=school.school_code if school is not None else None,
        school_name=school.name if school is not None else None,
        active_session_count=active_sessions,
        recent_events=[
            AdminUserEventRead(
                event_type=event.event_type,
                created_at=event.created_at,
                actor_email=actor.email if actor is not None else None,
                ip_address=event.ip_address,
            )
            for event, actor in events
        ],
    )


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


def _activate_target(
    session: Session,
    user: User,
    *,
    actor: User,
    event_type: str,
    allow_pending_teacher: bool,
) -> None:
    """Shared ``pending/suspended → active`` core (no transport concerns).

    409 when the account is already active. A pending *teacher* account
    is refused unless the caller is the teacher-specific route: teachers
    are activated by accepting their invitation, so activating them
    generically would bypass the email confirmation the invitation flow
    exists for. The caller names the audit event, so the trail keeps
    distinguishing ``teacher_activated`` from ``user_activated``.
    """
    if user.status == UserStatus.ACTIVE.value:
        raise AuthConflictError("account is already active")
    if (
        not allow_pending_teacher
        and user.role == UserRole.TEACHER.value
        and user.status == UserStatus.PENDING.value
    ):
        raise AuthConflictError(
            "a pending teacher account must be activated through the "
            "invitation flow: re-send it with POST /admin/teachers/"
            f"{user.id}/invite or accept it with POST /auth/accept-invite"
        )
    user.status = UserStatus.ACTIVE.value
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type=event_type,
        actor_user_id=actor.id,
    )
    session.flush()


def _deactivate_target(
    session: Session,
    user: User,
    *,
    actor: User,
    event_type: str,
) -> None:
    """Shared ``→ suspended`` core: every refresh session is revoked.

    409 when the account is already suspended. Two guards run before any
    state is written and are unreachable through the *teacher* routes
    (those only accept non-admin targets): an administrator cannot
    deactivate their own account, and the last active administrator
    cannot be deactivated. The second guard takes the same row lock that
    two concurrent deactivations contend on, so two administrators
    trying to deactivate each other cannot both pass it — exactly one
    commit survives.
    """
    if user.id == actor.id:
        raise AuthConflictError(
            "an administrator cannot deactivate their own account"
        )
    if user.status == UserStatus.SUSPENDED.value:
        raise AuthConflictError("account is already suspended")
    if user.role == UserRole.ADMIN.value and user.status == UserStatus.ACTIVE.value:
        admins = user_repo.lock_active_admins(session)
        if len(admins) == 1 and user.id in {admin.id for admin in admins}:
            raise AuthConflictError(
                "the last active administrator cannot be deactivated"
            )
    user.status = UserStatus.SUSPENDED.value
    revoked = revoke_all_sessions(session, user.id)
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type=event_type,
        actor_user_id=actor.id,
        metadata_json=json.dumps({"revoked_sessions": revoked}),
    )
    session.flush()


def activate_teacher(session: Session, user_id: uuid.UUID, *, actor: User) -> TeacherRead:
    """pending/suspended → active (the account may now authenticate)."""
    user = _load_teacher_user(session, user_id)
    _activate_target(
        session,
        user,
        actor=actor,
        event_type="teacher_activated",
        allow_pending_teacher=True,
    )
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
    _deactivate_target(
        session, user, actor=actor, event_type="teacher_deactivated"
    )
    logger.info(
        "teacher account deactivated",
        extra={"user_id": str(user.id), "actor_id": str(actor.id)},
    )
    return _read_teacher(session, user)


def activate_user(session: Session, user_id: uuid.UUID, *, actor: User) -> AdminUserListRead:
    """Any non-teacher account → active (``user_activated``).

    404 unknown id; 409 already active or a pending teacher (see
    ``_activate_target``). The teacher's own activate route is the one
    that lifts its invitation guard.
    """
    target = session.get(User, user_id)
    if target is None:
        raise AuthNotFoundError("user not found")
    _activate_target(
        session,
        target,
        actor=actor,
        event_type="user_activated",
        allow_pending_teacher=False,
    )
    logger.info(
        "user account activated",
        extra={"user_id": str(target.id), "actor_id": str(actor.id)},
    )
    return _list_item(target)


def deactivate_user(session: Session, user_id: uuid.UUID, *, actor: User) -> AdminUserListRead:
    """Any status → suspended, sessions revoked, ``user_deactivated``.

    404 unknown id; 409 already suspended, self-deactivation, or the last
    active administrator (see ``_deactivate_target``).
    """
    target = session.get(User, user_id)
    if target is None:
        raise AuthNotFoundError("user not found")
    _deactivate_target(session, target, actor=actor, event_type="user_deactivated")
    logger.info(
        "user account deactivated",
        extra={"user_id": str(target.id), "actor_id": str(actor.id)},
    )
    return _list_item(target)


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
