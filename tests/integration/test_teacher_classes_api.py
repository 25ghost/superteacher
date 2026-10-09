"""Online classes integration tests (PostgreSQL, guarded test DB).

End-to-end contract for Phase 3 slice 3A against ``super_teacher_db_test``:

- a teacher schedules a class inside their own offering (201), reads the
  timetable, starts it, ends it — and exactly one ``auth_events`` row per
  transition is on record;
- overlapping windows of the same offering are refused (409) while
  back-to-back windows are accepted;
- a student who enrolled can list and read the class (200); a student who
  did not, and an unknown class id, both answer the SAME 404 (L6);
- the role split on every route: anonymous → 401, wrong role → 403 from
  the guard, never a handler response.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

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
                    full_name="Classroom Teacher",
                    subject="Mathematics",
                    verification_status="approved",
                )
            )
        elif role == "student":
            session.add(Student(user_id=account.id, full_name="Classroom Student"))
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


def _window(offset_hours: float = 0.0, duration_hours: float = 1.0) -> tuple[datetime, datetime]:
    """A future window (start aligned to the hour) - 'start' stays legal."""
    base = datetime.now(timezone.utc) + timedelta(days=1)
    start = base.replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset_hours)
    return start, start + timedelta(hours=duration_hours)


def _payload(start: datetime, end: datetime) -> dict:
    return {"scheduled_start_at": start.isoformat(), "scheduled_end_at": end.isoformat()}


@pytest.fixture()
def classroom(clean_db, api_client: TestClient):
    """Seeded catalog + logged-in teacher/student + one owned offering."""
    engine = clean_db
    catalog = _seed_catalog(engine)
    teacher = _seed_account(engine, role="teacher")
    student = _seed_account(engine, role="student")
    teacher_header = _header(_login(api_client, teacher))
    student_header = _header(_login(api_client, student))

    offering = api_client.post(
        "/api/v1/me/teacher/offerings", json=catalog, headers=teacher_header
    )
    assert offering.status_code == 201, offering.text
    return {
        "engine": engine,
        "teacher": teacher,
        "student": student,
        "teacher_header": teacher_header,
        "student_header": student_header,
        "offering_id": offering.json()["offering_id"],
    }


# --- lifecycle ------------------------------------------------------------------------


def test_teacher_schedules_starts_and_ends_a_class(
    api_client: TestClient, classroom: dict
) -> None:
    start, end = _window()

    created = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json=_payload(start, end),
        headers=classroom["teacher_header"],
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "scheduled"
    assert body["teaching_offering_id"] == classroom["offering_id"]
    class_id = body["class_id"]

    listed = api_client.get(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        headers=classroom["teacher_header"],
    )
    assert listed.status_code == 200, listed.text
    assert [c["class_id"] for c in listed.json()] == [class_id]

    detail = api_client.get(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}",
        headers=classroom["teacher_header"],
    )
    assert detail.status_code == 200
    assert detail.json()["class_id"] == class_id

    # A second, back-to-back window is fine; an overlapping one is 409.
    adjacent_start, adjacent_end = end, end + timedelta(hours=1)
    adjacent = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json=_payload(adjacent_start, adjacent_end),
        headers=classroom["teacher_header"],
    )
    assert adjacent.status_code == 201, adjacent.text

    overlap = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json=_payload(start + timedelta(minutes=30), end + timedelta(minutes=30)),
        headers=classroom["teacher_header"],
    )
    assert overlap.status_code == 409, overlap.text

    # SCHEDULED -> LIVE -> ENDED, each audited exactly once.
    started = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}/start",
        headers=classroom["teacher_header"],
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "live"
    assert started.json()["actual_started_at"] is not None

    again = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}/start",
        headers=classroom["teacher_header"],
    )
    assert again.status_code == 409, again.text

    ended = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}/end",
        headers=classroom["teacher_header"],
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "ended"
    assert ended.json()["actual_ended_at"] is not None

    cancel_after_end = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}/cancel",
        headers=classroom["teacher_header"],
    )
    assert cancel_after_end.status_code == 409, cancel_after_end.text

    session = _session(classroom["engine"])
    try:
        from app.models.auth_event import AuthEvent

        events = session.scalars(
            select(AuthEvent).where(
                AuthEvent.user_id == uuid.UUID(classroom["teacher"]["user_id"])
            )
        ).all()
        class_events = [
            e.event_type for e in events if e.event_type.startswith("online_class")
        ]
    finally:
        session.close()
    # One row per transition of the first class, plus the adjacent create.
    assert class_events.count("online_class_created") == 2
    assert class_events.count("online_class_started") == 1
    assert class_events.count("online_class_ended") == 1


def test_cancel_drops_a_scheduled_class_and_frees_the_window(
    api_client: TestClient, classroom: dict
) -> None:
    start, end = _window(6)
    created = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json=_payload(start, end),
        headers=classroom["teacher_header"],
    )
    assert created.status_code == 201, created.text
    class_id = created.json()["class_id"]

    cancelled = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}/cancel",
        headers=classroom["teacher_header"],
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    # The window is free again for a replacement class.
    replacement = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json=_payload(start, end),
        headers=classroom["teacher_header"],
    )
    assert replacement.status_code == 201, replacement.text

    # ...and the cancelled class can never be revived.
    start_again = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}/start",
        headers=classroom["teacher_header"],
    )
    assert start_again.status_code == 409, start_again.text


# --- student surface ------------------------------------------------------------------


def test_enrolled_student_reads_the_class_everyone_else_cannot(
    api_client: TestClient, classroom: dict
) -> None:
    start, end = _window(12)
    created = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json=_payload(start, end),
        headers=classroom["teacher_header"],
    )
    assert created.status_code == 201, created.text
    class_id = created.json()["class_id"]

    # Enrollment is the whole visibility chain: marketplace join first.
    enrolled = api_client.post(
        f"/api/v1/marketplace/offerings/{classroom['offering_id']}/enroll",
        headers=classroom["student_header"],
    )
    assert enrolled.status_code == 201, enrolled.text

    listed = api_client.get("/api/v1/me/classes", headers=classroom["student_header"])
    assert listed.status_code == 200, listed.text
    assert [c["class_id"] for c in listed.json()] == [class_id]

    detail = api_client.get(
        f"/api/v1/me/classes/{class_id}", headers=classroom["student_header"]
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "scheduled"

    # Known id vs unknown id: the SAME 404, so ids never confirm anything.
    for target in (uuid.uuid4(), "00000000-0000-0000-0000-000000000000"):
        foreign = api_client.get(
            f"/api/v1/me/classes/{target}", headers=classroom["student_header"]
        )
        assert foreign.status_code == 404, foreign.text


def test_a_student_without_an_enrollment_sees_no_classes(
    api_client: TestClient, classroom: dict
) -> None:
    start, end = _window(18)
    created = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json=_payload(start, end),
        headers=classroom["teacher_header"],
    )
    assert created.status_code == 201, created.text
    class_id = created.json()["class_id"]

    listed = api_client.get("/api/v1/me/classes", headers=classroom["student_header"])
    assert listed.status_code == 200, listed.text
    assert listed.json() == []

    # The class exists - and still answers 404 through this student's path.
    detail = api_client.get(
        f"/api/v1/me/classes/{class_id}", headers=classroom["student_header"]
    )
    assert detail.status_code == 404, detail.text


# --- role guards ----------------------------------------------------------------------


def test_role_split_on_every_class_route(
    api_client: TestClient, classroom: dict
) -> None:
    offering_id = classroom["offering_id"]
    teacher_base = f"/api/v1/me/teacher/offerings/{offering_id}/classes"
    unknown = str(uuid.uuid4())
    body = _payload(*_window(24))

    # Anonymous: 401 on every route (no handler runs).
    assert api_client.post(teacher_base, json=body).status_code == 401
    assert api_client.get(teacher_base).status_code == 401
    assert api_client.get(f"/api/v1/me/classes").status_code == 401

    # A student on the teacher surface: 403 from require_teacher.
    assert (
        api_client.post(teacher_base, json=body, headers=classroom["student_header"]).status_code
        == 403
    )
    assert (
        api_client.post(
            f"{teacher_base}/{unknown}/start", headers=classroom["student_header"]
        ).status_code
        == 403
    )
    assert (
        api_client.patch(
            f"{teacher_base}/{unknown}", json=body, headers=classroom["student_header"]
        ).status_code
        == 403
    )

    # A teacher on the student surface: 403 from get_current_student.
    assert (
        api_client.get("/api/v1/me/classes", headers=classroom["teacher_header"]).status_code
        == 403
    )
    assert (
        api_client.get(
            f"/api/v1/me/classes/{unknown}", headers=classroom["teacher_header"]
        ).status_code
        == 403
    )

    # The teacher's own routes still answer normally (404 for the unknown id).
    own = api_client.get(f"{teacher_base}/{unknown}", headers=classroom["teacher_header"])
    assert own.status_code == 404, own.text


def test_a_foreign_offering_is_the_same_404_as_an_unknown_one(
    api_client: TestClient, classroom: dict
) -> None:
    other = _seed_account(classroom["engine"], role="teacher")
    other_header = _header(_login(api_client, other))
    other_offering = api_client.post(
        "/api/v1/me/teacher/offerings",
        json=_seed_catalog(classroom["engine"]),
        headers=other_header,
    )
    assert other_offering.status_code == 201, other_offering.text
    foreign_id = other_offering.json()["offering_id"]

    for target in (foreign_id, str(uuid.uuid4())):
        response = api_client.get(
            f"/api/v1/me/teacher/offerings/{target}/classes",
            headers=classroom["teacher_header"],
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"].startswith("no teaching offering with id ")
