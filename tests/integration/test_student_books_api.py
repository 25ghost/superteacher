"""Student guided learning — integration (PostgreSQL, book P5).

End to end against ``super_teacher_db_test`` over HTTP, the exact path a
student takes through a published book:

- **the full walk**: upload → sweep → resolve designations → author one
  verified question on the extracted exercise set → designate it
  ``required`` → approve every unit → publish; then the student lists
  the book for a context, walks chapter → topic → required checkpoint
  (completion refused, a wrong attempt leaves no progress, the correct
  attempt writes progress + audit) → closing chapter → finished;
- **L6 guards over HTTP**: anonymous 401, unknown context hides books,
  unknown book / context mismatch / beyond-frontier unit all answer the
  same 404, content units refuse attempts (409), an unreached exercise
  set refuses attempts (409);
- **fresh editions**: after an administrator publishes a corrected
  revision, the student's detail reads 0 completed units for the new
  edition and the walk restarts — historical progress stays with the
  revision it was earned on.

The test database must already exist and be migrated to head 0022
(``alembic upgrade head``). Run with ``ENVIRONMENT=testing
DB_NAME=super_teacher_db_test`` so the application engine binds to the
test database.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests.conftest import build_multiline_pdf

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

BOOKS_PATH = "/api/v1/admin/books"
ME_BOOKS_PATH = "/api/v1/me/books"


# --- fixtures -----------------------------------------------------------------------


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


# --- principals & seeding ------------------------------------------------------------


def _header_for(client: TestClient, engine, *, role: str, prefix: str) -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    email = f"sb-{prefix}-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        user = User(
            email=email,
            role=role,
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        session.commit()
    finally:
        session.close()
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _admin_header(client: TestClient, engine) -> dict:
    return _header_for(client, engine, role="admin", prefix="admin")


def _student_header(client: TestClient, engine, suffix: str) -> dict:
    """A student account WITH its profile row (the service requires one)."""
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.student import Student
    from app.models.user import User

    email = f"sb-student-{suffix}-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        user = User(
            email=email,
            role="student",
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        session.flush()
        session.add(Student(user_id=user.id, full_name=f"SB Student {suffix}"))
        session.commit()
    finally:
        session.close()
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _seed_context(engine) -> uuid.UUID:
    from app.core.database import SessionLocal
    from app.models.academic_year import AcademicYear
    from app.models.education_level import EducationLevel
    from app.models.enums import AcademicYearStatus
    from app.models.learning_context import LearningContext
    from app.models.pathway import Pathway
    from app.models.subject import Subject

    session = SessionLocal(bind=engine)
    try:
        suffix = uuid.uuid4().hex[:6].upper()
        year = session.scalar(select(AcademicYear).limit(1))
        if year is None:
            year = AcademicYear(
                name="2026/2027",
                start_date=date(2026, 9, 1),
                end_date=date(2027, 6, 30),
                status=AcademicYearStatus.ACTIVE.value,
            )
            session.add(year)
            session.flush()
        pathway = Pathway(code=f"BP{suffix}", name="O Level")
        level = EducationLevel(code=f"BL{suffix}", name="Senior 3", level_number=3)
        subject = Subject(code=f"BS{suffix}", name="Mathematics")
        session.add_all([pathway, level, subject])
        session.flush()
        context = LearningContext(
            academic_year_id=year.id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            subject_id=subject.id,
            program_version_id=None,
        )
        session.add(context)
        session.commit()
        return context.id
    finally:
        session.close()


def _science_pdf() -> bytes:
    return build_multiline_pdf(
        [
            ["Chapter 1 Introduction", "Science studies the world."],
            [
                "1.1 Water Cycle",
                "Water evaporates and falls as rain.",
                "Exercises",
                "1. Solve the puzzle.",
            ],
            ["Chapter 2 Forces", "Forces push and pull."],
        ]
    )


def _upload_and_extract(client: TestClient, headers: dict, engine) -> dict:
    """Upload the science PDF and run one sweep — returns book+revision ids."""
    context_id = _seed_context(engine)
    created = client.post(
        BOOKS_PATH,
        files=[
            ("title", (None, "Integrated Science S3")),
            ("learning_context_id", (None, str(context_id))),
            ("file", ("textbook.pdf", _science_pdf(), "application/pdf")),
        ],
        headers=headers,
    )
    assert created.status_code == 201, created.text
    book_id = created.json()["book_id"]

    from app.services.book_extraction_sweeper import book_extraction_sweeper

    assert book_extraction_sweeper.sweep_once() == 1
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id::text, status FROM book_revisions WHERE book_id = :bid"
            ),
            {"bid": str(book_id)},
        ).mappings().one()
    assert row["status"] == "pending_review"
    return {"book_id": book_id, "revision_id": uuid.UUID(row["id"])}


# --- helpers --------------------------------------------------------------------------


def _detail(client, headers, book_id, revision_id) -> dict:
    response = client.get(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _unit_ids(detail: dict) -> dict[str, uuid.UUID]:
    return {unit["title"]: unit["unit_id"] for unit in detail["units"]}


def _exercise_set_id(detail: dict) -> uuid.UUID:
    sets = [u for u in detail["units"] if u["kind"] == "exercise_set"]
    assert len(sets) == 1
    return sets[0]["unit_id"]


def _resolve_designations(client, headers, book_id, revision_id, detail) -> None:
    for unit in detail["units"]:
        if unit["kind"] in ("chapter", "topic"):
            response = client.patch(
                f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit['unit_id']}",
                json={"assessment_requirement": "required"},
                headers=headers,
            )
            assert response.status_code == 200, response.text


def _approve_every_unit(client, headers, book_id, revision_id, detail) -> None:
    for unit in detail["units"]:
        response = client.post(
            f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit['unit_id']}/approve",
            headers=headers,
        )
        assert response.status_code == 200, response.text


def _set_exercise_required(engine, unit_id: uuid.UUID) -> None:
    """P3 scopes the HTTP designation field to chapter/topic, so the
    exercise set's ``required`` designation is set the way extraction's
    (deliberately disabled) classifier would emit it."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE book_units SET assessment_requirement = 'required'"
                " WHERE id = :uid"
            ),
            {"uid": str(unit_id)},
        )


def _publish(client, headers, book_id, revision_id) -> dict:
    response = client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/approve",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _post_question(client, headers, book_id, revision_id, unit_id) -> dict:
    response = client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit_id}/questions",
        json={
            "question_type": "multiple_choice",
            "prompt": "Which option is the correct answer?",
            "explanation": "Because it is marked correct.",
            "display_order": 1,
            "options": [
                {"option_text": "The right one", "display_order": 1},
                {"option_text": "A wrong one", "display_order": 2},
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _verify(client, headers, book_id, revision_id, question_id, option_id) -> None:
    response = client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/questions/{question_id}/verify",
        json={"correct_option_id": str(option_id)},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _context_id(engine, revision_id) -> uuid.UUID:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT learning_context_id::text AS context_id"
                " FROM book_revision_contexts WHERE book_revision_id = :rid"
            ),
            {"rid": str(revision_id)},
        ).mappings().one()
    return uuid.UUID(row["context_id"])


def _sql(engine, query: str, **params) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(text(query), params).mappings().all()
        return [dict(row) for row in rows]


def _progress_rows(engine) -> list[dict]:
    return _sql(
        engine,
        "SELECT student_id::text, book_revision_id::text, book_unit_id::text"
        " FROM student_book_progress ORDER BY completed_at",
    )


def _audit_events(engine, event_type: str) -> list[dict]:
    return _sql(
        engine,
        "SELECT user_id::text, metadata_json FROM auth_events"
        " WHERE event_type = :event_type",
        event_type=event_type,
    )


# --- the full walk ----------------------------------------------------------------------


def test_the_full_walk_from_listing_to_a_finished_book(api_client, clean_db):
    client, engine = api_client, clean_db
    admin = _admin_header(client, engine)
    ids = _upload_and_extract(client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]

    detail = _detail(client, admin, book_id, revision_id)
    _resolve_designations(client, admin, book_id, revision_id, detail)
    exercise_unit = _exercise_set_id(detail)
    question = _post_question(client, admin, book_id, revision_id, exercise_unit)
    correct_option = question["options"][0]
    wrong_option = question["options"][1]
    _verify(
        client,
        admin,
        book_id,
        revision_id,
        question["question_id"],
        correct_option["option_id"],
    )
    _set_exercise_required(engine, exercise_unit)
    _approve_every_unit(client, admin, book_id, revision_id, detail)
    _publish(client, admin, book_id, revision_id)

    context_id = _context_id(engine, revision_id)
    student = _student_header(client, engine, "walk")

    # 1. Listing: the book appears for its context.
    listed = client.get(
        ME_BOOKS_PATH, params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json() != []
    assert listed.json()[0]["book_id"] == str(book_id)

    # 2. Detail: 4 units, none completed (the student detail carries
    # counts — unit ids come from the administrator surface).
    body = client.get(
        f"{ME_BOOKS_PATH}/{book_id}",
        params={"learning_context_id": str(context_id)},
        headers=student,
    ).json()
    assert body["total_units"] == 4
    assert body["completed_units"] == 0
    admin_units = _unit_ids(_detail(client, admin, book_id, revision_id))
    assert set(admin_units) == {
        "Chapter 1 Introduction",
        "1.1 Water Cycle",
        "Exercises",
        "Chapter 2 Forces",
    }
    chapter1 = admin_units["Chapter 1 Introduction"]
    topic = admin_units["1.1 Water Cycle"]

    # 3. The walk starts at the first content unit.
    position = client.get(
        f"{ME_BOOKS_PATH}/{book_id}/next-unit",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert position.status_code == 200, position.text
    assert position.json()["unit"]["unit_id"] == str(chapter1)

    # 4. Complete chapter 1, then the topic — each advances one step.
    step = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{chapter1}/complete",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert step.status_code == 200, step.text
    step = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{topic}/complete",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert step.status_code == 200, step.text

    # 5. The exercise set is now the current unit — a CHECKPOINT.
    position = client.get(
        f"{ME_BOOKS_PATH}/{book_id}/next-unit",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert position.json()["unit"]["unit_id"] == str(exercise_unit)
    assert position.json()["unit"]["assessment_requirement"] == "required"
    assert position.json()["unit"]["questions"] != []

    refused = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{exercise_unit}/complete",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert refused.status_code == 409, refused.text
    assert "passing attempts" in refused.json()["detail"]

    # 6. A wrong answer: recorded attempt, NO progress, walk unmoved.
    wrong = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{exercise_unit}/attempts",
        json={
            "learning_context_id": str(context_id),
            "answers": [
                {"question_id": question["question_id"],
                 "option_id": wrong_option["option_id"]}
            ],
        },
        headers=student,
    )
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["passed"] is False
    assert wrong.json()["feedback"][0]["explanation"]
    assert len(_progress_rows(engine)) == 2  # chapter + topic only

    # 7. The correct answer completes the checkpoint.
    correct = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{exercise_unit}/attempts",
        json={
            "learning_context_id": str(context_id),
            "answers": [
                {"question_id": question["question_id"],
                 "option_id": correct_option["option_id"]}
            ],
        },
        headers=student,
    )
    assert correct.status_code == 200, correct.text
    assert correct.json()["passed"] is True
    assert correct.json()["feedback"] == []
    progress_units = {row["book_unit_id"] for row in _progress_rows(engine)}
    assert str(exercise_unit) in progress_units
    checkpoints = _audit_events(engine, "book_checkpoint_passed")
    assert len(checkpoints) == 1
    assert checkpoints[0]["user_id"]

    # 8. The closing chapter, then the book is finished.
    position = client.get(
        f"{ME_BOOKS_PATH}/{book_id}/next-unit",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert position.json()["unit"]["unit_id"] == str(
        admin_units["Chapter 2 Forces"]
    )
    step = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{admin_units['Chapter 2 Forces']}/complete",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert step.status_code == 200, step.text
    finished = client.get(
        f"{ME_BOOKS_PATH}/{book_id}/next-unit",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["finished"] is True

    body = client.get(
        f"{ME_BOOKS_PATH}/{book_id}",
        params={"learning_context_id": str(context_id)},
        headers=student,
    ).json()
    assert body["completed_units"] == 4
    completions = _audit_events(engine, "book_unit_completed")
    assert len(completions) == 3


# --- L6 guards over HTTP ----------------------------------------------------------------


def test_guards_over_http(api_client, clean_db):
    client, engine = api_client, clean_db
    admin = _admin_header(client, engine)
    ids = _upload_and_extract(client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    detail = _detail(client, admin, book_id, revision_id)
    _resolve_designations(client, admin, book_id, revision_id, detail)
    exercise_unit = _exercise_set_id(detail)
    question = _post_question(client, admin, book_id, revision_id, exercise_unit)
    _verify(
        client,
        admin,
        book_id,
        revision_id,
        question["question_id"],
        question["options"][0]["option_id"],
    )
    _set_exercise_required(engine, exercise_unit)
    _approve_every_unit(client, admin, book_id, revision_id, detail)
    _publish(client, admin, book_id, revision_id)
    context_id = _context_id(engine, revision_id)
    student = _student_header(client, engine, "guard")

    # Anonymous: 401 on every route.
    assert client.get(ME_BOOKS_PATH).status_code == 401

    # An unknown context matches nothing.
    assert client.get(
        ME_BOOKS_PATH, params={"learning_context_id": str(uuid.uuid4())},
        headers=student,
    ).json() == []

    # Unknown book and context mismatch: the same 404 family.
    unknown_book = client.get(
        f"{ME_BOOKS_PATH}/{uuid.uuid4()}",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert unknown_book.status_code == 404
    wrong_context = client.get(
        f"{ME_BOOKS_PATH}/{book_id}",
        params={"learning_context_id": str(uuid.uuid4())},
        headers=student,
    )
    assert wrong_context.status_code == 404
    assert "no book with id" in unknown_book.json()["detail"]

    units = {u["title"]: u["unit_id"] for u in _detail(
        client, admin, book_id, revision_id
    )["units"]}
    chapter1 = units["Chapter 1 Introduction"]
    topic = units["1.1 Water Cycle"]

    # Beyond-frontier reads and completions answer the same 404.
    later = client.get(
        f"{ME_BOOKS_PATH}/{book_id}/units/{topic}",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert later.status_code == 404
    assert "no book unit with id" in later.json()["detail"]
    skip = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{topic}/complete",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert skip.status_code == 409
    assert "not the current available learning unit" in skip.json()["detail"]

    # Attempts are refused on content units (409)...
    content_attempt = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{chapter1}/attempts",
        json={"learning_context_id": str(context_id), "answers": [
            {"question_id": str(uuid.uuid4()), "option_id": str(uuid.uuid4())}
        ]},
        headers=student,
    )
    assert content_attempt.status_code == 409
    assert "only an exercise_set unit" in content_attempt.json()["detail"]

    # ...and on an exercise set the walk has not reached yet (409).
    unreached = client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{exercise_unit}/attempts",
        json={"learning_context_id": str(context_id), "answers": [
            {"question_id": str(uuid.uuid4()), "option_id": str(uuid.uuid4())}
        ]},
        headers=student,
    )
    assert unreached.status_code == 409
    assert "not available yet" in unreached.json()["detail"]


# --- fresh editions ---------------------------------------------------------------------


def test_a_corrected_edition_restarts_the_walk_and_keeps_history(
    api_client, clean_db
):
    client, engine = api_client, clean_db
    admin = _admin_header(client, engine)
    ids = _upload_and_extract(client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    detail = _detail(client, admin, book_id, revision_id)
    _resolve_designations(client, admin, book_id, revision_id, detail)
    _approve_every_unit(client, admin, book_id, revision_id, detail)
    _publish(client, admin, book_id, revision_id)
    context_id = _context_id(engine, revision_id)
    student = _student_header(client, engine, "edition")

    admin_units = _unit_ids(_detail(client, admin, book_id, revision_id))
    client.post(
        f"{ME_BOOKS_PATH}/{book_id}/units/{admin_units['Chapter 1 Introduction']}/complete",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    assert len(_progress_rows(engine)) == 1

    # Publish edition 2: correction of the published revision.
    correction = client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/correct", headers=admin
    )
    assert correction.status_code == 201, correction.text
    edition2 = uuid.UUID(correction.json()["revision_id"])
    detail2 = _detail(client, admin, book_id, edition2)
    _approve_every_unit(client, admin, book_id, edition2, detail2)
    _publish(client, admin, book_id, edition2)

    # The walk restarts: 0 completed units for the new revision.
    body = client.get(
        f"{ME_BOOKS_PATH}/{book_id}",
        params={"learning_context_id": str(context_id)},
        headers=student,
    ).json()
    assert body["published_revision_id"] == str(edition2)
    assert body["completed_units"] == 0

    position = client.get(
        f"{ME_BOOKS_PATH}/{book_id}/next-unit",
        params={"learning_context_id": str(context_id)},
        headers=student,
    )
    new_units = _unit_ids(_detail(client, admin, book_id, edition2))
    assert position.json()["unit"]["unit_id"] == new_units["Chapter 1 Introduction"]

    # History stays with the revision it was earned on.
    rows = _progress_rows(engine)
    assert {row["book_revision_id"] for row in rows} == {str(revision_id)}
