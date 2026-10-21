"""Book content extraction and retry — integration (PostgreSQL, book P2).

End to end against ``super_teacher_db_test``, the exact pipeline an
administrator upload triggers in production:

- **upload → sweep → pending_review**: a real PDF uploaded through the
  API becomes a reviewable ``book_units`` tree — kinds, titles, source
  text, one-based page ranges, sibling order, ``unresolved`` assessment
  requirements — with the ``book_extract_started``/``book_extract_completed``
  audit pair, the extraction summary on the revision, and the source
  asset bytes + checksum untouched;
- **failure → retry**: a stored document that can no longer be parsed
  lands in ``extraction_failed`` with the fixed code and an empty unit
  table; the administrator-only retry endpoint requeues it (403/404/409
  contract included) and the next sweep finishes the job;
- **idempotency and recovery**: repeated sweeps never duplicate the tree;
  a revision stranded in ``uploaded`` (crash/redeploy) is extracted by
  the next pass with no client request; concurrent workers claiming the
  same batch produce ONE extraction, ONE tree and ONE audit pair;
- **isolation**: no student/teacher token reaches any book route, and
  nothing in the extracted hierarchy is exposed anywhere but the
  administrator book read.

The test database must already exist and be migrated
(``CREATE DATABASE super_teacher_db_test;`` + ``alembic upgrade head``).
Run with ``ENVIRONMENT=testing DB_NAME=super_teacher_db_test`` so the
application engine (and therefore the sweeper's ``SessionLocal``) binds
to the test database, exactly like the classroom lifecycle suite.
"""
from __future__ import annotations

import hashlib
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from sqlalchemy import func, select, text

from tests.conftest import build_minimal_pdf, build_multiline_pdf

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


def _session(engine):
    from app.core.database import SessionLocal

    return SessionLocal(bind=engine)


# --- principals ---------------------------------------------------------------------


def _header_for(client: TestClient, engine, *, role: str, prefix: str) -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.teacher import Teacher
    from app.models.user import User

    email = f"bx-{prefix}-{uuid.uuid4().hex[:8]}@test.example"
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
            session.add(Teacher(user_id=user.id, full_name="BX Teacher"))
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


# --- seeding -------------------------------------------------------------------------


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
        pathway = Pathway(code=f"XP{suffix}", name="O Level")
        level = EducationLevel(code=f"XL{suffix}", name="Senior 3", level_number=3)
        subject = Subject(code=f"XS{suffix}", name="Mathematics")
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


def _upload(
    client: TestClient,
    headers: dict,
    context_id: uuid.UUID,
    *,
    data: bytes,
    title: str = "Integrated Science S3",
):
    return client.post(
        BOOKS_PATH,
        files=[
            ("title", (None, title)),
            ("learning_context_id", (None, str(context_id))),
            ("file", ("textbook.pdf", data, "application/pdf")),
        ],
        headers=headers,
    )


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


# --- helpers --------------------------------------------------------------------------


def _sweep() -> int:
    """One sweep pass — the exact code path the lifespan startup runs."""
    from app.services.book_extraction_sweeper import book_extraction_sweeper

    return book_extraction_sweeper.sweep_once()


def _count(engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _units(engine, revision_id) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, parent_unit_id, kind, title, content, display_order,"
                "       source_page_start, source_page_end, extraction_warnings,"
                "       assessment_requirement"
                " FROM book_units WHERE book_revision_id = :rid"
                " ORDER BY COALESCE(parent_unit_id::text, ''), display_order"
            ),
            {"rid": str(revision_id)},
        ).mappings().all()
        return [dict(row) for row in rows]


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


def _revision(engine, book_id) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id::text, status, extraction_summary, extraction_error,"
                "       file_asset_id::text"
                " FROM book_revisions WHERE book_id = :bid"
            ),
            {"bid": str(book_id)},
        ).mappings().one()
        return dict(row)


def _encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(build_minimal_pdf())))
    writer.encrypt("locked")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- upload → sweep → pending_review ---------------------------------------------------


def test_an_uploaded_book_is_extracted_asynchronously_into_a_reviewable_tree(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)
    data = _science_pdf()

    created = _upload(api_client, admin, context_id, data=data, title="Science S3")
    assert created.status_code == 201, created.text
    book_id = created.json()["book_id"]
    assert created.json()["revision"]["status"] == "uploaded"

    # Nobody asked for extraction: the next background pass (which the
    # lifespan runs at startup) does the work.
    assert _sweep() == 1

    # Fresh-session assertions — a brand-new connection, not the sweep's.
    row = _revision(engine, book_id)
    assert row["status"] == "pending_review"
    assert row["extraction_error"] is None

    import json

    summary = json.loads(row["extraction_summary"])
    assert summary["page_count"] == 3
    assert "no_outline" in summary["warnings"]

    units = _units(engine, row["id"])
    assert len(units) == summary["unit_count"]
    by_title = {unit["title"]: unit for unit in units}
    chapter = by_title["Chapter 1 Introduction"]
    assert chapter["kind"] == "chapter"
    assert chapter["assessment_requirement"] == "unresolved"
    assert chapter["source_page_start"] == 1
    water = by_title["1.1 Water Cycle"]
    assert water["kind"] == "topic"
    assert water["source_page_start"] == 2 and water["source_page_end"] == 2
    assert water["parent_unit_id"] == chapter["id"]
    exercises = by_title["Exercises"]
    assert exercises["kind"] == "exercise_set"
    assert exercises["parent_unit_id"] == water["id"]
    assert by_title["Chapter 2 Forces"]["source_page_start"] == 3
    # Source-derived text survives verbatim.
    assert "Water evaporates and falls as rain." in water["content"]

    # The API read carries the outcome — counts and warnings, never the
    # extracted prose itself.
    detail = api_client.get(f"{BOOKS_PATH}/{book_id}", headers=admin)
    assert detail.status_code == 200
    body = detail.json()
    assert body["revision"]["status"] == "pending_review"
    assert body["revision"]["extraction_summary"]["unit_count"] == len(units)
    assert body["revision"]["extraction_error"] is None
    assert "Water evaporates" not in detail.text
    assert "storage_key" not in detail.text

    # Audit: one started + one completed, about the revision, no invented
    # administrator actor for the worker's own events.
    started = _audit_events(engine, "book_extract_started")
    completed = _audit_events(engine, "book_extract_completed")
    assert len(started) == 1 and len(completed) == 1
    assert started[0]["actor_user_id"] is None
    assert str(book_id) in started[0]["metadata_json"]

    # The source PDF and its checksum are byte-identical after extraction.
    from app.services.storage import get_storage_backend

    asset_id = created.json()["revision"]["file_asset"]["file_asset_id"]
    backend = get_storage_backend()
    assert backend.read(f"file_assets/{asset_id}") == data
    with engine.connect() as connection:
        checksum = connection.execute(
            text("SELECT checksum_sha256 FROM file_assets WHERE id = :aid"),
            {"aid": asset_id},
        ).scalar_one()
    assert checksum == hashlib.sha256(data).hexdigest()


def test_a_textless_document_fails_durably_and_the_administrator_may_retry(
    api_client, clean_db
) -> None:
    """Failure is a distinct state; retry requeues; the re-run succeeds."""
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)

    # A PDF with no extractable text is refused at intake (P1), so the
    # failure path is a document that BECOMES unreadable after intake —
    # here: the stored bytes are replaced with a textless container, the
    # way disk corruption would present itself to the extractor.
    created = _upload(api_client, admin, context_id, data=_science_pdf())
    assert created.status_code == 201, created.text
    book_id = created.json()["book_id"]
    row = _revision(engine, book_id)

    from app.services.storage import get_storage_backend

    backend = get_storage_backend()
    storage_key = f"file_assets/{row['file_asset_id']}"
    backend.save(storage_key, build_minimal_pdf(None), content_type="application/pdf")

    assert _sweep() == 1
    failed = _revision(engine, book_id)
    assert failed["status"] == "extraction_failed"
    assert failed["extraction_error"].startswith("no_extractable_text:")
    assert failed["extraction_summary"] is None
    assert _units(engine, failed["id"]) == []
    assert len(_audit_events(engine, "book_extract_failed")) == 1

    # Only an administrator may retry; the response shows the requeue.
    retried = api_client.post(
        f"{BOOKS_PATH}/{book_id}/retry-extraction", headers=admin
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["revision"]["status"] == "uploaded"
    assert retried.json()["revision"]["extraction_error"] is None
    assert len(_audit_events(engine, "book_extract_retry")) == 1

    # Repair the stored bytes (as an operator would after the root
    # cause) and the next sweep completes the requeued extraction.
    backend.save(storage_key, _science_pdf(), content_type="application/pdf")
    assert _sweep() == 1
    done = _revision(engine, book_id)
    assert done["status"] == "pending_review"
    assert done["extraction_error"] is None
    assert _units(engine, done["id"]) != []
    assert len(_audit_events(engine, "book_extract_completed")) == 1


def test_retry_contract_unknown_conflict_and_role_guards(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    teacher = _teacher_header(api_client, engine)
    student = _student_header(api_client, engine)
    context_id = _seed_context(engine)

    created = _upload(api_client, admin, context_id, data=_science_pdf())
    book_id = created.json()["book_id"]

    # Not failed yet → 409 for everyone who is allowed to ask at all.
    conflict = api_client.post(
        f"{BOOKS_PATH}/{book_id}/retry-extraction", headers=admin
    )
    assert conflict.status_code == 409, conflict.text

    # Wrong role → 403, before any state check.
    for header in (teacher, student):
        refused = api_client.post(
            f"{BOOKS_PATH}/{book_id}/retry-extraction", headers=header
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == _ADMIN_DETAIL
    assert api_client.post(f"{BOOKS_PATH}/{book_id}/retry-extraction").status_code == 401

    # Unknown id → the established foreign-resource 404.
    missing = api_client.post(
        f"{BOOKS_PATH}/{uuid.uuid4()}/retry-extraction", headers=admin
    )
    assert missing.status_code == 404, missing.text
    assert "no book with id" in missing.json()["detail"]

    # A completed extraction cannot be "retried" either.
    assert _sweep() == 1
    done = api_client.post(
        f"{BOOKS_PATH}/{book_id}/retry-extraction", headers=admin
    )
    assert done.status_code == 409, done.text

    assert _audit_events(engine, "book_extract_retry") == []


def test_no_student_or_teacher_surface_exists_for_books(
    api_client, clean_db
) -> None:
    """P2 adds no learning-side book access; content stays unpublished."""
    engine = clean_db
    admin = _admin_header(api_client, engine)
    teacher = _teacher_header(api_client, engine)
    student = _student_header(api_client, engine)
    context_id = _seed_context(engine)
    created = _upload(api_client, admin, context_id, data=_science_pdf())
    book_id = created.json()["book_id"]
    assert _sweep() == 1

    # Every book route refuses the learning roles.
    for header in (teacher, student):
        assert api_client.get(BOOKS_PATH, headers=header).status_code == 403
        assert (
            api_client.get(f"{BOOKS_PATH}/{book_id}", headers=header).status_code == 403
        )
        assert (
            api_client.post(
                f"{BOOKS_PATH}/{book_id}/retry-extraction", headers=header
            ).status_code
            == 403
        )
    # Anonymous is 401 everywhere.
    assert api_client.get(f"{BOOKS_PATH}/{book_id}").status_code == 401

    # And nothing was published: the revision is pending_review, the
    # statuses reachable from this pipeline are only the intake ones.
    row = _revision(engine, book_id)
    assert row["status"] == "pending_review"
    with engine.connect() as connection:
        statuses = {
            status
            for (status,) in connection.execute(text("SELECT DISTINCT status FROM book_revisions"))
        }
    assert statuses == {"pending_review"}


# --- idempotency, recovery and concurrency ---------------------------------------------


def test_repeated_sweeps_never_duplicate_units_or_audit_rows(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)
    created = _upload(api_client, admin, context_id, data=_science_pdf())
    book_id = created.json()["book_id"]

    assert _sweep() == 1
    row = _revision(engine, book_id)
    first_ids = {unit["id"] for unit in _units(engine, row["id"])}
    assert first_ids

    # Further passes find nothing extracted-eligible and change nothing.
    assert _sweep() == 0
    assert _sweep() == 0
    second_ids = {unit["id"] for unit in _units(engine, row["id"])}
    assert second_ids == first_ids
    assert len(_audit_events(engine, "book_extract_completed")) == 1
    assert len(_audit_events(engine, "book_extract_started")) == 1


def test_a_revision_stranded_uploaded_by_a_crash_is_recovered_by_the_next_pass(
    api_client, clean_db
) -> None:
    """Restart recovery: no request, no retry — the sweep alone finishes it."""
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)
    created = _upload(api_client, admin, context_id, data=_science_pdf())
    book_id = created.json()["book_id"]

    # The "crash": the process died after intake committed and before
    # any sweep ran. The revision simply sits in ``uploaded`` with its
    # stored PDF — exactly what a redeploy leaves behind.
    assert _revision(engine, book_id)["status"] == "uploaded"

    assert _sweep() == 1  # the startup pass, no client involved
    assert _revision(engine, book_id)["status"] == "pending_review"
    assert _units(engine, _revision(engine, book_id)["id"]) != []


def test_concurrent_workers_claiming_the_same_batch_extract_exactly_once(
    api_client, clean_db
) -> None:
    """Two sessions, one revision: PostgreSQL's row lock is the referee.

    Each thread opens its own session, exactly as a second worker
    process would; the loser blocks on ``SELECT ... FOR UPDATE``,
    re-reads the committed state and extracts nothing — ONE tree, ONE
    started/completed audit pair.
    """
    from app.core.database import SessionLocal
    from app.services.book_extraction_service import extract_due_revisions

    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)
    created = _upload(api_client, admin, context_id, data=_science_pdf())
    book_id = created.json()["book_id"]

    def _one_worker() -> int:
        session = SessionLocal()
        try:
            return extract_due_revisions(session, limit=10)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _one_worker(), range(2)))

    assert sorted(results) == [0, 1]
    row = _revision(engine, book_id)
    assert row["status"] == "pending_review"
    assert len(_audit_events(engine, "book_extract_started")) == 1
    assert len(_audit_events(engine, "book_extract_completed")) == 1
    assert len(_audit_events(engine, "book_extract_failed")) == 0
    # One tree, and its root count matches the summary.
    import json

    units = _units(engine, row["id"])
    assert len(units) == json.loads(row["extraction_summary"])["unit_count"]
    root_orders = [
        unit["display_order"]
        for unit in units
        if unit["parent_unit_id"] is None
    ]
    assert root_orders == sorted(set(root_orders)) == [1, 2]


def test_failed_processing_leaves_no_partial_hierarchy_at_all(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)
    created = _upload(api_client, admin, context_id, data=_science_pdf())
    book_id = created.json()["book_id"]
    row = _revision(engine, book_id)

    from app.services.storage import get_storage_backend

    backend = get_storage_backend()
    backend.save(
        f"file_assets/{row['file_asset_id']}",
        build_minimal_pdf(None),
        content_type="application/pdf",
    )

    assert _sweep() == 1
    failed = _revision(engine, book_id)
    assert failed["status"] == "extraction_failed"
    # Zero units: the atomic outcome is "honest failure", never a
    # half-written tree awaiting review.
    assert _units(engine, failed["id"]) == []
    assert _count(engine, "book_units") == 0


def test_an_encrypted_document_stored_after_intake_fails_with_the_fixed_code(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)
    created = _upload(api_client, admin, context_id, data=_science_pdf())
    book_id = created.json()["book_id"]
    row = _revision(engine, book_id)

    from app.services.storage import get_storage_backend

    backend = get_storage_backend()
    backend.save(
        f"file_assets/{row['file_asset_id']}",
        _encrypted_pdf(),
        content_type="application/pdf",
    )

    assert _sweep() == 1
    failed = _revision(engine, book_id)
    assert failed["status"] == "extraction_failed"
    assert failed["extraction_error"].startswith("encrypted_pdf:")
    # The stored reason never echoes storage paths or library internals.
    assert "file_assets/" not in failed["extraction_error"]
