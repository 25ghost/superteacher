"""Mutation commit-persistence integration tests (PostgreSQL).

Maintenance patch: nine endpoints used to ``return`` inside their
``try`` block, making the trailing ``session.commit()`` unreachable —
the mutation was flushed into the request-scoped session and silently
discarded when the session closed. Every test here proves the fix the
only way that cannot lie: the HTTP call, then a **fresh, independent
SQLAlchemy session** against the test database. The request's session,
its identity map and any shared fixture session are never used as
evidence of persistence.

Covered endpoints (the nine):

1. ``POST /admin/materials/{id}/approve`` — pending_review → published
2. ``POST /admin/materials/{id}/reject``  — pending_review → rejected (+ reason)
3. ``POST /admin/materials/{id}/archive`` — published → archived
4. ``POST /me/teacher/offerings/{id}/topics``          — topic created
5. ``POST /me/teacher/offerings/{id}/topics/{id}/lessons`` — lesson created
6. ``PATCH /me/teacher/offerings/{id}/materials/{id}`` — metadata amended
7. ``POST .../materials/{id}/submit``  — draft → pending_review
8. ``POST .../materials/{id}/revise``  — rejected → draft
9. ``POST .../materials/{id}/archive`` — published → archived

Plus focused negative coverage: an invalid transition leaves the durable
row untouched, a student token stays 403 on an admin mutation, and a
commit failure never reports success nor persists partial state.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
_PDF_BYTES = b"%PDF-1.4 integration persistence payload"


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


def _seed_catalog(engine) -> dict:
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


def _seed_account(engine, *, role: str) -> dict:
    from app.core.security import hash_password
    from app.models.student import Student
    from app.models.teacher import Teacher
    from app.models.user import User

    session = _session(engine)
    try:
        email = f"{role}-{uuid.uuid4().hex[:8]}@test.example"
        account = User(
            email=email,
            role=role,
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(account)
        session.flush()
        if role == "teacher":
            session.add(
                Teacher(
                    user_id=account.id,
                    full_name="Commit Teacher",
                    subject="Mathematics",
                    verification_status="approved",
                )
            )
        elif role == "student":
            session.add(Student(user_id=account.id, full_name="Commit Student"))
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


@pytest.fixture()
def world(clean_db, api_client: TestClient):
    """Catalog + admin + approved teacher + student + one owned offering."""
    from app.models.user import User
    from app.schemas.learning import TeachingOfferingCreate
    from tests.conftest import approve_teacher_for_payloads

    engine = clean_db
    catalog = _seed_catalog(engine)
    admin = _seed_account(engine, role="admin")
    teacher = _seed_account(engine, role="teacher")
    student = _seed_account(engine, role="student")
    admin_header = _header(_login(api_client, admin))
    teacher_header = _header(_login(api_client, teacher))
    student_header = _header(_login(api_client, student))

    session = _session(engine)
    try:
        teacher_user = session.get(User, uuid.UUID(teacher["user_id"]))
        approve_teacher_for_payloads(
            session, teacher_user, [TeachingOfferingCreate(**catalog)]
        )
        session.commit()
    finally:
        session.close()

    offering = api_client.post(
        "/api/v1/me/teacher/offerings", json=catalog, headers=teacher_header
    )
    assert offering.status_code == 201, offering.text
    return {
        "engine": engine,
        "admin": admin,
        "teacher": teacher,
        "student": student,
        "admin_header": admin_header,
        "teacher_header": teacher_header,
        "student_header": student_header,
        "offering_id": offering.json()["offering_id"],
    }


# --- independent-session verification --------------------------------------------------------


def _fresh(engine):
    """A brand-new session: its own connection, its own identity map.

    Nothing the request touched can leak into these reads — the only way
    an unreachable commit can hide is behind a shared session.
    """
    return _session(engine)


def _material_row(engine, material_id: str):
    from app.models.material import Material

    session = _fresh(engine)
    try:
        row = session.get(Material, uuid.UUID(material_id))
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def _moderation_rows(engine, material_id: str):
    from app.models.material_moderation import MaterialModeration

    session = _fresh(engine)
    try:
        rows = list(
            session.scalars(
                select(MaterialModeration).where(
                    MaterialModeration.material_id == uuid.UUID(material_id)
                )
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def _audit_events(engine, user_id: str, event_type: str) -> list:
    from app.models.auth_event import AuthEvent

    session = _fresh(engine)
    try:
        rows = list(
            session.scalars(
                select(AuthEvent).where(
                    AuthEvent.user_id == uuid.UUID(user_id),
                    AuthEvent.event_type == event_type,
                )
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def _topic_row(engine, topic_id: str):
    from app.models.topic import Topic

    session = _fresh(engine)
    try:
        row = session.get(Topic, uuid.UUID(topic_id))
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def _lesson_row(engine, lesson_id: str):
    from app.models.lesson import Lesson

    session = _fresh(engine)
    try:
        row = session.get(Lesson, uuid.UUID(lesson_id))
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


# --- material helpers (real HTTP, real storage) ----------------------------------------------


def _upload_material(
    api_client: TestClient, world: dict, *, title: str = "Algebra worksheet"
) -> dict:
    response = api_client.post(
        f"/api/v1/me/teacher/offerings/{world['offering_id']}/materials",
        data={"title": title, "material_type": "pdf_document"},
        files={"file": ("worksheet.pdf", _PDF_BYTES, "application/pdf")},
        headers=world["teacher_header"],
    )
    assert response.status_code == 201, response.text
    return response.json()


def _material_base(world: dict, material_id: str) -> str:
    return f"/api/v1/me/teacher/offerings/{world['offering_id']}/materials/{material_id}"


def _admin_base(material_id: str) -> str:
    return f"/api/v1/admin/materials/{material_id}"


def _submit(api_client: TestClient, world: dict, material_id: str) -> dict:
    response = api_client.post(
        f"{_material_base(world, material_id)}/submit",
        headers=world["teacher_header"],
    )
    assert response.status_code == 200, response.text
    return response.json()


def _admin_approve(api_client: TestClient, world: dict, material_id: str):
    return api_client.post(
        f"{_admin_base(material_id)}/approve", headers=world["admin_header"]
    )


def _make_published(api_client: TestClient, world: dict) -> dict:
    material = _upload_material(api_client, world)
    _submit(api_client, world, material["material_id"])
    approved = _admin_approve(api_client, world, material["material_id"])
    assert approved.status_code == 200, approved.text
    return approved.json()


def _make_rejected(api_client: TestClient, world: dict) -> dict:
    material = _upload_material(api_client, world)
    _submit(api_client, world, material["material_id"])
    rejected = api_client.post(
        f"{_admin_base(material['material_id'])}/reject",
        json={"reason": "needs worked solutions"},
        headers=world["admin_header"],
    )
    assert rejected.status_code == 200, rejected.text
    return rejected.json()


def _create_topic(api_client: TestClient, world: dict, *, title: str = "Quadratics") -> dict:
    response = api_client.post(
        f"/api/v1/me/teacher/offerings/{world['offering_id']}/topics",
        json={"title": title, "description": "Unit 1"},
        headers=world["teacher_header"],
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- 1-3: admin materials ---------------------------------------------------------------------


def test_admin_approve_material_persists_before_success(
    api_client: TestClient, world: dict
) -> None:
    material = _upload_material(api_client, world, title="Approval target")
    _submit(api_client, world, material["material_id"])

    response = _admin_approve(api_client, world, material["material_id"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"

    # Independent session: the publication, the decision row and the
    # audit trail are all durable — the request session is long gone.
    row = _material_row(world["engine"], material["material_id"])
    assert row.status == "published"

    moderations = _moderation_rows(world["engine"], material["material_id"])
    assert len(moderations) == 1
    assert moderations[0].decision == "approved"
    assert moderations[0].reason is None
    assert str(moderations[0].reviewer_user_id) == world["admin"]["user_id"]

    events = _audit_events(
        world["engine"], world["teacher"]["user_id"], "material_approved"
    )
    assert len(events) == 1


def test_admin_reject_material_persists_reason_and_reviewer(
    api_client: TestClient, world: dict
) -> None:
    material = _upload_material(api_client, world, title="Rejection target")
    _submit(api_client, world, material["material_id"])

    response = api_client.post(
        f"{_admin_base(material['material_id'])}/reject",
        json={"reason": "missing answers on page 3"},
        headers=world["admin_header"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"

    row = _material_row(world["engine"], material["material_id"])
    assert row.status == "rejected"

    moderations = _moderation_rows(world["engine"], material["material_id"])
    assert len(moderations) == 1
    assert moderations[0].decision == "rejected"
    assert moderations[0].reason == "missing answers on page 3"
    assert str(moderations[0].reviewer_user_id) == world["admin"]["user_id"]

    events = _audit_events(
        world["engine"], world["teacher"]["user_id"], "material_rejected"
    )
    assert len(events) == 1


def test_admin_archive_material_persists_archived_state(
    api_client: TestClient, world: dict
) -> None:
    published = _make_published(api_client, world)

    response = api_client.post(
        f"{_admin_base(published['material_id'])}/archive",
        headers=world["admin_header"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"

    row = _material_row(world["engine"], published["material_id"])
    assert row.status == "archived"
    # Archiving records no moderation row — the domain keeps the prior
    # approve decision as the trail (existing semantics, unchanged).
    moderations = _moderation_rows(world["engine"], published["material_id"])
    assert [m.decision for m in moderations] == ["approved"]


# --- 4-5: teacher curriculum --------------------------------------------------------------------


def test_teacher_topic_creation_persists_topic_row(
    api_client: TestClient, world: dict
) -> None:
    response = api_client.post(
        f"/api/v1/me/teacher/offerings/{world['offering_id']}/topics",
        json={"title": "Quadratics", "description": "Unit 1"},
        headers=world["teacher_header"],
    )
    assert response.status_code == 201, response.text
    topic_id = response.json()["topic_id"]

    row = _topic_row(world["engine"], topic_id)
    assert row is not None
    assert row.title == "Quadratics"
    assert row.description == "Unit 1"
    assert str(row.teaching_offering_id) == world["offering_id"]
    assert row.display_order == 1

    events = _audit_events(world["engine"], world["teacher"]["user_id"], "topic_created")
    assert len(events) == 1


def test_teacher_lesson_creation_persists_lesson_row(
    api_client: TestClient, world: dict
) -> None:
    topic = _create_topic(api_client, world)

    response = api_client.post(
        f"/api/v1/me/teacher/offerings/{world['offering_id']}/topics/"
        f"{topic['topic_id']}/lessons",
        json={"title": "Factorising", "description": "Lesson 1"},
        headers=world["teacher_header"],
    )
    assert response.status_code == 201, response.text
    lesson_id = response.json()["lesson_id"]

    row = _lesson_row(world["engine"], lesson_id)
    assert row is not None
    assert row.title == "Factorising"
    assert row.description == "Lesson 1"
    assert str(row.topic_id) == topic["topic_id"]
    assert row.display_order == 1

    # The relationship chain survives: lesson → topic → this offering.
    topic_row = _topic_row(world["engine"], topic["topic_id"])
    assert str(topic_row.teaching_offering_id) == world["offering_id"]

    events = _audit_events(world["engine"], world["teacher"]["user_id"], "lesson_created")
    assert len(events) == 1


# --- 6-9: teacher materials ----------------------------------------------------------------------


def test_teacher_material_update_persists_changed_fields(
    api_client: TestClient, world: dict
) -> None:
    material = _upload_material(api_client, world, title="Original title")

    response = api_client.patch(
        _material_base(world, material["material_id"]),
        json={"title": "Revised title", "description": "Now with answers"},
        headers=world["teacher_header"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Revised title"

    row = _material_row(world["engine"], material["material_id"])
    assert row.title == "Revised title"
    assert row.description == "Now with answers"

    events = _audit_events(world["engine"], world["teacher"]["user_id"], "material_updated")
    assert len(events) == 1


def test_teacher_material_submission_persists_pending_review(
    api_client: TestClient, world: dict
) -> None:
    material = _upload_material(api_client, world, title="Submission target")

    response = api_client.post(
        f"{_material_base(world, material['material_id'])}/submit",
        headers=world["teacher_header"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending_review"

    row = _material_row(world["engine"], material["material_id"])
    assert row.status == "pending_review"

    events = _audit_events(
        world["engine"], world["teacher"]["user_id"], "material_submitted"
    )
    assert len(events) == 1


def test_teacher_material_revision_persists_draft_again(
    api_client: TestClient, world: dict
) -> None:
    rejected = _make_rejected(api_client, world)

    response = api_client.post(
        f"{_material_base(world, rejected['material_id'])}/revise",
        headers=world["teacher_header"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"

    row = _material_row(world["engine"], rejected["material_id"])
    assert row.status == "draft"

    # The revision keeps the rejection trail intact (the durable decision
    # record is not erased by moving back to draft).
    moderations = _moderation_rows(world["engine"], rejected["material_id"])
    assert [m.decision for m in moderations] == ["rejected"]
    assert moderations[0].reason == "needs worked solutions"

    events = _audit_events(
        world["engine"], world["teacher"]["user_id"], "material_revised"
    )
    assert len(events) == 1


def test_teacher_material_archive_persists_archived_state(
    api_client: TestClient, world: dict
) -> None:
    published = _make_published(api_client, world)

    response = api_client.post(
        f"{_material_base(world, published['material_id'])}/archive",
        headers=world["teacher_header"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"

    row = _material_row(world["engine"], published["material_id"])
    assert row.status == "archived"

    events = _audit_events(
        world["engine"], world["teacher"]["user_id"], "material_archived"
    )
    assert len(events) == 1


# --- negative paths and authorization ------------------------------------------------------------


def test_an_invalid_transition_changes_nothing_durably(
    api_client: TestClient, world: dict
) -> None:
    """Approving a draft (not pending_review) is 409 — and stays a draft."""
    material = _upload_material(api_client, world, title="Untouchable draft")

    response = _admin_approve(api_client, world, material["material_id"])
    assert response.status_code == 409, response.text

    row = _material_row(world["engine"], material["material_id"])
    assert row.status == "draft"
    assert _moderation_rows(world["engine"], material["material_id"]) == []
    assert (
        _audit_events(world["engine"], world["teacher"]["user_id"], "material_approved")
        == []
    )


def test_a_student_token_is_still_refused_an_admin_mutation(
    api_client: TestClient, world: dict
) -> None:
    material = _upload_material(api_client, world)
    _submit(api_client, world, material["material_id"])

    response = api_client.post(
        f"{_admin_base(material['material_id'])}/approve",
        headers=world["student_header"],
    )
    assert response.status_code == 403, response.text

    row = _material_row(world["engine"], material["material_id"])
    assert row.status == "pending_review"


def test_a_commit_failure_never_reports_success_or_persists(
    api_client: TestClient, world: dict
) -> None:
    """A failing commit never reports success, and persists nothing.

    TestClient re-raises the server-side exception (no 2xx/200 sneaks
    through); the dependency teardown closes the session, which discards
    the flushed-but-uncommitted mutation.
    """
    from app.core.database import get_db
    from app.main import app

    material = _upload_material(api_client, world, title="Commit target")
    _submit(api_client, world, material["material_id"])

    original_override = app.dependency_overrides[get_db]
    engine = world["engine"]

    def _failing_commit_get_db():
        from app.core.database import SessionLocal

        db = SessionLocal(bind=engine)

        def _boom() -> None:
            db.rollback()
            raise RuntimeError("simulated commit failure")

        db.commit = _boom  # type: ignore[method-assign]
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _failing_commit_get_db
    try:
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            _admin_approve(api_client, world, material["material_id"])
    finally:
        app.dependency_overrides[get_db] = original_override

    # Nothing committed: still pending_review, no moderation, no audit.
    row = _material_row(engine, material["material_id"])
    assert row.status == "pending_review"
    assert _moderation_rows(engine, material["material_id"]) == []
    assert (
        _audit_events(engine, world["teacher"]["user_id"], "material_approved") == []
    )
