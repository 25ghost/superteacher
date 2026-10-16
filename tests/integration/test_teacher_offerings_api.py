"""Teaching-offering mutation integration tests (PostgreSQL, guarded test DB).

End-to-end contract for the P2 authorization gap against
``super_teacher_db_test``:

- an eligible teacher amends their offering's description and moves it
  through the documented status transitions (200, one audit row each);
- starting from a published offering, a teacher suspended AFTERWARDS
  gets 403 on every mutation — description edit, pause, archive — the
  committed row is untouched (fresh session), no audit event is written
  for a mutation that never happened, and the read path still answers
  200 (reads are not part of this gate);
- the suspension is re-read from the profile row: the very same
  mutations succeed again once the verification is restored;
- a suspended teacher probing an unknown id gets the SAME 403 a known id
  gets — eligibility is decided before ownership, so ids stay
  indistinguishable (L6).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


# --- scaffolding ---------------------------------------------------------------------


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
    yield TestClient(app)
    app.dependency_overrides.clear()


def _session(engine):
    from app.core.database import SessionLocal

    return SessionLocal(bind=engine)


def _seed_catalog(engine) -> dict:
    """A minimal coherent catalog (year, pathway, level, program, subject)."""
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
        level = EducationLevel(code=f"L-{uuid.uuid4().hex[:6]}", name="Senior 3", level_number=3)
        session.add_all([year, pathway, level])
        session.flush()
        session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

        program = Program(
            code=f"C-{uuid.uuid4().hex[:6]}", name="Combination", program_type="combination"
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
    """An ACTIVE account for the role, with the profile the role needs."""
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
                    full_name="Offerings Teacher",
                    subject="Mathematics",
                    verification_status="approved",
                )
            )
        elif role == "student":
            session.add(Student(user_id=account.id, full_name="Offerings Student"))
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


def _stored(engine, offering_id: str) -> dict:
    """The committed offering row, read through a FRESH session."""
    session = _session(engine)
    try:
        row = session.execute(
            text(
                "SELECT status, description FROM teaching_offerings "
                "WHERE id = :id"
            ),
            {"id": uuid.UUID(offering_id)},
        ).one()
        return {"status": row.status, "description": row.description}
    finally:
        session.close()


def _events(engine, user_id: str) -> list[str]:
    """The committed audit trail of one user, read through a FRESH session."""
    from app.models.auth_event import AuthEvent

    session = _session(engine)
    try:
        return [
            e.event_type
            for e in session.scalars(
                select(AuthEvent)
                .where(
                    AuthEvent.user_id == uuid.UUID(user_id),
                    AuthEvent.event_type.like("teaching_offering_%"),
                )
                .order_by(AuthEvent.created_at, AuthEvent.id)
            )
        ]
    finally:
        session.close()


def _set_verification(engine, user_id: str, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE teachers SET verification_status = :status "
                "WHERE user_id = :id"
            ),
            {"status": status, "id": uuid.UUID(user_id)},
        )


@pytest.fixture()
def offering(clean_db, api_client: TestClient):
    """Seeded catalog + logged-in approved teacher + one published offering."""
    from app.models.user import User
    from app.schemas.learning import TeachingOfferingCreate
    from tests.conftest import approve_teacher_for_payloads

    engine = clean_db
    catalog = _seed_catalog(engine)
    teacher = _seed_account(engine, role="teacher")
    teacher_header = _header(_login(api_client, teacher))

    session = _session(engine)
    try:
        teacher_user = session.get(User, uuid.UUID(teacher["user_id"]))
        approve_teacher_for_payloads(
            session, teacher_user, [TeachingOfferingCreate(**catalog)]
        )
        session.commit()
    finally:
        session.close()

    published = api_client.post(
        "/api/v1/me/teacher/offerings", json=catalog, headers=teacher_header
    )
    assert published.status_code == 201, published.text
    return {
        "engine": engine,
        "teacher": teacher,
        "teacher_header": teacher_header,
        "offering_id": published.json()["offering_id"],
    }


# --- amendment authorization (P2) ------------------------------------------------------


def test_an_eligible_teacher_amends_pauses_and_resumes(
    api_client: TestClient, offering: dict
) -> None:
    """Positive control: the ordinary mutations still work, audited once each."""
    url = f"/api/v1/me/teacher/offerings/{offering['offering_id']}"
    header = offering["teacher_header"]

    edited = api_client.patch(
        url, json={"description": "Now with proofs"}, headers=header
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["description"] == "Now with proofs"
    assert edited.json()["status"] == "active"

    paused = api_client.patch(url, json={"status": "paused"}, headers=header)
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"

    resumed = api_client.patch(url, json={"status": "active"}, headers=header)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"

    assert _events(offering["engine"], offering["teacher"]["user_id"]) == [
        "teaching_offering_created",
        "teaching_offering_updated",
        "teaching_offering_status_changed",
        "teaching_offering_status_changed",
    ]


def test_a_suspended_teacher_cannot_mutate_their_offering(
    api_client: TestClient, offering: dict
) -> None:
    """Publish while approved, suspended afterwards: every mutation is refused.

    The refusal must be total — no committed change, no audit row — and
    it must be driven by the database row, so the identical mutations
    succeed again once the verification is restored. Reads stay open:
    the read paths are not part of this gate.
    """
    engine = offering["engine"]
    header = offering["teacher_header"]
    url = f"/api/v1/me/teacher/offerings/{offering['offering_id']}"

    # One ordinary amendment first, so the row has a known committed state.
    edited = api_client.patch(
        url, json={"description": "Committed while eligible"}, headers=header
    )
    assert edited.status_code == 200, edited.text

    # Suspended AFTER the offering was published — the access token is
    # still valid and the account is still active, only the vetting moved.
    _set_verification(engine, offering["teacher"]["user_id"], "suspended")

    for mutation in (
        {"description": "tampered"},
        {"status": "paused"},
        {"status": "archived"},
    ):
        refused = api_client.patch(url, json=mutation, headers=header)
        assert refused.status_code == 403, refused.text
        message = refused.json()["detail"]
        assert "approved teacher" in message
        # A refusal never confirms which offering was probed (L6).
        assert offering["offering_id"] not in message

    # Nothing partially changed: a FRESH session sees the committed row,
    # and no audit event was written for a mutation that never happened.
    assert _stored(engine, offering["offering_id"]) == {
        "status": "active",
        "description": "Committed while eligible",
    }
    assert _events(engine, offering["teacher"]["user_id"]) == [
        "teaching_offering_created",
        "teaching_offering_updated",
    ]

    # Reads are untouched by the gate: the suspended teacher still sees it.
    read = api_client.get(url, headers=header)
    assert read.status_code == 200, read.text
    assert read.json()["status"] == "active"

    # The check reads the ROW, not the token: restoring the verification
    # lets the very same pause-and-resume through.
    _set_verification(engine, offering["teacher"]["user_id"], "approved")

    paused = api_client.patch(url, json={"status": "paused"}, headers=header)
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"

    resumed = api_client.patch(url, json={"status": "active"}, headers=header)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"

    assert _events(engine, offering["teacher"]["user_id"]) == [
        "teaching_offering_created",
        "teaching_offering_updated",
        "teaching_offering_status_changed",
        "teaching_offering_status_changed",
    ]


def test_a_suspended_teacher_probing_an_unknown_id_gets_the_same_403(
    api_client: TestClient, offering: dict
) -> None:
    """Eligibility is decided before ownership: ids stay indistinguishable."""
    _set_verification(
        offering["engine"], offering["teacher"]["user_id"], "suspended"
    )
    header = offering["teacher_header"]

    unknown = api_client.patch(
        f"/api/v1/me/teacher/offerings/{uuid.uuid4()}",
        json={"description": "probe"},
        headers=header,
    )
    known = api_client.patch(
        f"/api/v1/me/teacher/offerings/{offering['offering_id']}",
        json={"description": "probe"},
        headers=header,
    )
    assert unknown.status_code == 403, unknown.text
    assert known.status_code == 403, known.text
    assert unknown.json()["detail"] == known.json()["detail"]
    assert "no teaching offering" not in unknown.json()["detail"]
