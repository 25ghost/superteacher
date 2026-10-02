"""Student profile services: user identity + profile lifecycle rules.

Module imports (logging) follow.

Responsibilities (Phase 5C):

- atomic creation of the ``users`` + ``students`` pair (one transaction; a
  failure after the user insert rolls the whole operation back),
- duplicate handling (existing email/phone identity, existing profile for a
  user) translated into the API conflict convention,
- role enforcement: this API creates only ``student`` identities — never
  teacher or admin,
- profile retrieval and profile-field updates with audit logging,
- ``ensure_plausible_dob``: the shared date-of-birth sanity rule (no
  future dates, age within the configured bounds) used by BOTH this
  service and public account registration, so the two create paths can
  never drift.

**Authentication boundary:** nothing here authenticates callers. This layer
is reusable SuperTeacher infrastructure (Registration, Learning, Progress
and the AI Tutor will consume it); the endpoints above it derive the
acting identity from the authenticated principal and pass it in as
``changed_by``.

Errors raised here use the shared ``...Error`` convention and carry the
HTTP status the API layer should emit:
``ProfileValidationError`` → 422, ``ProfileConflictError`` → 409,
``ProfileNotFoundError`` → 404.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.student import Student
from app.models.user import User
from app.repositories import student_repository as student_repo
from app.repositories import student_profile_history_repository as history_repo
from app.repositories import user_repository as user_repo
from app.schemas.pagination import Page
from app.schemas.student_profile import (
    GENDER_VALUES,
    StudentProfileCreate,
    StudentProfileRead,
    StudentProfileSelfCreate,
    StudentProfileUpdate,
    UNSET,
)

logger = logging.getLogger(__name__)

# Database unique constraints that are the final duplicate protection for
# identity creation (mirrored by the service pre-checks above). A concurrent
# INSERT winning the race surfaces as an IntegrityError whose constraint
# name identifies the exact conflict. Both the plain unique constraint and
# the case-insensitive functional index (migration 0004) are matched.
DUPLICATE_EMAIL_CONSTRAINT = "uq_users_email_key"
DUPLICATE_EMAIL_CI_CONSTRAINT = "uq_users_email_ci_key"
DUPLICATE_PHONE_CONSTRAINT = "uq_users_phone_key"
DUPLICATE_PROFILE_CONSTRAINT = "students_user_id_key"


class ProfileError(Exception):
    """Base class: ``.status_code`` tells the API layer which HTTP status to emit."""

    status_code = 400


class ProfileValidationError(ProfileError):
    status_code = 422


class ProfileConflictError(ProfileError):
    status_code = 409


class ProfileNotFoundError(ProfileError):
    status_code = 404


class ProfileForbiddenError(ProfileError):
    """Authenticated but not permitted (e.g. a non-student role on the
    self-service profile-creation path)."""

    status_code = 403


# --- date-of-birth sanity (shared by both creation paths) ----------------------


def _n_years_before(today: date, years: int) -> date:
    """``today`` shifted back ``years`` calendar years (Feb 29 safe)."""
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 February in a non-leap target year
        return today.replace(year=today.year - years, day=28)


def ensure_plausible_dob(dob: date) -> None:
    """Reject impossible or implausible dates of birth (raises 422).

    Rules, in order:
    - not in the future (mirrors the database CHECK for a readable error),
    - old enough to be a student (``STUDENT_MIN_AGE_YEARS``),
    - not older than ``STUDENT_MAX_AGE_YEARS`` (junk-data guard).

    Shared by ``create_student_profile`` and public registration so a
    future DOB can never reach the database CHECK and surface as a 500.
    """
    from app.core.config import get_settings

    settings = get_settings()
    today = date.today()
    if dob > today:
        raise ProfileValidationError("date_of_birth must not be in the future")
    youngest_accepted = _n_years_before(today, settings.STUDENT_MIN_AGE_YEARS)
    if dob > youngest_accepted:
        raise ProfileValidationError(
            f"student must be at least {settings.STUDENT_MIN_AGE_YEARS} years old"
        )
    oldest_accepted = _n_years_before(today, settings.STUDENT_MAX_AGE_YEARS)
    if dob < oldest_accepted:
        raise ProfileValidationError(
            f"date_of_birth must be within the last "
            f"{settings.STUDENT_MAX_AGE_YEARS} years"
        )


# --- reads ---------------------------------------------------------------------


def read_profile(user: User | None, student: Student) -> StudentProfileRead:
    if user is None:
        raise ProfileNotFoundError(
            f"orphaned student profile {student.id}: linked user no longer exists"
        )
    return StudentProfileRead(
        student_id=student.id,
        user_id=user.id,
        email=user.email,
        phone=user.phone,
        role=user.role,
        full_name=student.full_name,
        date_of_birth=student.date_of_birth,
        gender=student.gender,
        country=student.country,
        created_at=student.created_at,
        updated_at=student.updated_at,
    )


def list_students(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
    gender: str | None = None,
    country: str | None = None,
) -> Page[StudentProfileRead]:
    """One page of student profiles for the administrative list.

    ``q`` case-insensitively matches the account email and the profile
    full name (wildcards in the input are treated literally by the
    repository). ``gender`` is confined to the shared vocabulary — an
    unknown value is a 422 domain error, not an empty result. Exactly one
    count query plus one page query run (the page query joins ``users``),
    so the response costs the same number of statements for one row or
    for a full page.

    Read-only: the caller's session is used as-is and nothing is
    committed here.
    """
    if gender is not None and gender not in GENDER_VALUES:
        raise ProfileValidationError(
            f"gender must be one of {', '.join(GENDER_VALUES)}"
        )
    total = student_repo.count_page(session, q=q, gender=gender, country=country)
    rows = student_repo.list_page(
        session, q=q, gender=gender, country=country, limit=limit, offset=offset
    )
    return Page[StudentProfileRead](
        items=[read_profile(user, student) for student, user in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- creation ------------------------------------------------------------------


def create_student_profile(
    session: Session,
    payload: StudentProfileCreate,
    changed_by: uuid.UUID | None = None,
) -> StudentProfileRead:
    """Create the user identity + student profile pair atomically.

    The caller's transaction (the request-scoped session) is used as-is: the
    service performs ``flush``es, and the API/dependency layer commits. If
    any step raises, the session is rolled back by the caller, so no
    orphaned ``users`` row can survive a failed profile creation.

    ``changed_by`` records the acting administrator in the audit trail
    (``change_type="create"``); self-service registration passes the new
    account's own user id.
    """
    ensure_plausible_dob(payload.date_of_birth)

    # Duplicate identity handling (users.email / users.phone are UNIQUE).
    email = str(payload.email).strip().lower() if payload.email is not None else None
    phone = str(payload.phone).strip() if payload.phone is not None else None
    if email is not None and user_repo.get_by_email(session, email) is not None:
        raise ProfileConflictError(f"a user with email {email!r} already exists")
    if phone is not None and user_repo.get_by_phone(session, phone) is not None:
        raise ProfileConflictError(f"a user with phone {phone!r} already exists")

    try:
        user = user_repo.create_student_user(
            session, email=email, phone=phone
        )
        student = student_repo.create(
            session,
            user_id=user.id,
            full_name=payload.full_name,
            date_of_birth=payload.date_of_birth,
            gender=payload.gender,
            country=payload.country,
        )
    except IntegrityError as exc:
        # Race handling: between the pre-checks above and this INSERT a
        # concurrent request may have committed the same identity. The
        # users/students UNIQUE constraints are the final protection; the
        # constraint name tells us exactly which conflict occurred so the
        # 409 message is accurate instead of a generic 500.
        constraint_text = str(exc.orig)
        if DUPLICATE_EMAIL_CONSTRAINT in constraint_text or (
            DUPLICATE_EMAIL_CI_CONSTRAINT in constraint_text
        ):
            logger.warning("duplicate email race lost for a new student profile")
            raise ProfileConflictError(
                f"a user with email {email!r} already exists"
            ) from exc
        if DUPLICATE_PHONE_CONSTRAINT in constraint_text:
            logger.warning("duplicate phone race lost for a new student profile")
            raise ProfileConflictError(
                f"a user with phone {phone!r} already exists"
            ) from exc
        if DUPLICATE_PROFILE_CONSTRAINT in constraint_text:
            logger.warning("duplicate profile race lost for one user identity")
            raise ProfileConflictError(
                "student profile already exists for this user"
            ) from exc
        raise  # an unexpected integrity problem must stay visible (500)

    # Audit: attribute the creation (who minted this profile pair).
    history_repo.log_change(
        session,
        student_id=student.id,
        field_name="profile",
        old_value=None,
        new_value=None,
        changed_by=changed_by,
        change_type="create",
    )

    return read_profile(user, student)


def create_profile_for_existing_user(
    session: Session,
    user: User,
    payload: StudentProfileSelfCreate,
) -> StudentProfileRead:
    """Attach the profile half of the pair to an *existing* user (self-service).

    ``POST /me/student`` — the authenticated student supplies the profile
    fields; identity anchors (email/phone) stay exactly as the account
    already has them and are never accepted in the payload.

    Guards, in order:
    - role must be ``student`` (403) — a teacher/parent/admin can never
      attach a student profile to their account,
    - the profile must not already exist (409), including the concurrent
      race lost against ``students_user_id_key`` (409),
    - the date of birth must be plausible (422) — same shared rule as the
      registration and administrative create paths.

    The creation is recorded in the audit trail attributed to the account
    itself (``change_type="create"``, ``field_name="profile"``). The caller
    commits; on any raise the caller rolls back.
    """
    if user.role != "student":
        raise ProfileForbiddenError(
            "only student accounts may create a student profile"
        )
    if student_repo.get_by_user_id(session, user.id) is not None:
        raise ProfileConflictError("student profile already exists for this user")

    ensure_plausible_dob(payload.date_of_birth)

    try:
        student = student_repo.create(
            session,
            user_id=user.id,
            full_name=payload.full_name,
            date_of_birth=payload.date_of_birth,
            gender=payload.gender,
            country=payload.country,
        )
    except IntegrityError as exc:
        # Race: two concurrent POSTs for the same account — the UNIQUE
        # students.user_id constraint is the final protection. Match both
        # the PostgreSQL constraint name and SQLite's unit-test phrasing
        # ("UNIQUE constraint failed: students.user_id").
        constraint_text = str(exc.orig)
        if (
            DUPLICATE_PROFILE_CONSTRAINT in constraint_text
            or "students.user_id" in constraint_text
        ):
            logger.warning("duplicate profile race lost on self-service create")
            raise ProfileConflictError(
                "student profile already exists for this user"
            ) from exc
        raise

    history_repo.log_change(
        session,
        student_id=student.id,
        field_name="profile",
        old_value=None,
        new_value=None,
        changed_by=user.id,
        change_type="create",
    )
    return read_profile(user, student)


# --- reads / ownership helpers -------------------------------------------------


def load_student_for_user(user: User) -> Student | None:
    """The student profile linked to one user object (or None).

    Uses the ORM relationship already loaded on the identity; used by the
    API layer for object-level ownership checks (Phase 5G).
    """
    return user.student


def load_student_row(session: Session, student_id: uuid.UUID) -> Student:
    """The raw ``students`` row for ownership checks (404 when unknown)."""
    student = student_repo.get_by_id(session, student_id)
    if student is None:
        raise ProfileNotFoundError(f"no student profile with id {student_id}")
    return student


def get_student_profile(session: Session, student_id: uuid.UUID) -> StudentProfileRead:
    student = student_repo.get_by_id(session, student_id)
    if student is None:
        raise ProfileNotFoundError(f"no student profile with id {student_id}")
    user = session.get(User, student.user_id)
    return read_profile(user, student)


# --- updates -------------------------------------------------------------------


def profile_update_kwargs(payload: StudentProfileUpdate) -> dict:
    """Map a PATCH body onto ``update_student_profile`` kwargs: supplied fields only.

    ``model_fields_set`` distinguishes omission (leave untouched) from an
    explicit ``null`` (clear a nullable column). Shared by both PATCH
    endpoints so their semantics can never drift.
    """
    fields = payload.model_fields_set
    kwargs: dict = {}
    if "full_name" in fields:
        kwargs["full_name"] = payload.full_name
    if "gender" in fields:
        kwargs["gender"] = payload.gender
    if "country" in fields:
        kwargs["country"] = payload.country
    return kwargs


def update_student_profile(
    session: Session,
    student_id: uuid.UUID,
    full_name=UNSET,
    gender=UNSET,
    country=UNSET,
    changed_by: uuid.UUID | None = None,
) -> StudentProfileRead:
    """Update mutable profile fields (email/phone/DOB are identity anchors —
    deliberately immutable here).

    Parameters left at ``UNSET`` are untouched; an explicit ``None`` on the
    nullable ``gender``/``country`` clears them. Every actual change —
    including a clear — is logged to the audit trail with the acting
    ``changed_by`` user.
    """
    student = student_repo.get_by_id(session, student_id)
    if student is None:
        raise ProfileNotFoundError(f"no student profile with id {student_id}")

    # Compute changes before applying them (audit first, then write).
    changes: list[tuple[str, str | None, str | None]] = []
    if (
        full_name is not UNSET
        and full_name is not None
        and student.full_name != full_name
    ):
        changes.append(("full_name", student.full_name, full_name))
    if gender is not UNSET and student.gender != gender:
        changes.append(("gender", student.gender, gender))
    if country is not UNSET and student.country != country:
        changes.append(("country", student.country, country))

    for field_name, old_val, new_val in changes:
        history_repo.log_change(
            session,
            student_id=student_id,
            field_name=field_name,
            old_value=str(old_val) if old_val is not None else None,
            new_value=str(new_val) if new_val is not None else None,
            changed_by=changed_by,
            change_type="update",
        )

    try:
        student = student_repo.update_profile(
            session, student, full_name=full_name, gender=gender, country=country
        )
    except IntegrityError as exc:
        # A schema↔DB vocabulary drift (e.g. a future gender value that the
        # API schema accepts but students_gender_check rejects) must surface
        # as a readable 422, never a raw 500. The caller rolls back, which
        # also discards the flushed audit rows above.
        logger.warning("profile update hit a database CHECK constraint")
        raise ProfileValidationError(
            "update violates a database validation constraint"
        ) from exc
    user = session.get(User, student.user_id)
    return read_profile(user, student)
