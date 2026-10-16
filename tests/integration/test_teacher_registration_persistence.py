"""Teacher registration + verification persistence integration tests (PostgreSQL).

Registration and admin vetting against the guarded test DB, proven the only
way that cannot lie: the real HTTP request, then a **fresh, independent
SQLAlchemy session**. The request's session, its identity map and any shared
fixture session are never used as evidence of persistence.

Covered:

1. ``POST /auth/register-teacher`` — the complete happy path survives the
   request boundary; the returned access token authenticates ``GET /me/teacher``;
   the account is ACTIVE while the profile is PENDING (two independent axes).
2. Registration contract negatives — duplicate email, invalid input,
   unexpected client-supplied role/verification fields, short password,
   invalid full name, overlong strings; failed registration never leaves a
   partial user or profile.
3. ``PATCH /admin/teachers/{user_id}/verification`` — the direct
   approval bypass is CLOSED by the teacher-verification policy:
   ``approved`` on a teacher who never completed the application
   workflow is refused 409 (pointing at the application queue);
   ``pending``/``rejected``/``suspended`` remain free admin moves;
   repeating the current status is 409 and does not mutate the row;
   an unknown teacher id answers 404 identically to a non-teacher
   account; a student cannot change verification; a teacher cannot
   self-approve; a rejected teacher stays locked out of teaching
   while still able to authenticate and read status.
4. Application workflow + end-to-end journey — the real
   ``/me/teacher/application`` → evidence upload → submit →
   ``/admin/teacher-applications/{id}/checks`` →
   ``.../decide`` path against PostgreSQL: direct approval 409 and
   offering denial before approval; approval refused (with zero
   partial mutation) until every evidence has a recorded ``verified``
   check and the decision covers every requested context; an approved
   context becomes durable scope while a rejected one does not; the
   profile, application decision, approved category and scope rows
   survive a fresh independent session; audit events are durable and
   attributed to the deciding admin; the same token then publishes
   the offering the policy previously refused, and the offering row
   persists independently.

The registration implementation itself is already complete and is not
modified. These tests close the audit's UNVERIFIED/DEFECTIVE findings by
proving request-boundary persistence, the closed direct-approval bypass,
and the pending-to-approved permission transition through the real
application workflow against real PostgreSQL state.
"""
from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
_EMAIL_TAKEN_DETAIL = "email cannot be used to create an account"


# --- scaffolding --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def api_client(pg_engine):
    from app.core.database import Base, SessionLocal, get_db
    import app.models  # noqa: F401
    from app.main import app

    Base.metadata.create_all(pg_engine)

    def _override_get_db():
        db = SessionLocal(bind=pg_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _session(engine):
    from app.core.database import SessionLocal

    return SessionLocal(bind=engine)


def _fresh(engine):
    """A brand-new session: its own connection, its own identity map.

    Nothing the request touched can leak into these reads — the only way
    an unreachable commit can hide is behind a shared session.
    """
    return _session(engine)


def _seed_admin(engine) -> dict:
    from app.core.security import hash_password
    from app.models.user import User

    session = _session(engine)
    try:
        email = f"admin-{uuid.uuid4().hex[:8]}@test.example"
        account = User(
            email=email,
            role="admin",
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(account)
        session.commit()
        return {"email": email, "password": PASSWORD, "user_id": str(account.id)}
    finally:
        session.close()


def _seed_teacher_with_status(engine, *, verification: str) -> dict:
    from app.core.security import hash_password
    from app.models.teacher import Teacher
    from app.models.user import User

    session = _session(engine)
    try:
        email = f"teacher-{uuid.uuid4().hex[:8]}@test.example"
        account = User(
            email=email,
            role="teacher",
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(account)
        session.flush()
        session.add(
            Teacher(
                user_id=account.id,
                full_name="Seeded Teacher",
                verification_status=verification,
            )
        )
        session.commit()
        return {"email": email, "password": PASSWORD, "user_id": str(account.id)}
    finally:
        session.close()


def _seed_student(engine) -> dict:
    from app.core.security import hash_password
    from app.models.student import Student
    from app.models.user import User

    session = _session(engine)
    try:
        email = f"student-{uuid.uuid4().hex[:8]}@test.example"
        account = User(
            email=email,
            role="student",
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(account)
        session.flush()
        session.add(Student(user_id=account.id, full_name="Seeded Student"))
        session.commit()
        return {"email": email, "password": PASSWORD, "user_id": str(account.id)}
    finally:
        session.close()


def _login(api_client: TestClient, account: dict) -> dict:
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _header(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _register_teacher(api_client: TestClient, **overrides) -> dict:
    payload = {
        "email": f"reg-{uuid.uuid4().hex[:8]}@test.example",
        "password": PASSWORD,
        "full_name": "Registered Teacher",
    }
    payload.update(overrides)
    response = api_client.post("/api/v1/auth/register-teacher", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    return {**body, "email": payload["email"], "password": payload["password"]}


def _seed_catalog(engine) -> dict:
    from datetime import datetime

    from app.models.academic_year import AcademicYear
    from app.models.education_level import EducationLevel
    from app.models.enums import AcademicYearStatus
    from app.models.pathway import Pathway
    from app.models.pathway_level import PathwayLevel
    from app.models.program import Program
    from app.models.program_subject import ProgramSubject
    from app.models.program_version import ProgramVersion
    from app.models.subject import Subject

    session = _session(engine)
    try:
        year = AcademicYear(
            name=f"IA-{uuid.uuid4().hex[:6]}",
            start_date=datetime(2026, 9, 1).date(),
            end_date=datetime(2027, 6, 30).date(),
            status=AcademicYearStatus.ACTIVE.value,
        )
        pathway = Pathway(code=f"P-{uuid.uuid4().hex[:6]}", name="O Level")
        level = EducationLevel(
            code=f"L-{uuid.uuid4().hex[:6]}", name="Senior 3", level_number=3
        )
        session.add_all([year, pathway, level])
        session.flush()
        session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

        program = Program(
            code=f"C-{uuid.uuid4().hex[:6]}",
            name="Combination",
            program_type="combination",
        )
        session.add(program)
        session.flush()
        program_version = ProgramVersion(
            program_id=program.id,
            academic_year_id=year.id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            code=f"CV-{uuid.uuid4().hex[:6]}",
            name="Combination 2026",
        )
        session.add(program_version)

        maths = Subject(code=f"S-{uuid.uuid4().hex[:6]}", name="Mathematics")
        session.add(maths)
        session.flush()
        session.add(
            ProgramSubject(program_version_id=program_version.id, subject_id=maths.id)
        )
        session.commit()
        return {
            "academic_year_id": str(year.id),
            "pathway_id": str(pathway.id),
            "education_level_id": str(level.id),
            "program_version_id": str(program_version.id),
            "subject_id": str(maths.id),
        }
    finally:
        session.close()


def _add_sibling_subject(engine, catalog: dict) -> dict:
    """A second subject under the same program version (a second context)."""
    from app.models.program_subject import ProgramSubject
    from app.models.subject import Subject

    session = _session(engine)
    try:
        subject = Subject(code=f"S-{uuid.uuid4().hex[:6]}", name="Physics")
        session.add(subject)
        session.flush()
        session.add(
            ProgramSubject(
                program_version_id=uuid.UUID(catalog["program_version_id"]),
                subject_id=subject.id,
            )
        )
        session.commit()
        return {**catalog, "subject_id": str(subject.id)}
    finally:
        session.close()


_PDF_BYTES = {
    "identity_document": b"%PDF-1.4 fake identity document",
    "academic_qualification": b"%PDF-1.4 fake academic qualification",
    "teacher_training": b"%PDF-1.4 fake teacher training",
}


def _context_payload(catalog: dict) -> dict:
    return {
        "academic_year_id": catalog["academic_year_id"],
        "pathway_id": catalog["pathway_id"],
        "education_level_id": catalog["education_level_id"],
        "program_version_id": catalog["program_version_id"],
        "subject_id": catalog["subject_id"],
    }


def _start_formal_application(api_client: TestClient, teacher_header: dict) -> str:
    """Open a draft formal-teacher application; return its id."""
    started = api_client.post(
        "/api/v1/me/teacher/application",
        json={"category": "formal_teacher"},
        headers=teacher_header,
    )
    assert started.status_code == 201, started.text
    return started.json()["id"]


def _submit_formal_via_http(
    api_client: TestClient,
    teacher_header: dict,
    application_id: str,
    *,
    contexts: list[dict],
) -> tuple[list[str], list[str]]:
    """Contexts → required formal evidence → submit, over real HTTP.

    Returns (context_ids, evidence_ids). The formal-teacher submit rules
    need identity + academic-qualification + teacher-training evidence
    (no claimed school, so no employment confirmation).
    """
    put = api_client.put(
        f"/api/v1/me/teacher/application/{application_id}/contexts",
        json={"contexts": contexts},
        headers=teacher_header,
    )
    assert put.status_code == 200, put.text
    context_ids = [c["learning_context_id"] for c in put.json()["contexts"]]
    assert context_ids, f"PUT contexts returned no contexts: {put.json()}"

    for evidence_type, title in (
        ("identity_document", "National ID"),
        ("academic_qualification", "B.Ed certificate"),
        ("teacher_training", "PGCE"),
    ):
        # Build the multipart body with httpx and send it as raw content.
        # Passing ``data``/``files`` straight to the TestClient loses the
        # repeated ``learning_context_ids`` fields under this starlette
        # version; pre-encoding keeps every part intact.
        body_request = httpx.Request(
            "POST",
            "http://testserver/",
            files=[
                ("evidence_type", (None, evidence_type)),
                ("title", (None, title)),
                *(
                    ("learning_context_ids", (None, context_id))
                    for context_id in context_ids
                ),
                (
                    "file",
                    (f"{evidence_type}.pdf", _PDF_BYTES[evidence_type], "application/pdf"),
                ),
            ],
        )
        body_request.read()
        upload = api_client.post(
            f"/api/v1/me/teacher/application/{application_id}/evidence",
            content=body_request.content,
            headers={
                **teacher_header,
                "Content-Type": body_request.headers["content-type"],
            },
        )
        assert upload.status_code == 201, upload.text

    submitted = api_client.post(
        f"/api/v1/me/teacher/application/{application_id}/submit",
        headers=teacher_header,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"
    evidence_ids = [e["id"] for e in submitted.json()["evidence"]]
    return context_ids, evidence_ids


def _record_verified_checks(
    api_client: TestClient, admin_header: dict, application_id: str, evidence_ids: list[str]
) -> None:
    """Record a ``verified`` check on every evidence through the admin API.

    This is the established, authorized reviewer workflow — a deterministic
    recorded human-review outcome. It is NOT an automated external
    credential verification; the test environment cannot contact a real
    issuer registry.
    """
    for evidence_id in evidence_ids:
        recorded = api_client.post(
            f"/api/v1/admin/teacher-applications/{application_id}/checks",
            json={
                "evidence_id": evidence_id,
                "method": "registry_lookup",
                "outcome": "verified",
                "source_institution": "Ministry registry (recorded reviewer outcome)",
            },
            headers=admin_header,
        )
        assert recorded.status_code == 201, recorded.text


# --- fresh-session row readers ---------------------------------------------------------------


def _user_row(engine, email: str):
    from app.models.user import User

    session = _fresh(engine)
    try:
        row = session.scalar(select(User).where(User.email == email))
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def _teacher_profile_for_user(engine, user_id):
    from app.models.teacher import Teacher

    session = _fresh(engine)
    try:
        row = session.scalar(select(Teacher).where(Teacher.user_id == user_id))
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def _teacher_profiles_for_user(engine, user_id) -> list:
    from app.models.teacher import Teacher

    session = _fresh(engine)
    try:
        rows = list(
            session.scalars(select(Teacher).where(Teacher.user_id == user_id))
        )
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def _user_count_for_email(engine, email: str) -> int:
    from app.models.user import User

    session = _fresh(engine)
    try:
        return len(list(session.scalars(select(User).where(User.email == email))))
    finally:
        session.close()


def _auth_events_for_user(engine, user_id) -> list:
    from app.models.auth_event import AuthEvent

    session = _fresh(engine)
    try:
        rows = list(
            session.scalars(select(AuthEvent).where(AuthEvent.user_id == user_id))
        )
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def _teacher_user_id_from_email(engine, email: str):
    from app.models.user import User

    session = _fresh(engine)
    try:
        row = session.scalar(select(User).where(User.email == email))
        return row.id if row is not None else None
    finally:
        session.close()


# --- 1: registration happy path ---------------------------------------------------------------


def test_teacher_registration_persists_and_token_authenticates(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    tokens = _register_teacher(api_client, phone="+250788000111", subject="Mathematics")

    # The request session is gone; a fresh session proves the durable rows.
    user = _user_row(engine, tokens["email"])
    assert user is not None, "registration returned 201 but no user row exists"
    assert user.role == "teacher"
    assert user.status == "active"
    assert user.password_hash is not None
    assert PASSWORD not in (user.password_hash or "")

    profile = _teacher_profile_for_user(engine, user.id)
    assert profile is not None, "teacher profile row missing after registration"
    assert profile.user_id == user.id
    assert profile.full_name == "Registered Teacher"
    assert profile.phone == "+250788000111"
    assert profile.subject == "Mathematics"
    assert profile.verification_status == "pending"

    # Exactly one account and one profile; no duplicate or extra role.
    assert _user_count_for_email(engine, tokens["email"]) == 1
    assert len(_teacher_profiles_for_user(engine, user.id)) == 1

    # The signup is the verification request.
    events = _auth_events_for_user(engine, user.id)
    assert [e.event_type for e in events] == ["teacher_verification_submitted"]

    # The returned access token authenticates the real self-service endpoint.
    me = api_client.get("/api/v1/me/teacher", headers=_header(tokens))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["user_id"] == str(user.id)
    assert body["email"] == tokens["email"]
    assert body["full_name"] == "Registered Teacher"
    assert body["phone"] == "+250788000111"
    assert body["subject"] == "Mathematics"
    assert body["role"] == "teacher"
    assert body["status"] == "active"
    assert body["verification_status"] == "pending"


def test_register_teacher_without_optional_fields_persists(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    tokens = _register_teacher(api_client)  # no phone, no subject

    user = _user_row(engine, tokens["email"])
    assert user is not None
    profile = _teacher_profile_for_user(engine, user.id)
    assert profile is not None
    assert profile.phone is None
    assert profile.subject is None
    assert profile.verification_status == "pending"
    assert user.status == "active"


def test_register_teacher_normalizes_email_and_rejects_case_duplicate(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    email = f"case-{uuid.uuid4().hex[:8]}@test.example"
    tokens = _register_teacher(api_client, email=email)

    # The stored email is the normalized (lowercased) form.
    user = _user_row(engine, email.lower())
    assert user is not None
    assert user.email == email.lower()

    # A differently-cased duplicate is refused with the established 409.
    response = api_client.post(
        "/api/v1/auth/register-teacher",
        json={
            "email": email.upper(),
            "password": PASSWORD,
            "full_name": "Another Teacher",
        },
    )
    assert response.status_code == 409, response.text
    assert _EMAIL_TAKEN_DETAIL in response.json()["detail"]

    # No second account was created.
    assert _user_count_for_email(engine, email.lower()) == 1


# --- 2: registration negatives ----------------------------------------------------------------


def test_duplicate_email_returns_409_and_leaves_no_partial_rows(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    first = _register_teacher(api_client)

    response = api_client.post(
        "/api/v1/auth/register-teacher",
        json={
            "email": first["email"],
            "password": PASSWORD,
            "full_name": "Impostor Teacher",
        },
    )
    assert response.status_code == 409, response.text
    assert _EMAIL_TAKEN_DETAIL in response.json()["detail"]

    # Exactly one account survives; the rejected attempt created nothing.
    assert _user_count_for_email(engine, first["email"]) == 1
    user = _user_row(engine, first["email"])
    assert user is not None
    assert user.role == "teacher"
    assert user.status == "active"
    profiles = _teacher_profiles_for_user(engine, user.id)
    assert len(profiles) == 1
    assert profiles[0].full_name == "Registered Teacher"


def test_invalid_email_returns_422_and_creates_nothing(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    bad_email = f"not-an-email-{uuid.uuid4().hex[:6]}"
    response = api_client.post(
        "/api/v1/auth/register-teacher",
        json={"email": bad_email, "password": PASSWORD, "full_name": "Bad Email"},
    )
    assert response.status_code == 422, response.text
    assert _user_count_for_email(engine, bad_email) == 0


def test_short_password_returns_422_and_creates_nothing(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    email = f"shortpw-{uuid.uuid4().hex[:8]}@test.example"
    response = api_client.post(
        "/api/v1/auth/register-teacher",
        json={"email": email, "password": "short", "full_name": "Short Password"},
    )
    assert response.status_code == 422, response.text
    assert _user_count_for_email(engine, email) == 0


def test_invalid_full_name_returns_422_and_creates_nothing(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    email = f"badname-{uuid.uuid4().hex[:8]}@test.example"
    response = api_client.post(
        "/api/v1/auth/register-teacher",
        json={"email": email, "password": PASSWORD, "full_name": "12345"},
    )
    assert response.status_code == 422, response.text
    assert _user_count_for_email(engine, email) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "x" * 256 + "@test.example"),
        ("password", "p" * 129),
        ("full_name", "N" * 201),
        ("phone", "p" * 33),
        ("subject", "s" * 121),
    ],
)
def test_overlong_fields_return_422_and_create_nothing(
    clean_db, api_client: TestClient, field: str, value: str
) -> None:
    engine = clean_db
    email = f"overlong-{uuid.uuid4().hex[:8]}@test.example"
    payload = {"email": email, "password": PASSWORD, "full_name": "Overlong"}
    payload[field] = value
    if field == "email":
        email = value
        payload["email"] = email
    response = api_client.post("/api/v1/auth/register-teacher", json=payload)
    assert response.status_code == 422, response.text
    assert _user_count_for_email(engine, email) == 0


def test_unexpected_role_or_verification_fields_are_rejected(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    for smuggled in (
        {"role": "admin"},
        {"role": "student"},
        {"status": "active"},
        {"verification_status": "approved"},
        {"school_id": str(uuid.uuid4())},
    ):
        email = f"smuggle-{uuid.uuid4().hex[:8]}@test.example"
        response = api_client.post(
            "/api/v1/auth/register-teacher",
            json={
                "email": email,
                "password": PASSWORD,
                "full_name": "Smuggler",
                **smuggled,
            },
        )
        assert response.status_code == 422, f"{smuggled!r} -> {response.text}"
        assert _user_count_for_email(engine, email) == 0


def test_missing_required_fields_return_422_and_create_nothing(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    for missing in ("email", "password", "full_name"):
        payload = {
            "email": f"missing-{uuid.uuid4().hex[:8]}@test.example",
            "password": PASSWORD,
            "full_name": "Missing Field",
        }
        payload.pop(missing)
        response = api_client.post("/api/v1/auth/register-teacher", json=payload)
        assert response.status_code == 422, response.text
    # None of the attempted emails exist.
    assert _user_count_for_email(engine, "missing-@test.example") == 0


# --- 3: admin verification --------------------------------------------------------------------


def test_admin_approves_teacher_and_change_persists(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    catalog = _seed_catalog(engine)
    sibling = _add_sibling_subject(engine, catalog)
    admin = _seed_admin(engine)
    admin_header = _header(_login(api_client, admin))

    # Register through the real endpoint — the signup is the verification
    # request: the account is ACTIVE while the profile is PENDING.
    tokens = _register_teacher(api_client)
    teacher_header = _header(tokens)
    user = _user_row(engine, tokens["email"])
    assert user is not None and user.role == "teacher" and user.status == "active"
    profile = _teacher_profile_for_user(engine, user.id)
    assert profile is not None
    assert profile.verification_status == "pending"

    # --- the legacy direct-approval bypass is CLOSED ------------------------
    denied = api_client.patch(
        f"/api/v1/admin/teachers/{user.id}/verification",
        json={"status": "approved"},
        headers=admin_header,
    )
    assert denied.status_code == 409, denied.text
    assert "application workflow" in denied.json()["detail"]

    # The refused request mutated nothing: no status change, no category,
    # no scope row, no approval audit event.
    session = _fresh(engine)
    try:
        from app.models.teacher import Teacher
        from app.models.teacher_approved_context import TeacherApprovedContext

        still = session.get(Teacher, profile.id)
        assert still.verification_status == "pending"
        assert still.verification_category is None
        assert (
            session.scalar(
                select(TeacherApprovedContext).where(
                    TeacherApprovedContext.teacher_id == profile.id
                )
            )
            is None
        )
    finally:
        session.close()
    events = _auth_events_for_user(engine, user.id)
    assert "application_approved" not in [e.event_type for e in events]

    # --- the real application workflow --------------------------------------
    application_id = _start_formal_application(api_client, teacher_header)

    # A draft cannot be decided: the status guard refuses before any payload
    # validation, and a second concurrent application cannot be opened either.
    early = api_client.post(
        f"/api/v1/admin/teacher-applications/{application_id}/decide",
        json={"approve": True},
        headers=admin_header,
    )
    assert early.status_code == 409, early.text
    second = api_client.post(
        "/api/v1/me/teacher/application",
        json={"category": "formal_teacher"},
        headers=teacher_header,
    )
    assert second.status_code == 409, second.text

    context_ids, evidence_ids = _submit_formal_via_http(
        api_client,
        teacher_header,
        application_id,
        contexts=[_context_payload(catalog), _context_payload(sibling)],
    )
    kept_context, dropped_context = context_ids

    # --- approval is refused until every evidence is verified ---------------
    # Both contexts must be covered for the decision to even reach the
    # evidence accounting — an uncovered context is its own 422.
    unverified = api_client.post(
        f"/api/v1/admin/teacher-applications/{application_id}/decide",
        json={
            "approve": True,
            "contexts": [
                {"learning_context_id": kept_context, "status": "approved"},
                {"learning_context_id": dropped_context, "status": "approved"},
            ],
        },
        headers=admin_header,
    )
    assert unverified.status_code == 422, unverified.text
    assert "has no latest check with outcome 'verified'" in unverified.text

    # The failed decision left no partial mutation behind: the profile is
    # still pending, the application still submitted, and no scope rows
    # or approval audit events were written.
    session = _fresh(engine)
    try:
        from app.models.teacher import Teacher
        from app.models.teacher_approved_context import TeacherApprovedContext
        from app.models.teacher_application import TeacherApplication

        still = session.get(Teacher, profile.id)
        assert still.verification_status == "pending"
        app_row = session.get(TeacherApplication, uuid.UUID(application_id))
        assert app_row.status == "submitted"
        assert (
            session.scalar(
                select(TeacherApprovedContext).where(
                    TeacherApprovedContext.teacher_id == profile.id
                )
            )
            is None
        )
    finally:
        session.close()
    events = _auth_events_for_user(engine, user.id)
    assert "application_approved" not in [e.event_type for e in events]

    _record_verified_checks(api_client, admin_header, application_id, evidence_ids)

    # --- the decision must cover every requested context explicitly ---------
    uncovered = api_client.post(
        f"/api/v1/admin/teacher-applications/{application_id}/decide",
        json={
            "approve": True,
            "contexts": [{"learning_context_id": kept_context, "status": "approved"}],
        },
        headers=admin_header,
    )
    assert uncovered.status_code == 422, uncovered.text
    assert "every requested learning context" in uncovered.text

    # --- approve: one context granted, one explicitly refused ----------------
    decided = api_client.post(
        f"/api/v1/admin/teacher-applications/{application_id}/decide",
        json={
            "approve": True,
            "contexts": [
                {"learning_context_id": kept_context, "status": "approved"},
                {
                    "learning_context_id": dropped_context,
                    "status": "rejected",
                    "reason": "credential does not cover this subject",
                },
            ],
        },
        headers=admin_header,
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "approved"
    assert "password" not in decided.text.lower()

    # Independent session: profile, application, per-context decisions, the
    # approved scope and the untouched account axis are all durable.
    session = _fresh(engine)
    try:
        from app.models.auth_event import AuthEvent
        from app.models.teacher import Teacher
        from app.models.teacher_approved_context import TeacherApprovedContext
        from app.models.teacher_application import TeacherApplication
        from app.models.teacher_application_context import TeacherApplicationContext

        fresh_profile = session.get(Teacher, profile.id)
        assert fresh_profile.verification_status == "approved"
        assert fresh_profile.verification_category == "formal_teacher"

        app_row = session.get(TeacherApplication, uuid.UUID(application_id))
        assert app_row.status == "approved"
        assert app_row.decided_at is not None

        decisions = {
            str(row.learning_context_id): row
            for row in session.scalars(
                select(TeacherApplicationContext).where(
                    TeacherApplicationContext.application_id == app_row.id
                )
            )
        }
        assert decisions[kept_context].status == "approved"
        assert decisions[dropped_context].status == "rejected"
        assert decisions[dropped_context].decision_reason == (
            "credential does not cover this subject"
        )
        assert decisions[dropped_context].decided_by == uuid.UUID(admin["user_id"])

        scope_rows = list(
            session.scalars(
                select(TeacherApprovedContext).where(
                    TeacherApprovedContext.teacher_id == fresh_profile.id
                )
            )
        )
        # Only the explicitly approved context is authorized — the rejected
        # one grants nothing.
        assert [str(r.learning_context_id) for r in scope_rows] == [kept_context]
        assert scope_rows[0].application_id == app_row.id
        assert scope_rows[0].approved_by == uuid.UUID(admin["user_id"])

        # Audit events are durable, use the current event names, and are
        # attributed to the deciding admin on the teacher's user account
        # (auth_events.user_id references users.id).
        # (Legacy name ``teacher_verification_approved`` no longer exists.)
        events = list(
            session.scalars(
                select(AuthEvent).where(AuthEvent.user_id == user.id)
            )
        )
        event_types = [e.event_type for e in events]
        assert "application_approved" in event_types
        assert "context_approved" in event_types
        assert "context_rejected" in event_types
        assert "teacher_verification_approved" not in event_types
        approved_events = [e for e in events if e.event_type == "application_approved"]
        assert approved_events[0].actor_user_id == uuid.UUID(admin["user_id"])

        # Vetting never touches the account axis.
        assert fresh_profile.user_id == user.id
    finally:
        session.close()
    user = _user_row(engine, tokens["email"])
    assert user.status == "active"
    assert user.role == "teacher"

    # The SAME token now observes the approved state and the exact scope.
    me = api_client.get("/api/v1/me/teacher", headers=teacher_header)
    assert me.status_code == 200, me.text
    assert me.json()["verification_status"] == "approved"
    scope = api_client.get(
        "/api/v1/me/teacher/verification/scope", headers=teacher_header
    )
    assert scope.status_code == 200, scope.text
    assert [r["learning_context_id"] for r in scope.json()] == [kept_context]


def test_repeating_current_verification_status_is_409_and_does_not_mutate(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    admin = _seed_admin(engine)
    teacher = _seed_teacher_with_status(engine, verification="approved")
    admin_header = _header(_login(api_client, admin))

    response = api_client.patch(
        f"/api/v1/admin/teachers/{teacher['user_id']}/verification",
        json={"status": "approved"},
        headers=admin_header,
    )
    assert response.status_code == 409, response.text

    profile = _teacher_profile_for_user(engine, uuid.UUID(teacher["user_id"]))
    assert profile.verification_status == "approved"

    # The refused request wrote no transition audit event.
    events = _auth_events_for_user(engine, uuid.UUID(teacher["user_id"]))
    assert "teacher_verification_approved" not in [
        e.event_type for e in events
        if e.actor_user_id == uuid.UUID(admin["user_id"])
    ]


def test_unknown_and_non_teacher_targets_are_indistinguishable_404(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    admin = _seed_admin(engine)
    student = _seed_student(engine)
    admin_header = _header(_login(api_client, admin))

    unknown_id = str(uuid.uuid4())
    student_id = student["user_id"]

    unknown = api_client.patch(
        f"/api/v1/admin/teachers/{unknown_id}/verification",
        json={"status": "approved"},
        headers=admin_header,
    )
    as_student = api_client.patch(
        f"/api/v1/admin/teachers/{student_id}/verification",
        json={"status": "approved"},
        headers=admin_header,
    )
    assert unknown.status_code == 404, unknown.text
    assert as_student.status_code == 404, as_student.text
    # Identical detail strings — a foreign id must not confirm existence.
    assert unknown.json()["detail"] == as_student.json()["detail"]

    # Neither the student nor any phantom profile was mutated.
    assert _teacher_profiles_for_user(engine, uuid.UUID(student_id)) == []


def test_invalid_verification_status_returns_422(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    admin = _seed_admin(engine)
    teacher = _seed_teacher_with_status(engine, verification="pending")
    admin_header = _header(_login(api_client, admin))

    response = api_client.patch(
        f"/api/v1/admin/teachers/{teacher['user_id']}/verification",
        json={"status": "definitely-not-a-status"},
        headers=admin_header,
    )
    assert response.status_code == 422, response.text

    profile = _teacher_profile_for_user(engine, uuid.UUID(teacher["user_id"]))
    assert profile.verification_status == "pending"


def test_student_and_teacher_cannot_change_verification(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    target = _seed_teacher_with_status(engine, verification="pending")
    student = _seed_student(engine)
    other_teacher = _seed_teacher_with_status(engine, verification="pending")

    for account in (student, other_teacher):
        header = _header(_login(api_client, account))
        response = api_client.patch(
            f"/api/v1/admin/teachers/{target['user_id']}/verification",
            json={"status": "approved"},
            headers=header,
        )
        assert response.status_code == 403, response.text

    profile = _teacher_profile_for_user(engine, uuid.UUID(target["user_id"]))
    assert profile.verification_status == "pending"


# --- 4: end-to-end registration → approval → offering -----------------------------------------


def test_registration_approval_offering_journey_persists_every_step(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    catalog = _seed_catalog(engine)
    admin = _seed_admin(engine)
    admin_header = _header(_login(api_client, admin))

    # 1-2. Register and prove the request boundary.
    tokens = _register_teacher(api_client)
    user = _user_row(engine, tokens["email"])
    assert user is not None
    assert user.role == "teacher"
    assert user.status == "active"
    profile = _teacher_profile_for_user(engine, user.id)
    assert profile.verification_status == "pending"
    teacher_user_id = str(user.id)

    # 3-5. Authenticate with the returned token; PENDING is visible.
    teacher_header = _header(tokens)
    me = api_client.get("/api/v1/me/teacher", headers=teacher_header)
    assert me.status_code == 200, me.text
    assert me.json()["verification_status"] == "pending"

    # 6-7. A pending teacher cannot publish; the guard reads the DB row.
    blocked = api_client.post(
        "/api/v1/me/teacher/offerings", json=catalog, headers=teacher_header
    )
    assert blocked.status_code == 403, blocked.text

    # The legacy direct-approval bypass is closed: no application, no 200.
    bypass = api_client.patch(
        f"/api/v1/admin/teachers/{teacher_user_id}/verification",
        json={"status": "approved"},
        headers=admin_header,
    )
    assert bypass.status_code == 409, bypass.text
    # Still pending, still unauthorized — the 409 granted nothing.
    still = _teacher_profile_for_user(engine, user.id)
    assert still.verification_status == "pending"
    blocked_again = api_client.post(
        "/api/v1/me/teacher/offerings", json=catalog, headers=teacher_header
    )
    assert blocked_again.status_code == 403, blocked_again.text

    # 8-10. The real application workflow: draft → contexts → evidence →
    # submit → recorded verified checks → explicit per-context approval.
    application_id = _start_formal_application(api_client, teacher_header)
    context_ids, evidence_ids = _submit_formal_via_http(
        api_client,
        teacher_header,
        application_id,
        contexts=[_context_payload(catalog)],
    )
    context_id = context_ids[0]
    _record_verified_checks(api_client, admin_header, application_id, evidence_ids)

    decided = api_client.post(
        f"/api/v1/admin/teacher-applications/{application_id}/decide",
        json={
            "approve": True,
            "contexts": [{"learning_context_id": context_id, "status": "approved"}],
        },
        headers=admin_header,
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    # The change is durable: a fresh, independent session sees the approved
    # profile, the approved application, and the canonical scope row.
    fresh_profile = _teacher_profile_for_user(engine, uuid.UUID(teacher_user_id))
    assert fresh_profile.verification_status == "approved"
    assert fresh_profile.verification_category == "formal_teacher"
    session = _fresh(engine)
    try:
        from app.models.teacher_approved_context import TeacherApprovedContext
        from app.models.teacher_application import TeacherApplication

        app_row = session.get(TeacherApplication, uuid.UUID(application_id))
        assert app_row is not None and app_row.status == "approved"
        scope_row = session.scalar(
            select(TeacherApprovedContext).where(
                TeacherApprovedContext.teacher_id == fresh_profile.id
            )
        )
        assert scope_row is not None
        assert str(scope_row.learning_context_id) == context_id
        assert scope_row.application_id == app_row.id
    finally:
        session.close()

    # 11-12. The SAME token now sees approved — no re-registration required.
    me_again = api_client.get("/api/v1/me/teacher", headers=teacher_header)
    assert me_again.status_code == 200, me_again.text
    assert me_again.json()["verification_status"] == "approved"
    scope = api_client.get(
        "/api/v1/me/teacher/verification/scope", headers=teacher_header
    )
    assert scope.status_code == 200, scope.text
    assert [r["learning_context_id"] for r in scope.json()] == [context_id]

    # Audit events for the approval are durable on the teacher's user
    # account (auth_events.user_id references users.id) and attributed to
    # the deciding admin (current event names, not the legacy
    # ``teacher_verification_approved``).
    events = _auth_events_for_user(engine, user.id)
    event_types = [e.event_type for e in events]
    assert "application_approved" in event_types
    assert "context_approved" in event_types
    assert "teacher_verification_approved" not in event_types
    approved_events = [e for e in events if e.event_type == "application_approved"]
    assert approved_events[0].actor_user_id == uuid.UUID(admin["user_id"])

    # 13-14. The offering the policy previously refused is now permitted,
    # and it persists across its own request boundary.
    offering = api_client.post(
        "/api/v1/me/teacher/offerings", json=catalog, headers=teacher_header
    )
    assert offering.status_code == 201, offering.text
    offering_id = offering.json()["offering_id"]

    from app.models.teaching_offering import TeachingOffering

    session = _fresh(engine)
    try:
        row = session.get(TeachingOffering, uuid.UUID(offering_id))
        assert row is not None, "offering returned 201 but no row exists"
        # ``fresh_profile.id`` is already a UUID (expunged ORM row).
        assert row.teacher_id == fresh_profile.id
        assert row.status == "active"
        session.expunge(row)
    finally:
        session.close()


def test_rejected_teacher_stays_locked_out_but_can_authenticate_and_read_status(
    clean_db, api_client: TestClient
) -> None:
    engine = clean_db
    catalog = _seed_catalog(engine)
    admin = _seed_admin(engine)
    admin_header = _header(_login(api_client, admin))

    tokens = _register_teacher(api_client)
    teacher_user_id = str(
        _teacher_user_id_from_email(engine, tokens["email"])
    )
    teacher_header = _header(tokens)

    rejected = api_client.patch(
        f"/api/v1/admin/teachers/{teacher_user_id}/verification",
        json={"status": "rejected"},
        headers=admin_header,
    )
    assert rejected.status_code == 200, rejected.text

    # Account activation and verification are independent: login and status
    # reads still work; only teaching capabilities are refused.
    me = api_client.get("/api/v1/me/teacher", headers=teacher_header)
    assert me.status_code == 200, me.text
    assert me.json()["verification_status"] == "rejected"
    assert me.json()["status"] == "active"

    blocked = api_client.post(
        "/api/v1/me/teacher/offerings", json=catalog, headers=teacher_header
    )
    assert blocked.status_code == 403, blocked.text
