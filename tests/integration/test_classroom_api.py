"""Classroom integration tests (PostgreSQL, guarded test DB).

End-to-end contract for Phase 3 slice 3B against ``super_teacher_db_test``:

- the role split on every classroom route: anonymous → 401, wrong role →
  403 from the guard, never a handler response; invalid cursors → 422;
- the teacher's participant roster and derived attendance for an owned
  class (200), 409 for attendance before the class started, empty roster
  before anyone joins, and a foreign class id answering the SAME 404 as
  an unknown one (L6);
- the teacher's transcript of an OWNED ENDED class: full history without
  needing to have attended, 409 while scheduled/live/cancelled, foreign
  class ids sharing the unknown-id 404;
- the student's own attendance (200, derived) and the ENDED-class
  transcript (cursor pages, exclusive ``after_sequence``, senders as role
  + display name — never an email address), with prior participation
  required (same 404 as an unknown id for someone who was never inside)
  and that participation surviving an enrollment leave (§14);
- participation itself is driven through the service with
  ``SessionLocal``: the HTTP join arrives with the WebSocket transport in
  slice 3C, so 3B proves the primitives behind the read endpoints.
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


def _teacher_urls(classroom: dict, class_id: str) -> dict[str, str]:
    base = f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}"
    return {
        "participants": f"{base}/participants",
        "attendance": f"{base}/attendance",
        "transcript": f"{base}/transcript",
    }


def _create_class(api_client: TestClient, classroom: dict, offset: float = 0.0) -> str:
    start, end = _window(offset)
    created = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes",
        json={"scheduled_start_at": start.isoformat(), "scheduled_end_at": end.isoformat()},
        headers=classroom["teacher_header"],
    )
    assert created.status_code == 201, created.text
    return created.json()["class_id"]


def _start_class(api_client: TestClient, classroom: dict, class_id: str) -> None:
    started = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}/start",
        headers=classroom["teacher_header"],
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "live"


def _end_class(api_client: TestClient, classroom: dict, class_id: str) -> None:
    ended = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/{class_id}/end",
        headers=classroom["teacher_header"],
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "ended"


def _enroll(api_client: TestClient, classroom: dict) -> None:
    enrolled = api_client.post(
        f"/api/v1/marketplace/offerings/{classroom['offering_id']}/enroll",
        headers=classroom["student_header"],
    )
    assert enrolled.status_code == 201, enrolled.text


def _student_profile_id(engine, user_id: str):
    from app.models.student import Student

    session = _session(engine)
    try:
        return session.scalar(select(Student.id).where(Student.user_id == uuid.UUID(user_id)))
    finally:
        session.close()


def _join_via_service(engine, user_id: str, class_id: str, connection_id: str) -> None:
    """Open a participation segment through the service (transport is 3C)."""
    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services import classroom_service

    session = SessionLocal(bind=engine)
    try:
        user = session.get(User, uuid.UUID(user_id))
        classroom_service.join_class(session, user, uuid.UUID(class_id), connection_id)
        session.commit()
    finally:
        session.close()


def _seed_message(engine, class_id: str, sender_user_id: str, sequence: int, body: str) -> None:
    from app.models.class_message import ClassMessage

    session = _session(engine)
    try:
        session.add(
            ClassMessage(
                class_session_id=uuid.UUID(class_id),
                sender_user_id=uuid.UUID(sender_user_id),
                client_message_id=f"seed-{sequence}",
                body=body,
                sequence=sequence,
            )
        )
        session.commit()
    finally:
        session.close()


# --- role guards ----------------------------------------------------------------------


def test_role_split_on_every_classroom_route(
    api_client: TestClient, classroom: dict
) -> None:
    class_id = _create_class(api_client, classroom)
    _start_class(api_client, classroom, class_id)
    teacher_urls = _teacher_urls(classroom, class_id)
    student_attendance = f"/api/v1/me/classes/{class_id}/attendance"
    transcript = f"/api/v1/me/classes/{class_id}/transcript"
    unknown = str(uuid.uuid4())

    # Anonymous: 401 on every route (no handler runs).
    for url in (*teacher_urls.values(), student_attendance, transcript):
        assert api_client.get(url).status_code == 401

    # A student on the teacher surface: 403 from require_teacher.
    for url in teacher_urls.values():
        assert api_client.get(url, headers=classroom["student_header"]).status_code == 403

    # A teacher on the student surface: 403 from get_current_student.
    for url in (student_attendance, transcript):
        assert api_client.get(url, headers=classroom["teacher_header"]).status_code == 403

    # Own routes, unknown id: 404 (a classroom id never confirms anything).
    for suffix in ("participants", "attendance", "transcript"):
        url = (
            f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/"
            f"{unknown}/{suffix}"
        )
        response = api_client.get(url, headers=classroom["teacher_header"])
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == f"no online class with id {unknown}"

    # Transcript cursor bounds are schema-level: 422, never a handler.
    assert (
        api_client.get(
            f"{transcript}?after_sequence=-1", headers=classroom["student_header"]
        ).status_code
        == 422
    )
    assert (
        api_client.get(f"{transcript}?limit=0", headers=classroom["student_header"]).status_code
        == 422
    )


# --- teacher surface ------------------------------------------------------------------


def test_teacher_reads_participants_and_derived_attendance(
    api_client: TestClient, classroom: dict
) -> None:
    _enroll(api_client, classroom)
    class_id = _create_class(api_client, classroom)
    teacher_urls = _teacher_urls(classroom, class_id)

    # A scheduled class: empty roster is a fact, attendance is a 409.
    participants = api_client.get(
        teacher_urls["participants"], headers=classroom["teacher_header"]
    )
    assert participants.status_code == 200, participants.text
    assert participants.json() == []

    too_early = api_client.get(
        teacher_urls["attendance"], headers=classroom["teacher_header"]
    )
    assert too_early.status_code == 409, too_early.text
    assert (
        "attendance is only available once the class has started"
        in too_early.json()["detail"]
    )

    _start_class(api_client, classroom, class_id)

    # The roster is the offering's ACTIVE enrollments: one row, zero seconds.
    attendance = api_client.get(
        teacher_urls["attendance"], headers=classroom["teacher_header"]
    )
    assert attendance.status_code == 200, attendance.text
    rows = attendance.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["student_id"] == str(
        _student_profile_id(classroom["engine"], classroom["student"]["user_id"])
    )
    assert row["full_name"] == "Classroom Student"
    assert row["online"] is False
    assert row["cumulative_seconds"] == 0
    assert row["attendance_status"] == "not_attended"
    assert row["segments"] == []
    assert row["actual_seconds"] >= 0  # live: the class keeps accumulating

    # One student joins through the service: roster gains a live body.
    _join_via_service(
        classroom["engine"], classroom["student"]["user_id"], class_id, "conn-int-1"
    )
    participants = api_client.get(
        teacher_urls["participants"], headers=classroom["teacher_header"]
    )
    assert participants.status_code == 200, participants.text
    roster = participants.json()
    assert len(roster) == 1
    assert roster[0]["full_name"] == "Classroom Student"
    assert roster[0]["online"] is True
    assert roster[0]["segment_count"] == 1
    assert roster[0]["first_joined_at"] is not None
    assert roster[0]["last_seen_at"] is not None

    attendance = api_client.get(
        teacher_urls["attendance"], headers=classroom["teacher_header"]
    )
    assert attendance.json()[0]["online"] is True  # still connected
    assert len(attendance.json()[0]["segments"]) == 1


def test_a_foreign_class_id_is_the_same_404_as_an_unknown_one(
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
    other_class = api_client.post(
        f"/api/v1/me/teacher/offerings/{other_offering.json()['offering_id']}/classes",
        json={
            "scheduled_start_at": _window(12)[0].isoformat(),
            "scheduled_end_at": _window(12)[1].isoformat(),
        },
        headers=other_header,
    )
    assert other_class.status_code == 201, other_class.text

    for target in (other_class.json()["class_id"], str(uuid.uuid4())):
        for suffix in ("participants", "attendance", "transcript"):
            url = (
                f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/"
                f"{target}/{suffix}"
            )
            response = api_client.get(url, headers=classroom["teacher_header"])
            assert response.status_code == 404, response.text
            assert response.json()["detail"] == f"no online class with id {target}"


# --- student surface ------------------------------------------------------------------


def test_student_reads_their_own_attendance_and_transcript(
    api_client: TestClient, classroom: dict
) -> None:
    _enroll(api_client, classroom)
    class_id = _create_class(api_client, classroom)
    scheduled_id = _create_class(api_client, classroom, offset=6)
    _start_class(api_client, classroom, class_id)
    unknown = str(uuid.uuid4())

    # Own attendance on a live class: a derived row, identity from the token.
    attendance = api_client.get(
        f"/api/v1/me/classes/{class_id}/attendance", headers=classroom["student_header"]
    )
    assert attendance.status_code == 200, attendance.text
    row = attendance.json()
    assert row["student_id"] == str(
        _student_profile_id(classroom["engine"], classroom["student"]["user_id"])
    )
    assert row["full_name"] == "Classroom Student"
    assert row["cumulative_seconds"] == 0
    assert row["attendance_status"] == "not_attended"
    assert row["segments"] == []
    assert "email" not in attendance.text

    # Before the start there is no verdict to derive: 409, both surfaces.
    too_early = api_client.get(
        f"/api/v1/me/classes/{scheduled_id}/attendance",
        headers=classroom["student_header"],
    )
    assert too_early.status_code == 409, too_early.text
    assert (
        "attendance is only available once the class has started"
        in too_early.json()["detail"]
    )

    # Attendance needs enrollment OR participation; unknown id → 404.
    missing = api_client.get(
        f"/api/v1/me/classes/{unknown}/attendance",
        headers=classroom["student_header"],
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == f"no online class with id {unknown}"

    # Transcript requires having BEEN inside: enrolled-but-absent == unknown.
    for target in (class_id, unknown):
        response = api_client.get(
            f"/api/v1/me/classes/{target}/transcript",
            headers=classroom["student_header"],
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == f"no online class with id {target}"

    # Participate, then seed two durable messages (the writer arrives in 3D).
    _join_via_service(
        classroom["engine"], classroom["student"]["user_id"], class_id, "conn-int-1"
    )
    _seed_message(classroom["engine"], class_id, classroom["student"]["user_id"], 1, "hello")
    _seed_message(classroom["engine"], class_id, classroom["teacher"]["user_id"], 2, "welcome")

    # LIVE: participation exists but the transcript is historical only —
    # the live classroom reads through the WebSocket (3C/3D).
    live_refusal = api_client.get(
        f"/api/v1/me/classes/{class_id}/transcript",
        headers=classroom["student_header"],
    )
    assert live_refusal.status_code == 409, live_refusal.text
    assert "only available once the class has ended" in live_refusal.json()["detail"]
    assert "status is 'live'" in live_refusal.json()["detail"]

    _end_class(api_client, classroom, class_id)

    transcript = api_client.get(
        f"/api/v1/me/classes/{class_id}/transcript",
        headers=classroom["student_header"],
    )
    assert transcript.status_code == 200, transcript.text
    messages = transcript.json()
    assert [m["sequence"] for m in messages] == [1, 2]
    assert messages[0]["body"] == "hello"
    assert messages[0]["sender"] == {"role": "student", "display_name": "Classroom Student"}
    assert messages[1]["sender"] == {"role": "teacher", "display_name": "Classroom Teacher"}
    assert "@test.example" not in transcript.text  # names only, never an address
    # Exactly the six presentation-safe fields — never the dedupe key.
    assert set(messages[0]) == {"message_id", "sequence", "sender", "body", "sent_at"}

    # The cursor is EXCLUSIVE — replay never duplicates a row.
    page = api_client.get(
        f"/api/v1/me/classes/{class_id}/transcript?after_sequence=1",
        headers=classroom["student_header"],
    )
    assert [m["sequence"] for m in page.json()] == [2]
    head = api_client.get(
        f"/api/v1/me/classes/{class_id}/transcript?limit=1",
        headers=classroom["student_header"],
    )
    assert [m["sequence"] for m in head.json()] == [1]

    # A student who never enrolled reaches nothing through this surface either.
    outsider = _seed_account(classroom["engine"], role="student")
    outsider_header = _header(_login(api_client, outsider))
    for target in (class_id, unknown):
        response = api_client.get(
            f"/api/v1/me/classes/{target}/attendance", headers=outsider_header
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == f"no online class with id {target}"


# --- teacher transcript ----------------------------------------------------------------


def _teacher_transcript_url(classroom: dict, class_id: str) -> str:
    return (
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/"
        f"{class_id}/transcript"
    )


def test_teacher_reads_the_full_transcript_only_after_the_class_ends(
    api_client: TestClient, classroom: dict
) -> None:
    """§12 matrix: owning teacher reads an ENDED class's full history."""
    _enroll(api_client, classroom)
    header = classroom["teacher_header"]
    expected = "only available once the class has ended"

    # SCHEDULED: no transcript before anything has happened.
    scheduled_id = _create_class(api_client, classroom, offset=0)
    response = api_client.get(_teacher_transcript_url(classroom, scheduled_id), headers=header)
    assert response.status_code == 409, response.text
    assert expected in response.json()["detail"]
    assert "status is 'scheduled'" in response.json()["detail"]

    # CANCELLED: a cancelled class never becomes historical.
    cancelled_id = _create_class(api_client, classroom, offset=6)
    cancelled = api_client.post(
        f"/api/v1/me/teacher/offerings/{classroom['offering_id']}/classes/"
        f"{cancelled_id}/cancel",
        headers=header,
    )
    assert cancelled.status_code == 200, cancelled.text
    response = api_client.get(_teacher_transcript_url(classroom, cancelled_id), headers=header)
    assert response.status_code == 409, response.text
    assert "status is 'cancelled'" in response.json()["detail"]

    # LIVE: participation and messages exist, but the live classroom reads
    # through the WebSocket (3C/3D) — this endpoint is historical only.
    live_id = _create_class(api_client, classroom, offset=3)
    _start_class(api_client, classroom, live_id)
    _join_via_service(
        classroom["engine"], classroom["student"]["user_id"], live_id, "conn-int-t1"
    )
    _seed_message(classroom["engine"], live_id, classroom["student"]["user_id"], 1, "hello")
    _seed_message(classroom["engine"], live_id, classroom["teacher"]["user_id"], 2, "welcome")
    response = api_client.get(_teacher_transcript_url(classroom, live_id), headers=header)
    assert response.status_code == 409, response.text
    assert "status is 'live'" in response.json()["detail"]

    # ENDED: the owning teacher reads the whole transcript — they never
    # attended a session themselves; offering ownership is the entitlement.
    _end_class(api_client, classroom, live_id)
    transcript = api_client.get(_teacher_transcript_url(classroom, live_id), headers=header)
    assert transcript.status_code == 200, transcript.text
    messages = transcript.json()
    assert [m["sequence"] for m in messages] == [1, 2]
    assert messages[0]["sender"] == {"role": "student", "display_name": "Classroom Student"}
    assert messages[1]["sender"] == {"role": "teacher", "display_name": "Classroom Teacher"}
    assert "@test.example" not in transcript.text
    assert set(messages[0]) == {"message_id", "sequence", "sender", "body", "sent_at"}

    # The exclusive cursor pages the same way the student's copy does.
    page = api_client.get(
        f"{_teacher_transcript_url(classroom, live_id)}?after_sequence=1", headers=header
    )
    assert [m["sequence"] for m in page.json()] == [2]

    # An unknown class id under my own offering: SAME 404 as ever.
    unknown = str(uuid.uuid4())
    response = api_client.get(_teacher_transcript_url(classroom, unknown), headers=header)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"no online class with id {unknown}"


def test_historical_transcript_access_survives_leaving_the_enrollment(
    api_client: TestClient, classroom: dict
) -> None:
    """§14: past participation preserves transcript access after leaving."""
    _enroll(api_client, classroom)
    student_header = classroom["student_header"]
    class_id = _create_class(api_client, classroom, offset=0)
    _start_class(api_client, classroom, class_id)
    _join_via_service(
        classroom["engine"], classroom["student"]["user_id"], class_id, "conn-int-14"
    )
    _seed_message(classroom["engine"], class_id, classroom["student"]["user_id"], 1, "I was here")
    _end_class(api_client, classroom, class_id)

    # Leave the enrollment while the class sits ENDED.
    enrollments = api_client.get("/api/v1/me/learning-enrollments", headers=student_header)
    assert enrollments.status_code == 200, enrollments.text
    active = [row for row in enrollments.json() if row["status"] == "active"]
    assert len(active) == 1
    left = api_client.post(
        f"/api/v1/me/learning-enrollments/{active[0]['enrollment_id']}/leave",
        headers=student_header,
    )
    assert left.status_code == 200, left.text

    # The class they attended stays readable — participation is history.
    transcript = api_client.get(
        f"/api/v1/me/classes/{class_id}/transcript", headers=student_header
    )
    assert transcript.status_code == 200, transcript.text
    assert [m["sequence"] for m in transcript.json()] == [1]

    # A later ENDED class of the SAME offering they never joined: SAME 404
    # as an unknown id — enrollment membership alone never grants history.
    later = _create_class(api_client, classroom, offset=6)
    _start_class(api_client, classroom, later)
    _end_class(api_client, classroom, later)
    response = api_client.get(
        f"/api/v1/me/classes/{later}/transcript", headers=student_header
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"no online class with id {later}"
