"""Book review & publication — integration (PostgreSQL, book P3).

End to end against ``super_teacher_db_test`` over HTTP, the exact
decisions an administrator makes after extraction finished:

- **the full review loop**: upload → background sweep → inspect the
  unit tree → correct designations (and one title) → approve every unit
  → approve the revision, which becomes the book's CURRENT publication;
  every step re-read from a FRESH session, audit events included;
- **rejection → correction edition**: reject with a recorded reason,
  clone a correction revision (next edition number, fresh unit ids,
  source untouched), finish its review and publish it — the pointer
  moves, the rejected history stays;
- **role and lifecycle guards**: student/teacher/anonymous never pass,
  unknown and foreign ids answer the same 404, invalid corrections and
  incomplete approvals are 422, non-pending transitions are 409 —
  and refused requests write nothing (no audit, no state);
- **real row locks**: two administrators racing to publish the SAME
  revision through separate sessions serialize on the book row —
  exactly one wins, exactly one ``book_revision_approved`` event, and
  the partial unique index never observes two current rows.

The test database must already exist and be migrated to head 0020
(``alembic upgrade head``). Run with ``ENVIRONMENT=testing
DB_NAME=super_teacher_db_test`` so the application engine binds to the
test database.
"""
from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests.conftest import build_multiline_pdf

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

BOOKS_PATH = "/api/v1/admin/books"
_ADMIN_DETAIL = "administrator role required for this operation"


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


# --- principals & seeding -------------------------------------------------------------


def _header_for(client: TestClient, engine, *, role: str, prefix: str) -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.teacher import Teacher
    from app.models.user import User

    email = f"rv-{prefix}-{uuid.uuid4().hex[:8]}@test.example"
    session = SessionLocal(bind=engine)
    try:
        user = User(
            email=email,
            role=role,
            status="active",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        if role == "teacher":
            session.flush()
            session.add(Teacher(user_id=user.id, full_name="RV Teacher"))
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


def _teacher_header(client: TestClient, engine) -> dict:
    return _header_for(client, engine, role="teacher", prefix="teacher")


def _student_header(client: TestClient, engine) -> dict:
    return _header_for(client, engine, role="student", prefix="student")


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
        pathway = Pathway(code=f"RP{suffix}", name="O Level")
        level = EducationLevel(code=f"RL{suffix}", name="Senior 3", level_number=3)
        subject = Subject(code=f"RS{suffix}", name="Mathematics")
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


def _resolve_designations(client, headers, book_id, revision_id, detail) -> None:
    """PATCH ``required`` onto every chapter/topic unit of the tree."""
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
        assert response.json()["review_status"] == "approved"


def _revision_row(engine, revision_id) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id::text, book_id::text, revision_number, status,"
                "       is_current_publication, rejection_reason"
                " FROM book_revisions WHERE id = :rid"
            ),
            {"rid": str(revision_id)},
        ).mappings().one()
        return dict(row)


def _audit_events(engine, event_type: str) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT user_id::text, actor_user_id::text, metadata_json "
                "FROM auth_events WHERE event_type = :event_type"
            ),
            {"event_type": event_type},
        ).mappings().all()
        return [dict(row) for row in rows]


def _current_publications(engine, book_id) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id::text, status FROM book_revisions"
                " WHERE book_id = :bid AND is_current_publication"
            ),
            {"bid": str(book_id)},
        ).mappings().all()
        return [dict(row) for row in rows]


# --- the full review loop ----------------------------------------------------------------


def test_the_full_review_loop_publishes_the_book_over_http(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]

    # 1. Inspection: the extracted tree, pending, no storage paths.
    detail = _detail(api_client, admin, book_id, revision_id)
    assert detail["status"] == "pending_review"
    assert detail["is_current_publication"] is False
    assert detail["rejection_reason"] is None
    titles = [unit["title"] for unit in detail["units"]]
    assert titles[0] == "Chapter 1 Introduction"
    assert "1.1 Water Cycle" in titles and "Exercises" in titles
    assert all(
        unit["review_status"] == "pending" for unit in detail["units"]
    )
    assert "storage_key" not in api_client.get(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}", headers=admin
    ).text

    # 2. Corrections: resolve the designations, fix one title.
    _resolve_designations(api_client, admin, book_id, revision_id, detail)
    topic_id = _unit_ids(detail)["1.1 Water Cycle"]
    corrected = api_client.patch(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{topic_id}",
        json={"title": "1.1 The Water Cycle"},
        headers=admin,
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["title"] == "1.1 The Water Cycle"
    assert corrected.json()["review_status"] == "pending"

    # 3. Approve every unit (the topic keeps its corrected title).
    detail = _detail(api_client, admin, book_id, revision_id)
    assert {u["title"] for u in detail["units"]} >= {"1.1 The Water Cycle"}
    _approve_every_unit(api_client, admin, book_id, revision_id, detail)

    # 4. Publish: the revision becomes the book's current publication.
    approved = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/approve",
        headers=admin,
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "published"
    assert body["is_current_publication"] is True
    assert all(u["review_status"] == "approved" for u in body["units"])

    # Fresh sessions: durable state + the book read carries the pointer.
    row = _revision_row(engine, revision_id)
    assert row["status"] == "published"
    assert row["is_current_publication"] is True
    assert _current_publications(engine, book_id) == [
        {"id": str(revision_id), "status": "published"}
    ]
    book = api_client.get(f"{BOOKS_PATH}/{book_id}", headers=admin)
    assert book.status_code == 200
    assert book.json()["revision"]["is_current_publication"] is True
    assert book.json()["revision"]["status"] == "published"

    # Audit, in a fresh session: reviewers recorded, identifiers only.
    corrections = _audit_events(engine, "book_unit_corrected")
    assert len(corrections) >= 1
    approvals = _audit_events(engine, "book_unit_approved")
    assert len(approvals) == 4
    assert all(e["actor_user_id"] for e in approvals)
    publications = _audit_events(engine, "book_revision_approved")
    assert len(publications) == 1
    payload = json.loads(publications[0]["metadata_json"])
    assert payload["previous_published_revision_id"] is None
    assert payload["unit_count"] == len(detail["units"])
    # No extracted document prose ever reaches the audit trail.
    for event_type in ("book_unit_corrected", "book_unit_approved",
                       "book_revision_approved"):
        for event in _audit_events(engine, event_type):
            assert "Water evaporates" not in (event["metadata_json"] or "")


# --- rejection → correction edition → publication -----------------------------------------


def test_rejection_correction_revision_and_republish_over_http(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]

    # 1. Rejection with a recorded reason.
    rejected = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/reject",
        json={"reason": "  chapter 2 is from the wrong textbook  "},
        headers=admin,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rejection_reason"] == (
        "chapter 2 is from the wrong textbook"
    )
    row = _revision_row(engine, revision_id)
    assert row["status"] == "rejected"
    assert row["rejection_reason"] == "chapter 2 is from the wrong textbook"
    assert row["is_current_publication"] is False
    assert _audit_events(engine, "book_revision_rejected")[0]["actor_user_id"]

    # 2. A rejected revision refuses every other decision (409)…
    approve_again = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/approve",
        headers=admin,
    )
    assert approve_again.status_code == 409

    # 3. …but yields a correction edition.
    created = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/correct",
        headers=admin,
    )
    assert created.status_code == 201, created.text
    copy = created.json()
    assert copy["revision_number"] == 2
    assert copy["status"] == "pending_review"
    assert copy["is_current_publication"] is False
    assert copy["rejection_reason"] is None
    source_titles = {u["title"] for u in _detail(
        api_client, admin, book_id, revision_id
    )["units"]}
    assert {u["title"] for u in copy["units"]} == source_titles
    assert all(u["review_status"] == "pending" for u in copy["units"])
    copy_id = copy["revision_id"]

    # The source edition is untouched.
    source_row = _revision_row(engine, revision_id)
    assert source_row["status"] == "rejected"
    assert source_row["rejection_reason"] == (
        "chapter 2 is from the wrong textbook"
    )

    # 4. Review and publish the new edition; the pointer moves.
    copy_detail = _detail(api_client, admin, book_id, copy_id)
    _resolve_designations(api_client, admin, book_id, copy_id, copy_detail)
    copy_detail = _detail(api_client, admin, book_id, copy_id)
    _approve_every_unit(api_client, admin, book_id, copy_id, copy_detail)
    published = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{copy_id}/approve",
        headers=admin,
    )
    assert published.status_code == 200, published.text
    assert published.json()["is_current_publication"] is True

    assert _revision_row(engine, copy_id)["is_current_publication"] is True
    assert _current_publications(engine, book_id) == [
        {"id": str(copy_id), "status": "published"}
    ]
    # Rejected history is preserved, not rewritten.
    assert _revision_row(engine, revision_id)["status"] == "rejected"
    publication = json.loads(
        _audit_events(engine, "book_revision_approved")[0]["metadata_json"]
    )
    assert publication["previous_published_revision_id"] is None
    # The copy event names its source.
    corrected = _audit_events(engine, "book_revision_corrected")
    assert json.loads(corrected[0]["metadata_json"])["source_revision_id"] == (
        str(revision_id)
    )


# --- guards: roles, ids, lifecycle ----------------------------------------------------------


def test_role_guards_and_error_contract_over_http(api_client, clean_db) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    teacher = _teacher_header(api_client, engine)
    student = _student_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    detail = _detail(api_client, admin, book_id, revision_id)
    unit_id = detail["units"][0]["unit_id"]

    routes = [
        ("GET", f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}", None),
        (
            "PATCH",
            f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit_id}",
            {"title": "x"},
        ),
        (
            "POST",
            f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit_id}/approve",
            None,
        ),
        ("POST", f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/approve", None),
        (
            "POST",
            f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/reject",
            {"reason": "no"},
        ),
        ("POST", f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/correct", None),
    ]
    for method, path, body in routes:
        refused = api_client.request(
            method, path, json=body, headers=teacher
        )
        assert refused.status_code == 403, f"{method} {path}: {refused.text}"
        assert refused.json()["detail"] == _ADMIN_DETAIL
        refused = api_client.request(
            method, path, json=body, headers=student
        )
        assert refused.status_code == 403, f"{method} {path}: {refused.text}"
        anonymous = api_client.request(method, path, json=body)
        assert anonymous.status_code == 401, f"{method} {path}: {anonymous.text}"

    # Unknown and foreign ids answer the same 404 (no existence leak).
    unknown = api_client.get(
        f"{BOOKS_PATH}/{book_id}/revisions/{uuid.uuid4()}", headers=admin
    )
    assert unknown.status_code == 404
    other = _upload_and_extract(api_client, admin, engine)
    foreign = api_client.get(
        f"{BOOKS_PATH}/{book_id}/revisions/{other['revision_id']}",
        headers=admin,
    )
    assert foreign.status_code == 404
    assert unknown.json()["detail"].split(" ")[0:4] == (
        foreign.json()["detail"].split(" ")[0:4]
    )
    missing_book = api_client.get(
        f"{BOOKS_PATH}/{uuid.uuid4()}/revisions/{revision_id}", headers=admin
    )
    assert missing_book.status_code == 404
    missing_unit = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{uuid.uuid4()}/approve",
        headers=admin,
    )
    assert missing_unit.status_code == 404

    # Invalid corrections are 422 and write nothing.
    before = _detail(api_client, admin, book_id, revision_id)
    empty = api_client.patch(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit_id}",
        json={},
        headers=admin,
    )
    assert empty.status_code == 422
    null_title = api_client.patch(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit_id}",
        json={"content": None},
        headers=admin,
    )
    assert null_title.status_code == 422
    unknown_key = api_client.patch(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit_id}",
        json={"storage_key": "file_assets/1"},
        headers=admin,
    )
    assert unknown_key.status_code == 422
    after = _detail(api_client, admin, book_id, revision_id)
    assert [u["title"] for u in after["units"]] == [
        u["title"] for u in before["units"]
    ]
    assert _audit_events(engine, "book_unit_corrected") == []

    # A blank rejection reason is refused by the service (422).
    blank = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/reject",
        json={"reason": "   "},
        headers=admin,
    )
    assert blank.status_code == 422
    assert _revision_row(engine, revision_id)["status"] == "pending_review"

    # An incomplete approval is 422 and leaves the revision pending.
    incomplete = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/approve",
        headers=admin,
    )
    assert incomplete.status_code == 422
    assert "still unapproved" in incomplete.json()["detail"]
    assert _revision_row(engine, revision_id)["status"] == "pending_review"

    # Double unit approval is 409.
    _resolve_designations(api_client, admin, book_id, revision_id, detail)
    first = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit_id}/approve",
        headers=admin,
    )
    assert first.status_code == 200, first.text
    second = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/units/{unit_id}/approve",
        headers=admin,
    )
    assert second.status_code == 409

    # Correction of a still-pending source is 409.
    premature = api_client.post(
        f"{BOOKS_PATH}/{book_id}/revisions/{revision_id}/correct",
        headers=admin,
    )
    assert premature.status_code == 409


# --- real row locks: two administrators race --------------------------------------------------


def _approve_all_units_by_sql(engine, revision_id, reviewer_id) -> None:
    """Fast-path designation resolution + unit approval.

    The race under test is PUBLICATION, not the per-unit review steps.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE book_units SET assessment_requirement = 'required'"
                " WHERE book_revision_id = :rid"
                "   AND kind IN ('chapter', 'topic')"
            ),
            {"rid": str(revision_id)},
        )
        connection.execute(
            text(
                "UPDATE book_units SET review_status = 'approved',"
                "       reviewed_by_user_id = :reviewer_id,"
                "       reviewed_at = :now"
                " WHERE book_revision_id = :rid"
            ),
            {
                "rid": str(revision_id),
                "reviewer_id": str(reviewer_id),
                "now": datetime.now(timezone.utc),
            },
        )


def _latest_admin_id(engine) -> uuid.UUID:
    with engine.connect() as connection:
        # psycopg already returns a UUID object for an uuid column.
        return connection.execute(
            text(
                "SELECT id FROM users WHERE role = 'admin' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        ).scalar_one()


def _publish_in_a_thread(engine, book_id, revision_id, user_id) -> str:
    from app.core.database import SessionLocal
    from app.services.book_review_service import approve_revision
    from app.services.learning_context_service import LearningConflictError

    session = SessionLocal(bind=engine)
    try:
        actor = type("Actor", (), {"id": user_id, "role": "admin"})()
        try:
            approve_revision(
                session, actor, book_id=book_id, revision_id=revision_id
            )
            session.commit()
            return "published"
        except LearningConflictError:
            session.rollback()
            return "conflict"
    finally:
        session.close()


def test_two_administrators_racing_to_publish_serialize_on_the_book_lock(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    ids = _upload_and_extract(api_client, admin, engine)
    book_id, revision_id = ids["book_id"], ids["revision_id"]
    reviewer = _latest_admin_id(engine)
    _approve_all_units_by_sql(engine, revision_id, reviewer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: _publish_in_a_thread(
                    engine, book_id, revision_id, reviewer
                ),
                range(2),
            )
        )

    # Exactly one publication wins; the loser observes the committed
    # status and answers 409 — never a second current row, never a 500.
    assert sorted(outcomes) == ["conflict", "published"], outcomes
    row = _revision_row(engine, revision_id)
    assert row["status"] == "published"
    assert row["is_current_publication"] is True
    assert len(_current_publications(engine, book_id)) == 1
    assert len(_audit_events(engine, "book_revision_approved")) == 1
