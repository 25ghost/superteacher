"""Unit tests: student profile schemas and service (no PostgreSQL).

Service tests run on in-memory SQLite through the real ORM models, proving:
- schema validation rules (name, DOB, gender, email),
- role enforcement (only 'student' identities are created),
- duplicate handling (email/phone identity, per-user profile),
- atomic creation (user rolled back when the profile insert fails),
- retrieval and update paths, including 404 mapping.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.enums import UserRole
from app.models.student import Student
from app.models.user import User
from app.models.student_profile_history import StudentProfileHistory
from app.repositories import student_profile_history_repository as history_repo
from app.schemas.student_profile import GENDER_VALUES, StudentProfileCreate
from app.services import student_service as svc
from app.services.student_service import (
    ProfileConflictError,
    ProfileForbiddenError,
    ProfileNotFoundError,
    ProfileValidationError,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _payload(**overrides) -> StudentProfileCreate:
    base = dict(
        email="kid@example.com",
        phone=None,
        full_name="Aline Uwase",
        date_of_birth=date(2012, 4, 10),
        gender="female",
        country="Rwanda",
    )
    base.update(overrides)
    return StudentProfileCreate(**base)


# --- schema validation ---------------------------------------------------------


def test_schema_accepts_valid_payload() -> None:
    payload = _payload()
    assert payload.full_name == "Aline Uwase"
    assert payload.gender == "female"


def test_schema_rejects_blank_full_name() -> None:
    with pytest.raises(ValueError):
        _payload(full_name="   ")


def test_schema_allows_yesterday_but_service_rejects_tomorrow(session: Session) -> None:
    """DOB future-guard is a service rule (DB CHECK uses CURRENT_DATE)."""
    payload = _payload(date_of_birth=date.today() - timedelta(days=1))
    assert payload.date_of_birth < date.today()
    with pytest.raises(ProfileValidationError):
        svc.create_student_profile(
            session, _payload(date_of_birth=date.today() + timedelta(days=1))
        )


def test_schema_rejects_unknown_gender() -> None:
    with pytest.raises(ValueError):
        _payload(gender="robot")


def test_schema_requires_email() -> None:
    """An email is REQUIRED for administrative profile creation.

    Phone-only identities can never authenticate (login and password
    reset are email-based), so ``email=None`` — with or without a phone —
    is refused at the schema boundary.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _payload(email=None)
    with pytest.raises(ValidationError):
        _payload(email=None, phone="+250700000001")


def test_gender_vocabulary_is_stable() -> None:
    assert GENDER_VALUES == ("female", "male", "other", "undisclosed")


def test_schema_rejects_invalid_email() -> None:
    with pytest.raises(ValueError):
        _payload(email="not-an-email")


def test_schema_phone_blank_becomes_none() -> None:
    assert _payload(phone="   ").phone is None


# --- service behavior ------------------------------------------------------------


def test_create_profile_pairs_user_and_student(session: Session) -> None:
    profile = svc.create_student_profile(session, _payload())
    session.commit()
    assert profile.role == "student"
    assert profile.user_id and profile.student_id
    user = session.get(User, profile.user_id)
    student = session.get(Student, profile.student_id)
    assert user.role == "student"
    assert student.user_id == user.id
    assert student.full_name == "Aline Uwase"


def test_create_profile_never_creates_privileged_roles(session: Session) -> None:
    profile = svc.create_student_profile(session, _payload())
    session.commit()
    role = session.get(User, profile.user_id).role
    assert role == "student"
    assert role not in {"teacher", "admin"}


def test_duplicate_email_conflicts(session: Session) -> None:
    svc.create_student_profile(session, _payload())
    session.commit()
    with pytest.raises(ProfileConflictError):
        svc.create_student_profile(session, _payload(email="kid@example.com"))
    session.rollback()


def test_duplicate_phone_conflicts(session: Session) -> None:
    svc.create_student_profile(session, _payload(phone="+250700000001"))
    session.commit()
    with pytest.raises(ProfileConflictError):
        svc.create_student_profile(session, _payload(phone="+250700000001"))
    session.rollback()


def test_future_dob_raises_validation_error(session: Session) -> None:
    with pytest.raises(ProfileValidationError):
        svc.create_student_profile(
            session, _payload(date_of_birth=date.today() + timedelta(days=30))
        )


def test_failed_profile_creation_rolls_back_user(session: Session) -> None:
    """Atomicity: a profile-insert failure must not orphan the user row.

    Forces a genuine IntegrityError (duplicate students.user_id — enforced
    by SQLite and PostgreSQL alike) raised *after* the user row was inserted
    in the same transaction, then proves the rollback removes both.
    """
    from sqlalchemy.exc import IntegrityError

    from app.repositories import student_repository as student_repo
    from app.repositories import user_repository as user_repo

    # Seed an existing profile pair for user X.
    existing = svc.create_student_profile(session, _payload())
    session.commit()

    # Manually create a second user, then violate the unique profile rule.
    user = user_repo.create_student_user(session, email="other@example.com", phone=None)
    session.flush()
    with pytest.raises(IntegrityError):
        student_repo.create(
            session,
            user_id=existing.user_id,  # duplicate user_id -> IntegrityError
            full_name="Someone Else",
            date_of_birth=date(2010, 1, 1),
            gender=None,
            country=None,
        )
    session.rollback()

    # The second user (inserted in the failed transaction) is gone too.
    assert session.query(User).count() == 1
    assert session.query(Student).count() == 1


def test_get_profile_roundtrip(session: Session) -> None:
    created = svc.create_student_profile(session, _payload())
    session.commit()
    fetched = svc.get_student_profile(session, created.student_id)
    assert fetched.full_name == "Aline Uwase"
    assert fetched.email == "kid@example.com"


def test_get_unknown_profile_is_404(session: Session) -> None:
    import uuid

    with pytest.raises(ProfileNotFoundError):
        svc.get_student_profile(session, uuid.uuid4())


def test_update_profile_mutable_fields_only(session: Session) -> None:
    created = svc.create_student_profile(session, _payload())
    session.commit()
    updated = svc.update_student_profile(
        session, created.student_id, full_name="Aline U. Renamed", country="Uganda"
    )
    assert updated.full_name == "Aline U. Renamed"
    assert updated.country == "Uganda"
    assert updated.date_of_birth == date(2012, 4, 10)  # immutable here
    assert updated.email == "kid@example.com"          # immutable here


# --- edge-case validation tests -----------------------------------------------


def test_schema_rejects_max_length_name() -> None:
    """full_name must be <= 200 characters."""
    with pytest.raises(ValueError):
        _payload(full_name="A" * 201)


def test_schema_accepts_max_length_name() -> None:
    """Exactly 200 characters should be accepted."""
    payload = _payload(full_name="A" * 200)
    assert len(payload.full_name) == 200


def test_schema_rejects_country_with_numbers() -> None:
    with pytest.raises(ValueError, match="country"):
        _payload(country="12345")


def test_schema_rejects_country_with_html() -> None:
    with pytest.raises(ValueError, match="country"):
        _payload(country="<script>alert(1)</script>")


def test_schema_rejects_country_with_special_chars() -> None:
    with pytest.raises(ValueError, match="country"):
        _payload(country="Rwanda@#$%")


def test_schema_accepts_country_with_hyphen() -> None:
    payload = _payload(country="Cote-d'Ivoire")
    assert payload.country == "Cote-d'Ivoire"


def test_schema_accepts_country_with_apostrophe() -> None:
    payload = _payload(country="Cote d'Ivoire")
    assert payload.country == "Cote d'Ivoire"


def test_schema_rejects_phone_with_letters() -> None:
    with pytest.raises(ValueError, match="phone"):
        _payload(phone="not-a-phone")


def test_schema_rejects_phone_with_special_chars() -> None:
    with pytest.raises(ValueError, match="phone"):
        _payload(phone="+123-456-7890")


def test_schema_rejects_phone_starting_with_zero() -> None:
    with pytest.raises(ValueError, match="phone"):
        _payload(phone="+0123456789")


def test_schema_accepts_valid_e164_phone() -> None:
    payload = _payload(phone="+250700000001")
    assert payload.phone == "+250700000001"


def test_schema_country_blank_becomes_none() -> None:
    assert _payload(country="   ").country is None


def test_schema_rejects_markup_in_full_name() -> None:
    """The hardened name charset refuses markup (and control chars, digits)."""
    with pytest.raises(ValueError, match="full_name"):
        _payload(full_name="John <script> Doe")
    with pytest.raises(ValueError, match="full_name"):
        _payload(full_name="Mary\nJane")  # control character
    with pytest.raises(ValueError, match="full_name"):
        _payload(full_name="Student 42")  # digits are not name characters


def test_schema_accepts_names_with_accents_and_punctuation() -> None:
    """Unicode letters plus spaces/hyphens/apostrophes/periods remain valid."""
    assert _payload(full_name="José Ñandú").full_name == "José Ñandú"
    assert _payload(full_name="Jean-Baptiste N. Habineza").full_name == (
        "Jean-Baptiste N. Habineza"
    )
    assert _payload(full_name="O'Connor").full_name == "O'Connor"


# --- audit trail --------------------------------------------------------------


def test_update_creates_audit_trail(session: Session) -> None:
    profile = svc.create_student_profile(session, _payload())
    session.commit()
    admin = User(
        email="admin@example.com",
        role="admin",
        status="active",
        password_hash="x",
    )
    session.add(admin)
    session.flush()

    svc.update_student_profile(
        session,
        profile.student_id,
        full_name="New Name",
        changed_by=admin.id,
    )
    session.commit()

    records = history_repo.list_for_student(session, profile.student_id)
    # create + update rows now both exist; the update is the newest.
    assert len(records) == 2
    update_rows = [r for r in records if r.change_type == "update"]
    assert len(update_rows) == 1
    r = update_rows[0]
    assert r.field_name == "full_name"
    assert r.old_value == "Aline Uwase"
    assert r.new_value == "New Name"
    assert r.changed_by == admin.id
    assert r.change_type == "update"


def test_create_writes_attribution_audit_row(session: Session) -> None:
    """Profile creation itself is audited (change_type='create')."""
    admin = User(
        email="creator@example.com",
        role="admin",
        status="active",
        password_hash="x",
    )
    session.add(admin)
    session.flush()

    profile = svc.create_student_profile(
        session, _payload(), changed_by=admin.id
    )
    session.commit()

    records = history_repo.list_for_student(session, profile.student_id)
    creates = [r for r in records if r.change_type == "create"]
    assert len(creates) == 1
    assert creates[0].field_name == "profile"
    assert creates[0].old_value is None
    assert creates[0].new_value is None
    assert creates[0].changed_by == admin.id


def test_update_no_change_no_audit(session: Session) -> None:
    profile = svc.create_student_profile(session, _payload())
    session.commit()

    svc.update_student_profile(
        session,
        profile.student_id,
        full_name=None,  # UNSET-equivalent no-op for the NOT NULL column
    )
    session.commit()

    records = history_repo.list_for_student(session, profile.student_id)
    # Only the creation event — the no-op update wrote nothing.
    assert len(records) == 1
    assert records[0].change_type == "create"


def test_history_list_for_student_returns_all_changes(session: Session) -> None:
    profile = svc.create_student_profile(session, _payload())
    session.commit()

    svc.update_student_profile(
        session, profile.student_id, full_name="First Change"
    )
    svc.update_student_profile(
        session, profile.student_id, full_name="Second Change"
    )
    session.commit()

    records = history_repo.list_for_student(session, profile.student_id)
    # 1 create + 2 updates
    assert len(records) == 3
    update_rows = [r for r in records if r.change_type == "update"]
    new_values = {r.new_value for r in update_rows}
    assert new_values == {"First Change", "Second Change"}


def test_history_get_by_id(session: Session) -> None:
    profile = svc.create_student_profile(session, _payload())
    session.commit()

    svc.update_student_profile(
        session, profile.student_id, full_name="Changed"
    )
    session.commit()

    records = history_repo.list_for_student(session, profile.student_id)
    update_rows = [r for r in records if r.change_type == "update"]
    found = history_repo.get_by_id(session, update_rows[0].id)
    assert found is not None
    assert found.new_value == "Changed"

    missing = history_repo.get_by_id(session, uuid.uuid4())
    assert missing is None


def test_history_respects_limit_and_offset(session: Session) -> None:
    profile = svc.create_student_profile(session, _payload())
    session.commit()
    for name in ("One", "Two", "Three"):
        svc.update_student_profile(session, profile.student_id, full_name=name)
    session.commit()

    page = history_repo.list_for_student(session, profile.student_id, limit=2, offset=0)
    assert len(page) == 2
    rest = history_repo.list_for_student(session, profile.student_id, limit=10, offset=2)
    assert len(rest) == 2  # 4 total (1 create + 3 updates) minus the first page
    total = history_repo.list_for_student(session, profile.student_id, limit=200)
    assert len(total) == 4


def test_history_rejects_unknown_change_type(session: Session) -> None:
    """Vocabulary enforcement: only create/update are valid change types."""
    profile = svc.create_student_profile(session, _payload())
    session.commit()
    with pytest.raises(ValueError, match="change_type"):
        history_repo.log_change(
            session,
            student_id=profile.student_id,
            field_name="full_name",
            old_value=None,
            new_value="X",
            changed_by=None,
            change_type="delete",
        )
    session.rollback()


def test_history_rejects_unknown_field_name(session: Session) -> None:
    profile = svc.create_student_profile(session, _payload())
    session.commit()
    with pytest.raises(ValueError, match="field_name"):
        history_repo.log_change(
            session,
            student_id=profile.student_id,
            field_name="secret_field",
            old_value=None,
            new_value="X",
            changed_by=None,
            change_type="update",
        )
    session.rollback()


# --- hardening: date-of-birth age bounds --------------------------------------


def test_too_young_dob_rejected(session: Session) -> None:
    """A toddler is below STUDENT_MIN_AGE_YEARS (default 2)."""
    from app.core.config import get_settings

    min_years = get_settings().STUDENT_MIN_AGE_YEARS
    too_young = date.today().replace(year=date.today().year - (min_years - 1))
    with pytest.raises(ProfileValidationError, match="at least"):
        svc.create_student_profile(session, _payload(date_of_birth=too_young))


def test_too_old_dob_rejected(session: Session) -> None:
    """A pre-1900-era DOB is junk data — refused above the max age."""
    with pytest.raises(ProfileValidationError, match="years"):
        svc.create_student_profile(session, _payload(date_of_birth=date(1890, 1, 1)))


def test_plausible_dob_helper_accepts_normal_ages() -> None:
    svc.ensure_plausible_dob(date(2012, 4, 10))  # ~14 years old
    svc.ensure_plausible_dob(date(1990, 6, 15))  # ~36 years old
    # A centenarian inside the max-age bound is accepted.
    svc.ensure_plausible_dob(date(1940, 1, 1))


def test_plausible_dob_helper_rejects_future() -> None:
    with pytest.raises(ProfileValidationError, match="future"):
        svc.ensure_plausible_dob(date.today() + timedelta(days=1))


def test_plausible_dob_helper_rejects_infant() -> None:
    with pytest.raises(ProfileValidationError, match="at least"):
        svc.ensure_plausible_dob(date.today())


# --- hardening: phone canonicalisation ----------------------------------------


def test_phone_without_plus_is_canonicalised(session: Session) -> None:
    """``250700000002`` is stored as ``+250700000002``."""
    payload = _payload(phone="250700000002")
    assert payload.phone == "+250700000002"
    profile = svc.create_student_profile(session, payload)
    session.commit()
    assert profile.phone == "+250700000002"


def test_phone_format_variants_do_not_duplicate(session: Session) -> None:
    """The same number in two formats is ONE identity — second create 409s."""
    svc.create_student_profile(session, _payload(phone="+250700000003"))
    session.commit()
    with pytest.raises(ProfileConflictError):
        svc.create_student_profile(session, _payload(phone="250700000003"))
    session.rollback()


# --- hardening: email length cap ----------------------------------------------


def test_schema_rejects_over_long_email() -> None:
    from pydantic import ValidationError

    long_email = "a" * 300 + "@example.com"
    with pytest.raises(ValidationError):
        _payload(email=long_email)


# --- hardening: clearing nullable fields --------------------------------------


def test_explicit_none_clears_gender_and_is_audited(session: Session) -> None:
    """An explicit None on a nullable column clears it, with an audit row."""
    profile = svc.create_student_profile(session, _payload(gender="female"))
    session.commit()
    assert profile.gender == "female"

    updated = svc.update_student_profile(
        session, profile.student_id, gender=None, changed_by=profile.user_id
    )
    session.commit()
    assert updated.gender is None

    records = history_repo.list_for_student(session, profile.student_id)
    update_rows = [r for r in records if r.change_type == "update"]
    assert len(update_rows) == 1
    assert update_rows[0].field_name == "gender"
    assert update_rows[0].old_value == "female"
    assert update_rows[0].new_value is None  # the clear is recorded


def test_omitted_fields_are_untouched(session: Session) -> None:
    """Fields left at UNSET (not passed) never change — only supplied ones do."""
    profile = svc.create_student_profile(
        session, _payload(gender="female", country="Rwanda")
    )
    session.commit()

    updated = svc.update_student_profile(session, profile.student_id, full_name="Renamed")
    session.commit()
    assert updated.full_name == "Renamed"
    assert updated.gender == "female"
    assert updated.country == "Rwanda"


def test_full_name_explicit_null_rejected() -> None:
    """An explicit null for full_name (NOT NULL column) is a 422."""
    from pydantic import ValidationError

    from app.schemas.student_profile import StudentProfileUpdate

    with pytest.raises(ValidationError, match="full_name cannot be cleared"):
        StudentProfileUpdate(full_name=None)


def test_gender_explicit_null_is_supplied_not_omitted() -> None:
    from app.schemas.student_profile import StudentProfileUpdate

    payload = StudentProfileUpdate(gender=None)
    assert "gender" in payload.model_fields_set  # supplied → will clear
    omitted = StudentProfileUpdate()
    assert "gender" not in omitted.model_fields_set  # omitted → untouched


# --- self-service creation (POST /me/student) ---------------------------------


def _self_payload(**overrides):
    from app.schemas.student_profile import StudentProfileSelfCreate

    base = dict(
        full_name="Self Service Kid",
        date_of_birth=date(2012, 4, 10),
        gender="female",
        country="Rwanda",
    )
    base.update(overrides)
    return StudentProfileSelfCreate(**base)


def _profileless_student(session: Session) -> User:
    """A real student-role user with NO students row (the precondition)."""
    from app.repositories import user_repository as user_repo

    user = user_repo.create_student_user(
        session, email="profileless@example.com", phone=None
    )
    session.flush()
    return user


def test_self_create_attaches_profile_and_audits(session: Session) -> None:
    user = _profileless_student(session)
    session.commit()

    profile = svc.create_profile_for_existing_user(session, user, _self_payload())
    session.commit()

    assert profile.user_id == user.id
    assert profile.full_name == "Self Service Kid"
    assert profile.email == "profileless@example.com"  # identity untouched
    # Exactly one audit row: create/profile attributed to the account itself.
    records = history_repo.list_for_student(session, profile.student_id)
    assert len(records) == 1
    assert records[0].change_type == "create"
    assert records[0].field_name == "profile"
    assert records[0].changed_by == user.id


def test_self_create_conflicts_when_profile_exists(session: Session) -> None:
    created = svc.create_student_profile(session, _payload())
    session.commit()
    user = session.get(User, created.user_id)

    with pytest.raises(ProfileConflictError, match="already exists"):
        svc.create_profile_for_existing_user(session, user, _self_payload())
    session.rollback()


def test_self_create_race_maps_integrity_error_to_409(session: Session) -> None:
    """Concurrent POST lost against students_user_id_key → 409, not 500."""
    from unittest.mock import patch

    user = _profileless_student(session)
    session.flush()
    svc.create_profile_for_existing_user(session, user, _self_payload())
    session.commit()

    # Simulate the race: the pre-check is blinded, but the INSERT loses
    # against the UNIQUE students.user_id constraint.
    with patch.object(svc.student_repo, "get_by_user_id", return_value=None):
        with pytest.raises(ProfileConflictError, match="already exists"):
            svc.create_profile_for_existing_user(session, user, _self_payload())
    session.rollback()


def test_self_create_forbidden_for_non_student_role(session: Session) -> None:
    teacher = User(
        email="teach@example.com", role=UserRole.TEACHER.value, password_hash="x"
    )
    session.add(teacher)
    session.flush()
    with pytest.raises(ProfileForbiddenError) as excinfo:
        svc.create_profile_for_existing_user(session, teacher, _self_payload())
    assert excinfo.value.status_code == 403


def test_self_create_future_dob_is_422(session: Session) -> None:
    user = _profileless_student(session)
    session.flush()
    with pytest.raises(ProfileValidationError, match="future"):
        svc.create_profile_for_existing_user(
            session, user, _self_payload(date_of_birth=date.today() + timedelta(days=3))
        )
    session.rollback()


def test_self_create_implausible_dob_is_422(session: Session) -> None:
    user = _profileless_student(session)
    session.flush()
    with pytest.raises(ProfileValidationError):
        svc.create_profile_for_existing_user(
            session, user, _self_payload(date_of_birth=date(1850, 1, 1))
        )
    session.rollback()


def test_self_create_schema_rejects_identity_anchors_and_unknown_keys() -> None:
    """email/phone/DOB-adjacent anchors and typos are 422, never ignored."""
    from pydantic import ValidationError

    from app.schemas.student_profile import StudentProfileSelfCreate

    with pytest.raises(ValidationError):
        StudentProfileSelfCreate(email="x@example.com", **_self_payload().model_dump())
    with pytest.raises(ValidationError):
        StudentProfileSelfCreate(phone="+250700000009", **_self_payload().model_dump())
    with pytest.raises(ValidationError):
        StudentProfileSelfCreate(fullNam="Typo", **_self_payload().model_dump())


# --- hardening: PATCH rejects anchors / unknown keys (extra="forbid") ----------


def test_patch_schema_rejects_identity_anchor_keys() -> None:
    from pydantic import ValidationError

    from app.schemas.student_profile import StudentProfileUpdate

    for anchor in ("email", "phone", "date_of_birth", "fullNam"):
        with pytest.raises(ValidationError):
            StudentProfileUpdate(**{anchor: "whatever"})


def test_readme_gender_value_prefer_not_to_say_is_rejected() -> None:
    """Guards the documented vocabulary: 'prefer_not_to_say' is NOT a value.

    The accepted token is 'undisclosed' (schema, model CHECK, migration 0004).
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _payload(gender="prefer_not_to_say")
    with pytest.raises(ValueError):
        _validate_gender("prefer_not_to_say")


def _validate_gender(value):
    from app.schemas.student_profile import _validate_gender as impl

    return impl(value)


# --- hardening: DB CHECK violations surface as 422, never 500 ------------------


def test_update_integrity_error_maps_to_validation_error(session: Session) -> None:
    """A value that slips past the schema hits students_gender_check → 422.

    Calls the service directly (a hypothetical non-schema caller) so the
    database backstop from migration 0004 is exercised end-to-end.
    """
    from sqlalchemy.exc import IntegrityError

    profile = svc.create_student_profile(session, _payload(gender="female"))
    session.commit()

    with pytest.raises(ProfileValidationError) as excinfo:
        svc.update_student_profile(
            session, profile.student_id, gender="robot", changed_by=profile.user_id
        )
    assert excinfo.value.status_code == 422
    session.rollback()
    # Nothing changed: the gender is still the original value.
    assert session.get(Student, profile.student_id).gender == "female"


# --- orphaned profile ----------------------------------------------------------


def test_read_profile_orphan_is_404() -> None:
    student = Student(
        user_id=uuid.uuid4(),
        full_name="Ghost",
        date_of_birth=date(2010, 1, 1),
        gender=None,
        country=None,
    )
    with pytest.raises(ProfileNotFoundError, match="orphaned"):
        svc.read_profile(None, student)


# --- shared PATCH kwarg mapping ------------------------------------------------


def test_profile_update_kwargs_maps_only_supplied_fields() -> None:
    from app.schemas.student_profile import StudentProfileUpdate

    omitted = svc.profile_update_kwargs(StudentProfileUpdate())
    assert omitted == {}

    cleared = svc.profile_update_kwargs(StudentProfileUpdate(country=None))
    assert cleared == {"country": None}  # explicit null → clear

    renamed = svc.profile_update_kwargs(StudentProfileUpdate(full_name="New Name"))
    assert renamed == {"full_name": "New Name"}
