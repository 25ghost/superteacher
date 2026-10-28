"""Objective assessment over HTTP — integration (PostgreSQL, book P4).

End to end against ``super_teacher_db_test``, the whole administrator
surface added by P4 (no student routes exist yet — those are P5):

- **the full assessment loop**: upload → sweep → author multiple-choice
  and true/false questions → every validation refusal is 422 and writes
  nothing → delete/correct → explicit answer-key verification (with a
  re-verification that MOVES the key) → per-set passing threshold →
  review → publish; durable state and audit events re-read from FRESH
  sessions, and no authored text ever reaches the audit trail;
- **role guards**: teacher, student and anonymous tokens are refused
  on all six routes (403/401) with nothing written;
- **id contract**: unknown and foreign ids answer the SAME non-disclosing
  404 across the surface;
- **publication gates**: an unverified answer key and a required
  exercise set with no questions each refuse publication (422) until
  fixed — then the same revision publishes;
- **lifecycle**: published and rejected revisions refuse every question
  mutation (409) while the read surface keeps working;
- **correction editions clone questions with DEAD keys** (fresh ids,
  unverified, no correctness) — the source stays untouched, the clone
  cannot publish until re-verified, and the threshold rides along;
- **real row locks**: verification vs publication serializes on the
  revision row (never published-without-a-key, never a 500), and two
  concurrent attempt recordings persist two immutable attempts through
  separate sessions. The attempt race drives the service directly,
  exactly as the P5 endpoint will.

Run with ``ENVIRONMENT=testing DB_NAME=super_teacher_db_test pytest -m
integration --no-cov`` against a database migrated to head 0021.
"""
from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import build_multiline_pdf
from tests.integration.test_admin_books_review import (
    _ADMIN_DETAIL,
    _admin_header,
    _approve_all_units_by_sql,
    _approve_every_unit,
    _audit_events,
    _current_publications,
    _detail,
    _latest_admin_id,
    _resolve_designations,
    _revision_row,
    _student_header,
    _teacher_header,
    _unit_ids,
    _upload_and_extract,
    BOOKS_PATH,
)

pytestmark = pytest.mark.integration

Q1_PROMPT = "Photosynthesis needs light energy"
Q1_EXPLANATION = "Chlorophyll absorbs red and blue light"
Q1_OPTIONS = (
    "chlorophyll absorbs light",
    "mitochondria absorb light",
    "ribosomes absorb light",
    "cellulose absorbs light",
)
Q2_PROMPT = "Water boils at 100 degrees Celsius"
Q2_EXPLANATION = "At standard atmospheric pressure water boils at 100 degrees"
Q2_OPTIONS = ("boils at 100 degrees", "freezes at 100 degrees")
Q3_PROMPT = "The nucleus controls the cell"
Q3_EXPLANATION = "The nucleus stores the cell DNA"
Q3_OPTIONS = ("controls the cell", "provides energy")

ALL_AUTHORED_TEXT = (
    Q1_PROMPT,
    Q1_EXPLANATION,
    *Q1_OPTIONS,
    Q2_PROMPT,
    Q2_EXPLANATION,
    *Q2_OPTIONS,
    Q3_PROMPT,
    Q3_EXPLANATION,
    *Q3_OPTIONS,
)


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


# --- payload builders ----------------------------------------------------------------


def _mc(order: int, prompt: str, explanation: str, texts: tuple[str, ...]) -> dict:
    return {
        "question_type": "multiple_choice",
        "prompt": prompt,
        "explanation": explanation,
        "display_order": order,
        "options": [
            {"option_text": text, "display_order": index}
            for index, text in enumerate(texts, start=1)
        ],
    }


def _tf(order: int, prompt: str, explanation: str, texts: tuple[str, ...]) -> dict:
    return {
        "question_type": "true_false",
        "prompt": prompt,
        "explanation": explanation,
        "display_order": order,
        "options": [
            {"option_text": text, "display_order": index}
            for index, text in enumerate(texts, start=1)
        ],
    }


def _q_path(book_id, revision_id, unit_id=None, question_id=None) -> str:
    base = f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}"
    if unit_id is not None:
        return f"{base}/units/{unit_id}/questions"
    return f"{base}/questions/{question_id}"


# --- HTTP helpers --------------------------------------------------------------------


def _post_question(client, headers, book_id, revision_id, unit_id, payload) -> dict:
    response = client.post(
        _q_path(book_id, revision_id, unit_id=unit_id),
        json=payload,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _get_questions(client, headers, book_id, revision_id, unit_id) -> list[dict]:
    response = client.get(
        _q_path(book_id, revision_id, unit_id=unit_id), headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _verify(
    client, headers, book_id, revision_id, question_id, option_id
) -> dict:
    response = client.post(
        f"{_q_path(book_id, revision_id, question_id=question_id)}/verify",
        json={"correct_option_id": str(option_id)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _exercise_id(client, headers, book_id, revision_id) -> uuid.UUID:
    units = _unit_ids(_detail(client, headers, book_id, revision_id))
    assert "Exercises" in units
    return units["Exercises"]


def _publish(client, headers, book_id, revision_id) -> dict:
    response = client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/approve",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approve_exercise(client, headers, book_id, revision_id, exercise_id) -> None:
    response = client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{exercise_id}/approve",
        headers=headers,
    )
    assert response.status_code == 200, response.text


# --- fresh-session SQL readers ----------------------------------------------------------


def _sql(engine, query: str, **params) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(text(query), params).mappings().all()
        return [dict(row) for row in rows]


def _question_rows(engine, revision_id) -> list[dict]:
    return _sql(
        engine,
        "SELECT id::text, book_unit_id::text, question_type, prompt,"
        "       display_order, answer_key_verified_by_user_id::text AS verifier,"
        "       answer_key_verified_at"
        " FROM book_questions WHERE book_revision_id = :rid"
        " ORDER BY display_order",
        rid=str(revision_id),
    )


def _option_rows(engine, revision_id) -> list[dict]:
    return _sql(
        engine,
        "SELECT o.id::text, o.book_question_id::text, o.option_text,"
        "       o.display_order, o.is_correct"
        " FROM book_question_options o"
        " JOIN book_questions q ON q.id = o.book_question_id"
        " WHERE q.book_revision_id = :rid"
        " ORDER BY q.display_order, o.display_order",
        rid=str(revision_id),
    )


def _unit_row(engine, unit_id) -> dict:
    rows = _sql(
        engine,
        "SELECT kind, review_status, assessment_requirement,"
        "       passing_threshold_percent"
        " FROM book_units WHERE id = :uid",
        uid=str(unit_id),
    )
    assert len(rows) == 1
    return rows[0]


def _attempt_rows(engine) -> list[dict]:
    return _sql(
        engine,
        "SELECT id::text, student_id::text, book_revision_id::text,"
        "       book_unit_id::text, total_questions, correct_answers, passed"
        " FROM book_attempts ORDER BY created_at, id",
    )


def _answer_rows(engine) -> list[dict]:
    return _sql(
        engine,
        "SELECT book_attempt_id::text, book_question_id::text,"
        "       selected_option_id::text, is_correct"
        " FROM book_attempt_answers",
    )


def _context_id(engine, revision_id) -> uuid.UUID:
    rows = _sql(
        engine,
        "SELECT learning_context_id::text AS context_id"
        " FROM book_revision_contexts WHERE book_revision_id = :rid",
        rid=str(revision_id),
    )
    assert len(rows) == 1
    return uuid.UUID(rows[0]["context_id"])


def _audit_blob(engine) -> str:
    rows = _sql(engine, "SELECT metadata_json FROM auth_events")
    return "\n".join(row["metadata_json"] or "" for row in rows)


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


# --- principals for direct service calls ------------------------------------------------


def _create_student(engine) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns ``(user_id, student_row_id)`` — attempts store the
    ``students.id`` foreign key, audit events store the user id."""
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.student import Student
    from app.models.user import User

    session = SessionLocal(bind=engine)
    try:
        user = User(
            email=f"attempt-{uuid.uuid4().hex[:8]}@test.example",
            role="student",
            status="active",
            password_hash=hash_password("correct horse battery staple"),
        )
        session.add(user)
        session.flush()
        student = Student(user_id=user.id, full_name="Race Student")
        session.add(student)
        session.commit()
        return user.id, student.id
    finally:
        session.close()


def _verify_in_a_thread(
    engine, book_id, revision_id, question_id, option_id, admin_id
) -> str:
    from app.core.database import SessionLocal
    from app.schemas.book_assessment import BookQuestionVerifyRequest
    from app.services.book_question_service import verify_question
    from app.services.learning_context_service import (
        LearningConflictError,
        LearningNotFoundError,
    )

    session = SessionLocal(bind=engine)
    actor = type("Actor", (), {"id": admin_id, "role": "admin"})()
    try:
        verify_question(
            session,
            actor,
            book_id=book_id,
            revision_id=revision_id,
            question_id=question_id,
            payload=BookQuestionVerifyRequest(correct_option_id=option_id),
        )
        session.commit()
        return "verified"
    except LearningConflictError:
        session.rollback()
        return "conflict"
    except LearningNotFoundError:
        session.rollback()
        return "missing"
    finally:
        session.close()


def _publish_in_a_thread(engine, book_id, revision_id, admin_id) -> str:
    from app.core.database import SessionLocal
    from app.services.book_review_service import approve_revision
    from app.services.learning_context_service import (
        LearningConflictError,
        LearningValidationError,
    )

    session = SessionLocal(bind=engine)
    actor = type("Actor", (), {"id": admin_id, "role": "admin"})()
    try:
        approve_revision(session, actor, book_id=book_id, revision_id=revision_id)
        session.commit()
        return "published"
    except LearningConflictError:
        session.rollback()
        return "conflict"
    except LearningValidationError:
        session.rollback()
        return "refused"
    finally:
        session.close()


def _attempt_in_a_thread(
    engine, user_id, payload, book_id, revision_id, unit_id
) -> str:
    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services.book_assessment_service import record_attempt

    session = SessionLocal(bind=engine)
    try:
        user = session.get(User, user_id)
        read = record_attempt(
            session,
            user,
            book_id=book_id,
            revision_id=revision_id,
            unit_id=unit_id,
            payload=payload,
        )
        session.commit()
        return str(read.attempt_id)
    except Exception as exc:  # noqa: BLE001 — the assertion reports why
        session.rollback()
        return f"failed: {type(exc).__name__}: {exc}"
    finally:
        session.close()


def _in_two_threads(first, second) -> list:
    barrier = Barrier(2)

    def _run(fn, *args):
        barrier.wait(timeout=10)
        return fn(*args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_run, fn, *args)
            for fn, args in ((first[0], first[1]), (second[0], second[1]))
        ]
        return [future.result(timeout=30) for future in futures]


# --- the full assessment loop -------------------------------------------------------------


def test_the_full_assessment_loop_authors_verifies_configures_and_publishes_over_http(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    units = _unit_ids(_detail(api_client, admin, book_id, revision_id))
    exercise_id, chapter_id = units["Exercises"], units["Chapter 1 Introduction"]

    # 1. Nothing authored yet.
    assert _get_questions(api_client, admin, book_id, revision_id, exercise_id) == []

    # 2. Author three questions (two kinds).
    first = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
    )
    second = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _tf(2, Q2_PROMPT, Q2_EXPLANATION, Q2_OPTIONS),
    )
    _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(3, Q3_PROMPT, Q3_EXPLANATION, Q3_OPTIONS),
    )
    for created in (first, second):
        assert created["answer_key_verified_at"] is None
        assert all(option["is_correct"] is False for option in created["options"])

    # 3. Every refusal is 422 and writes nothing.
    refusals = [
        (
            _tf(4, Q2_PROMPT, Q2_EXPLANATION, ("a", "b", "c")),
            "exactly two options",
        ),
        (_mc(4, "   ", Q1_EXPLANATION, Q1_OPTIONS), "cannot be blank"),
        (
            _mc(1, "duplicate order", Q1_EXPLANATION, Q1_OPTIONS),
            "already exists in this exercise set",
        ),
    ]
    for payload, fragment in refusals:
        response = api_client.post(
            _q_path(book_id, revision_id, unit_id=exercise_id),
            json=payload,
            headers=admin,
        )
        assert response.status_code == 422, response.text
        assert fragment in response.json()["detail"]
    off_set = api_client.post(
        _q_path(book_id, revision_id, unit_id=chapter_id),
        json=_mc(1, "on a chapter", Q1_EXPLANATION, Q1_OPTIONS),
        headers=admin,
    )
    assert off_set.status_code == 422
    assert "exercise_set unit" in off_set.json()["detail"]
    threshold_off_set = api_client.patch(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{chapter_id}/threshold",
        json={"passing_threshold_percent": 60},
        headers=admin,
    )
    assert threshold_off_set.status_code == 422
    assert _get_questions(api_client, admin, book_id, revision_id, chapter_id) == []
    assert len(_question_rows(engine, revision_id)) == 3

    # 4. Deletion removes the third question and audits once.
    third_id = _q3_id(api_client, admin, book_id, revision_id, exercise_id)
    deleted = api_client.delete(
        _q_path(book_id, revision_id, question_id=third_id),
        headers=admin,
    )
    assert deleted.status_code == 204, deleted.text
    remaining = _get_questions(api_client, admin, book_id, revision_id, exercise_id)
    assert [question["prompt"] for question in remaining] == [
        Q1_PROMPT,
        Q2_PROMPT,
    ]
    assert len(_audit_events(engine, "book_question_deleted")) == 1

    # 5. Correction rewrites the prompt and kills any key state.
    corrected = api_client.patch(
        _q_path(book_id, revision_id, question_id=first["question_id"]),
        json={"prompt": "  Why do leaves turn yellow?  "},
        headers=admin,
    )
    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["prompt"] == "Why do leaves turn yellow?"
    assert body["answer_key_verified_at"] is None
    assert all(option["is_correct"] is False for option in body["options"])

    # 6. Verification: explicit, recorded, and MOVES on re-verification.
    verified = _verify(
        api_client,
        admin,
        book_id,
        revision_id,
        first["question_id"],
        first["options"][1]["option_id"],
    )
    keyed = [option for option in verified["options"] if option["is_correct"]]
    assert [option["option_id"] for option in keyed] == [
        first["options"][1]["option_id"]
    ]
    assert verified["answer_key_verified_by_user_id"]
    assert verified["answer_key_verified_at"]
    _verify(
        api_client,
        admin,
        book_id,
        revision_id,
        first["question_id"],
        first["options"][2]["option_id"],
    )
    _verify(
        api_client,
        admin,
        book_id,
        revision_id,
        second["question_id"],
        second["options"][0]["option_id"],
    )

    # 7. The per-set passing threshold, then the review.
    threshold = api_client.patch(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{exercise_id}/threshold",
        json={"passing_threshold_percent": 85},
        headers=admin,
    )
    assert threshold.status_code == 200, threshold.text
    assert threshold.json()["passing_threshold_percent"] == 85

    listed = _get_questions(api_client, admin, book_id, revision_id, exercise_id)
    assert [question["prompt"] for question in listed] == [
        "Why do leaves turn yellow?",
        Q2_PROMPT,
    ]
    for question in listed:
        assert question["answer_key_verified_at"] is not None
        assert (
            sum(1 for option in question["options"] if option["is_correct"]) == 1
        )

    _resolve_designations(api_client, admin, book_id, revision_id, _detail(
        api_client, admin, book_id, revision_id
    ))
    _approve_every_unit(api_client, admin, book_id, revision_id, _detail(
        api_client, admin, book_id, revision_id
    ))

    # 8. Setting the same threshold again is an idempotent no-op: the
    #    approval survives and no second audit event appears.
    again = api_client.patch(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{exercise_id}/threshold",
        json={"passing_threshold_percent": 85},
        headers=admin,
    )
    assert again.status_code == 200, again.text
    assert again.json()["review_status"] == "approved"
    assert again.json()["passing_threshold_percent"] == 85
    assert len(_audit_events(engine, "book_unit_threshold_set")) == 1

    # 9. Publish.
    published = _publish(api_client, admin, book_id, revision_id)
    assert published["status"] == "published"
    assert published["is_current_publication"] is True

    # 10. Durable state in fresh sessions.
    row = _revision_row(engine, revision_id)
    assert row["status"] == "published"
    assert row["is_current_publication"] is True
    questions = _question_rows(engine, revision_id)
    assert len(questions) == 2
    assert all(question["verifier"] for question in questions)
    assert all(question["answer_key_verified_at"] for question in questions)
    options = _option_rows(engine, revision_id)
    assert len(options) == 6
    keyed_counts: dict[str, int] = {}
    for option in options:
        if option["is_correct"]:
            keyed_counts[option["book_question_id"]] = (
                keyed_counts.get(option["book_question_id"], 0) + 1
            )
    assert set(keyed_counts.values()) == {1}
    assert len(keyed_counts) == 2
    assert _unit_row(engine, exercise_id)["passing_threshold_percent"] == 85
    assert _attempt_rows(engine) == []
    assert _get_questions(api_client, admin, book_id, revision_id, exercise_id) != []

    # 11. Audit: identifiers and counts only — never authored text.
    assert len(_audit_events(engine, "book_question_created")) == 3
    assert len(_audit_events(engine, "book_question_corrected")) == 1
    assert len(_audit_events(engine, "book_answer_key_verified")) == 3
    assert len(_audit_events(engine, "book_unit_threshold_set")) == 1
    assert len(_audit_events(engine, "book_question_deleted")) == 1
    assert len(_audit_events(engine, "book_revision_approved")) == 1
    blob = _audit_blob(engine)
    for fragment in ALL_AUTHORED_TEXT:
        assert fragment not in blob


def _q3_id(client, headers, book_id, revision_id, unit_id) -> uuid.UUID:
    questions = _get_questions(client, headers, book_id, revision_id, unit_id)
    assert len(questions) == 3
    return questions[2]["question_id"]


# --- roles, ids, lifecycle ----------------------------------------------------------------


def test_every_assessment_route_is_administrator_only_over_http(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    teacher = _teacher_header(api_client, engine)
    student = _student_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    units = _unit_ids(_detail(api_client, admin, book_id, revision_id))
    exercise_id = units["Exercises"]
    question = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
    )
    question_id = question["question_id"]
    option_id = question["options"][0]["option_id"]

    routes = [
        ("GET", _q_path(book_id, revision_id, unit_id=exercise_id), None),
        (
            "POST",
            _q_path(book_id, revision_id, unit_id=exercise_id),
            _mc(2, "another question", Q1_EXPLANATION, Q1_OPTIONS),
        ),
        (
            "PATCH",
            _q_path(book_id, revision_id, question_id=question_id),
            {"prompt": "nope"},
        ),
        ("DELETE", _q_path(book_id, revision_id, question_id=question_id), None),
        (
            "POST",
            f"{_q_path(book_id, revision_id, question_id=question_id)}/verify",
            {"correct_option_id": str(option_id)},
        ),
        (
            "PATCH",
            f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{exercise_id}/threshold",
            {"passing_threshold_percent": 60},
        ),
    ]
    for method, path, body in routes:
        refused = api_client.request(method, path, json=body, headers=teacher)
        assert refused.status_code == 403, f"{method} {path}: {refused.text}"
        assert refused.json()["detail"] == _ADMIN_DETAIL
        refused = api_client.request(method, path, json=body, headers=student)
        assert refused.status_code == 403, f"{method} {path}: {refused.text}"
        assert refused.json()["detail"] == _ADMIN_DETAIL
        anonymous = api_client.request(method, path, json=body)
        assert anonymous.status_code == 401, f"{method} {path}: {anonymous.text}"

    # Refused requests wrote nothing: one question, one creation event.
    assert len(_question_rows(engine, revision_id)) == 1
    assert len(_audit_events(engine, "book_question_created")) == 1
    assert _audit_events(engine, "book_question_corrected") == []
    assert _audit_events(engine, "book_question_deleted") == []
    assert _audit_events(engine, "book_answer_key_verified") == []
    assert _audit_events(engine, "book_unit_threshold_set") == []
    assert _unit_row(engine, exercise_id)["passing_threshold_percent"] is None


def test_unknown_and_foreign_ids_answer_the_same_404_over_http(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    units = _unit_ids(_detail(api_client, admin, book_id, revision_id))
    exercise_id = units["Exercises"]
    question = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
    )
    question_id = question["question_id"]
    option_id = question["options"][0]["option_id"]
    other = _upload_and_extract(api_client, admin, engine)

    unknown_book = api_client.get(
        _q_path(uuid.uuid4(), revision_id, unit_id=exercise_id), headers=admin
    )
    assert unknown_book.status_code == 404

    unknown_revision = api_client.get(
        _q_path(book_id, uuid.uuid4(), unit_id=exercise_id), headers=admin
    )
    foreign_revision = api_client.get(
        _q_path(book_id, other["revision_id"], unit_id=exercise_id), headers=admin
    )
    assert unknown_revision.status_code == 404
    assert foreign_revision.status_code == 404
    assert unknown_revision.json()["detail"].split(" ")[0:4] == (
        foreign_revision.json()["detail"].split(" ")[0:4]
    )

    unknown_unit = api_client.get(
        _q_path(book_id, revision_id, unit_id=uuid.uuid4()), headers=admin
    )
    assert unknown_unit.status_code == 404
    assert "no book unit with id" in unknown_unit.json()["detail"]

    probe_paths = [
        ("POST", _q_path(book_id, revision_id, unit_id=uuid.uuid4()),
         _mc(1, "x", Q1_EXPLANATION, Q1_OPTIONS)),
        ("PATCH", _q_path(book_id, revision_id, question_id=uuid.uuid4()),
         {"prompt": "x"}),
        ("DELETE", _q_path(book_id, revision_id, question_id=uuid.uuid4()), None),
        ("POST", f"{_q_path(book_id, revision_id, question_id=uuid.uuid4())}/verify",
         {"correct_option_id": str(option_id)}),
        ("PATCH",
         f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{uuid.uuid4()}/threshold",
         {"passing_threshold_percent": 60}),
    ]
    for method, path, body in probe_paths:
        response = api_client.request(method, path, json=body, headers=admin)
        assert response.status_code == 404, f"{method} {path}: {response.text}"

    # One setup question, one creation event — the 404s wrote nothing.
    assert len(_question_rows(engine, revision_id)) == 1
    assert len(_audit_events(engine, "book_question_created")) == 1
    for event_type in (
        "book_question_corrected",
        "book_question_deleted",
        "book_answer_key_verified",
        "book_unit_threshold_set",
    ):
        assert _audit_events(engine, event_type) == []


def test_published_and_rejected_revisions_refuse_every_question_mutation_over_http(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    units = _unit_ids(_detail(api_client, admin, book_id, revision_id))
    exercise_id = units["Exercises"]
    question = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
    )
    question_id = question["question_id"]
    option_id = question["options"][0]["option_id"]
    _verify(api_client, admin, book_id, revision_id, question_id, option_id)
    _approve_all_units_by_sql(engine, revision_id, _latest_admin_id(engine))
    _publish(api_client, admin, book_id, revision_id)

    mutations = [
        ("POST", _q_path(book_id, revision_id, unit_id=exercise_id),
         _mc(2, "late question", Q1_EXPLANATION, Q1_OPTIONS)),
        ("PATCH", _q_path(book_id, revision_id, question_id=question_id),
         {"prompt": "late edit"}),
        ("DELETE", _q_path(book_id, revision_id, question_id=question_id), None),
        ("POST", f"{_q_path(book_id, revision_id, question_id=question_id)}/verify",
         {"correct_option_id": str(option_id)}),
        ("PATCH",
         f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{exercise_id}/threshold",
         {"passing_threshold_percent": 60}),
    ]
    for method, path, body in mutations:
        response = api_client.request(method, path, json=body, headers=admin)
        assert response.status_code == 409, f"{method} {path}: {response.text}"
        assert "pending_review" in response.json()["detail"]
        assert "current status: published" in response.json()["detail"]

    # The read surface still works and the graded question is untouched.
    listed = _get_questions(api_client, admin, book_id, revision_id, exercise_id)
    assert len(listed) == 1
    assert listed[0]["answer_key_verified_at"] is not None
    questions = _question_rows(engine, revision_id)
    assert len(questions) == 1 and questions[0]["verifier"]
    assert len(_option_rows(engine, revision_id)) == 4
    assert _unit_row(engine, exercise_id)["passing_threshold_percent"] is None
    assert len(_audit_events(engine, "book_question_created")) == 1
    assert len(_audit_events(engine, "book_answer_key_verified")) == 1
    assert _audit_events(engine, "book_question_corrected") == []
    assert _audit_events(engine, "book_question_deleted") == []
    assert _audit_events(engine, "book_unit_threshold_set") == []

    # A rejected revision refuses authoring the same way.
    rejected_ids = _upload_and_extract(api_client, admin, engine)
    rejected = api_client.post(
        f"{BOOKS_PATH}/{rejected_ids['book_id']}/revisions/"
        f"{rejected_ids['revision_id']}/reject",
        json={"reason": "wrong textbook"},
        headers=admin,
    )
    assert rejected.status_code == 200, rejected.text
    refused = api_client.post(
        _q_path(
            rejected_ids["book_id"],
            rejected_ids["revision_id"],
            unit_id=_exercise_id(
                api_client,
                admin,
                rejected_ids["book_id"],
                rejected_ids["revision_id"],
            ),
        ),
        json=_mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
        headers=admin,
    )
    assert refused.status_code == 409
    assert "current status: rejected" in refused.json()["detail"]
    assert len(_audit_events(engine, "book_question_created")) == 1


# --- publication gates ----------------------------------------------------------------------


def test_publication_refuses_unverified_keys_and_empty_required_sets_over_http(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    units = _unit_ids(_detail(api_client, admin, book_id, revision_id))
    exercise_id = units["Exercises"]

    first = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
    )
    second = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _tf(2, Q2_PROMPT, Q2_EXPLANATION, Q2_OPTIONS),
    )
    _verify(
        api_client,
        admin,
        book_id,
        revision_id,
        first["question_id"],
        first["options"][0]["option_id"],
    )
    _set_exercise_required(engine, exercise_id)
    _resolve_designations(api_client, admin, book_id, revision_id, _detail(
        api_client, admin, book_id, revision_id
    ))
    _approve_every_unit(api_client, admin, book_id, revision_id, _detail(
        api_client, admin, book_id, revision_id
    ))

    # 1. One key still unconfirmed → 422, nothing published.
    refusal = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/approve",
        headers=admin,
    )
    assert refusal.status_code == 422, refusal.text
    assert "unverified answer key" in refusal.json()["detail"]
    assert _revision_row(engine, revision_id)["status"] == "pending_review"
    assert _audit_events(engine, "book_revision_approved") == []

    # 2. Remove every question from the required set → a different 422.
    for question_id in (first["question_id"], second["question_id"]):
        deleted = api_client.delete(
            _q_path(book_id, revision_id, question_id=question_id),
            headers=admin,
        )
        assert deleted.status_code == 204, deleted.text
    _approve_exercise(api_client, admin, book_id, revision_id, exercise_id)
    refusal = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/approve",
        headers=admin,
    )
    assert refusal.status_code == 422, refusal.text
    assert (
        "required exercise sets have no objective questions"
        in refusal.json()["detail"]
    )
    assert _revision_row(engine, revision_id)["status"] == "pending_review"
    assert _audit_events(engine, "book_revision_approved") == []

    # 3. Author and verify ONE question; the SAME revision publishes.
    question = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(3, Q3_PROMPT, Q3_EXPLANATION, Q3_OPTIONS),
    )
    _verify(
        api_client,
        admin,
        book_id,
        revision_id,
        question["question_id"],
        question["options"][1]["option_id"],
    )
    _approve_exercise(api_client, admin, book_id, revision_id, exercise_id)
    published = _publish(api_client, admin, book_id, revision_id)
    assert published["status"] == "published"
    assert published["is_current_publication"] is True
    assert len(_audit_events(engine, "book_revision_approved")) == 1
    assert len(_question_rows(engine, revision_id)) == 1
    assert _audit_events(engine, "book_question_deleted") and len(
        _audit_events(engine, "book_question_deleted")
    ) == 2


# --- correction editions ---------------------------------------------------------------------


def test_a_correction_edition_clones_questions_with_dead_answer_keys_over_http(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    units = _unit_ids(_detail(api_client, admin, book_id, revision_id))
    exercise_id = units["Exercises"]

    first = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
    )
    second = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _tf(2, Q2_PROMPT, Q2_EXPLANATION, Q2_OPTIONS),
    )
    for question in (first, second):
        _verify(
            api_client,
            admin,
            book_id,
            revision_id,
            question["question_id"],
            question["options"][0]["option_id"],
        )
    threshold = api_client.patch(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{exercise_id}/threshold",
        json={"passing_threshold_percent": 85},
        headers=admin,
    )
    assert threshold.status_code == 200, threshold.text
    _approve_all_units_by_sql(engine, revision_id, _latest_admin_id(engine))
    _publish(api_client, admin, book_id, revision_id)
    source_questions = _get_questions(
        api_client, admin, book_id, revision_id, exercise_id
    )

    created = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/correct",
        headers=admin,
    )
    assert created.status_code == 201, created.text
    copy = created.json()
    copy_id = copy["revision_id"]
    assert copy["revision_number"] == 2
    assert copy["status"] == "pending_review"

    # The clone carries the text with FRESH ids and a DEAD key.
    copy_units = _unit_ids(_detail(api_client, admin, book_id, copy_id))
    copy_exercise = copy_units["Exercises"]
    cloned = _get_questions(api_client, admin, book_id, copy_id, copy_exercise)
    assert [q["prompt"] for q in cloned] == [q["prompt"] for q in source_questions]
    assert [q["question_type"] for q in cloned] == [
        q["question_type"] for q in source_questions
    ]
    assert {q["question_id"] for q in cloned}.isdisjoint(
        {q["question_id"] for q in source_questions}
    )
    for source, dead in zip(source_questions, cloned):
        assert [o["option_text"] for o in dead["options"]] == [
            o["option_text"] for o in source["options"]
        ]
        assert {o["option_id"] for o in dead["options"]}.isdisjoint(
            {o["option_id"] for o in source["options"]}
        )
        assert dead["answer_key_verified_at"] is None
        assert dead["answer_key_verified_by_user_id"] is None
        assert all(option["is_correct"] is False for option in dead["options"])

    # The threshold rides along onto the copied exercise set — under a
    # FRESH unit id (the clone never shares source rows).
    assert copy_exercise != exercise_id
    assert not set(copy_units.values()) & set(units.values())
    assert (
        _unit_row(engine, copy_exercise)["passing_threshold_percent"] == 85
    )
    assert _unit_row(engine, copy_exercise)["review_status"] == "pending"

    # The published source is byte-identical.
    assert _get_questions(
        api_client, admin, book_id, revision_id, exercise_id
    ) == source_questions
    correction = _audit_events(engine, "book_revision_corrected")
    assert len(correction) == 1
    payload = json.loads(correction[0]["metadata_json"])
    assert payload["question_count"] == 2
    assert payload["source_revision_id"] == str(revision_id)

    # The dead keys cannot publish the clone until re-verified.
    _approve_all_units_by_sql(engine, copy_id, _latest_admin_id(engine))
    refusal = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{copy_id}/approve",
        headers=admin,
    )
    assert refusal.status_code == 422, refusal.text
    assert "unverified answer key" in refusal.json()["detail"]
    assert _revision_row(engine, copy_id)["status"] == "pending_review"

    for question in cloned:
        _verify(
            api_client,
            admin,
            book_id,
            copy_id,
            question["question_id"],
            question["options"][0]["option_id"],
        )
    published = _publish(api_client, admin, book_id, copy_id)
    assert published["is_current_publication"] is True
    assert _current_publications(engine, book_id) == [
        {"id": str(copy_id), "status": "published"}
    ]
    source_row = _revision_row(engine, revision_id)
    assert source_row["status"] == "published"
    assert source_row["is_current_publication"] is False
    assert len(
        [row for row in _question_rows(engine, copy_id) if row["verifier"]]
    ) == 2


# --- real row locks ---------------------------------------------------------------------------


def test_verification_and_publication_serialize_on_the_revision_row_lock(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    units = _unit_ids(_detail(api_client, admin, book_id, revision_id))
    exercise_id = units["Exercises"]
    question = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
    )
    admin_id = _latest_admin_id(engine)
    _approve_all_units_by_sql(engine, revision_id, admin_id)

    outcomes = _in_two_threads(
        (
            _verify_in_a_thread,
            (
                engine,
                book_id,
                revision_id,
                question["question_id"],
                question["options"][0]["option_id"],
                admin_id,
            ),
        ),
        (
            _publish_in_a_thread,
            (engine, book_id, revision_id, admin_id),
        ),
    )
    verify_outcome, publish_outcome = outcomes

    # The verification ALWAYS completes: publication can never beat it,
    # because an unverified key is exactly what publication refuses.
    assert verify_outcome == "verified", outcomes
    assert publish_outcome in ("published", "refused"), outcomes

    if publish_outcome == "refused":
        # The loser observed the unverified key; a retry now publishes.
        assert _revision_row(engine, revision_id)["status"] == "pending_review"
        assert _audit_events(engine, "book_revision_approved") == []
        _publish(api_client, admin, book_id, revision_id)

    row = _revision_row(engine, revision_id)
    assert row["status"] == "published"
    assert row["is_current_publication"] is True
    questions = _question_rows(engine, revision_id)
    assert all(question["answer_key_verified_at"] for question in questions)
    assert all(question["verifier"] for question in questions)
    assert len(_audit_events(engine, "book_answer_key_verified")) == 1
    assert len(_audit_events(engine, "book_revision_approved")) == 1
    assert len(_current_publications(engine, book_id)) == 1


def test_concurrent_attempt_recordings_persist_two_immutable_rows(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    units = _unit_ids(_detail(api_client, admin, book_id, revision_id))
    exercise_id = units["Exercises"]
    first = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _mc(1, Q1_PROMPT, Q1_EXPLANATION, Q1_OPTIONS),
    )
    second = _post_question(
        api_client,
        admin,
        book_id,
        revision_id,
        exercise_id,
        _tf(2, Q2_PROMPT, Q2_EXPLANATION, Q2_OPTIONS),
    )
    for question in (first, second):
        _verify(
            api_client,
            admin,
            book_id,
            revision_id,
            question["question_id"],
            question["options"][0]["option_id"],
        )
    _approve_all_units_by_sql(engine, revision_id, _latest_admin_id(engine))
    _publish(api_client, admin, book_id, revision_id)

    student_id, student_row_id = _create_student(engine)
    context_id = _context_id(engine, revision_id)
    # Both questions keyed option 0 — a perfect submission.
    from app.schemas.book_assessment import (
        AssessmentAnswerSubmit,
        AssessmentAttemptSubmit,
    )

    submission = AssessmentAttemptSubmit(
        learning_context_id=context_id,
        answers=[
            AssessmentAnswerSubmit(
                question_id=uuid.UUID(question["question_id"]),
                option_id=uuid.UUID(question["options"][0]["option_id"]),
            )
            for question in (first, second)
        ],
    )

    outcomes = _in_two_threads(
        (
            _attempt_in_a_thread,
            (engine, student_id, submission, book_id, revision_id, exercise_id),
        ),
        (
            _attempt_in_a_thread,
            (engine, student_id, submission, book_id, revision_id, exercise_id),
        ),
    )

    assert all(not str(outcome).startswith("failed") for outcome in outcomes), (
        outcomes
    )
    assert len(set(outcomes)) == 2, outcomes

    attempts = _attempt_rows(engine)
    assert len(attempts) == 2
    assert {attempt["id"] for attempt in attempts} == set(outcomes)
    for attempt in attempts:
        assert attempt["student_id"] == str(student_row_id)
        assert attempt["book_revision_id"] == str(revision_id)
        assert attempt["book_unit_id"] == str(exercise_id)
        assert attempt["total_questions"] == 2
        assert attempt["correct_answers"] == 2
        assert attempt["passed"] is True

    answers = _answer_rows(engine)
    assert len(answers) == 4
    per_attempt: dict[str, int] = {}
    for answer in answers:
        assert answer["is_correct"] is True
        per_attempt[answer["book_attempt_id"]] = (
            per_attempt.get(answer["book_attempt_id"], 0) + 1
        )
    assert per_attempt == {outcome: 2 for outcome in outcomes}
    assert len(_audit_events(engine, "book_attempt_recorded")) == 2
    for event in _audit_events(engine, "book_attempt_recorded"):
        assert event["actor_user_id"] is None
        assert event["user_id"] == str(student_id)
