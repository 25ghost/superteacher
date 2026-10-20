"""POST/GET /api/v1/admin/books — administrator book intake (book P1).

End to end on the real router against the PostgreSQL test database:

- the role contract: anonymous 401, teacher/student 403, administrator
  allowed — one role per endpoint, guard before handler;
- a real, text-extractable PDF upload becomes a book + edition 1
  (``uploaded``) + its file asset + exactly the canonical learning
  contexts supplied, with the bytes stored under the opaque
  ``file_assets/{uuid}`` key and a ``book_uploaded`` audit event;
- a non-PDF payload, a scanned/image-only document and an encrypted
  document are each refused 422, leave no rows and no stored bytes, and
  are audited as ``book_validation_failed`` with a stable reason code;
- an unknown learning context id answers 404 (same as a foreign one) and
  creates nothing;
- list/detail reads are bounded, newest first, and never expose a
  ``storage_key`` or file bytes.

The test database must already exist and be migrated
(``CREATE DATABASE super_teacher_db_test;`` + ``alembic upgrade head``).
"""
from __future__ import annotations

import hashlib
import io
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from tests.conftest import build_minimal_pdf

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

    Base.metadata.create_all(pg_engine)  # no-op when the migration schema is applied

    def _override_get_db():
        db = SessionLocal(bind=pg_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# --- principals ---------------------------------------------------------------------


def _header_for(client: TestClient, engine, *, role: str, prefix: str) -> dict:
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.teacher import Teacher
    from app.models.user import User

    email = f"books-{prefix}-{uuid.uuid4().hex[:8]}@test.example"
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
            session.add(Teacher(user_id=user.id, full_name="Books Teacher"))
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
    """A real canonical learning context (catalog FKs are really there)."""
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


def _upload(
    client: TestClient,
    headers: dict,
    context_id: uuid.UUID,
    *,
    data: bytes | None = None,
    filename: str = "textbook.pdf",
    content_type: str = "application/pdf",
    title: str = "Integrated Science S3",
    extra_context_ids: tuple[uuid.UUID, ...] = (),
):
    payload = data if data is not None else build_minimal_pdf()
    # httpx builds the multipart body from ``files`` parts; form fields ride
    # along as (name, (None, value)) tuples so repeated keys stay intact.
    parts: list[tuple] = [
        ("title", (None, title)),
        ("learning_context_id", (None, str(context_id))),
    ]
    parts.extend(
        ("learning_context_id", (None, str(extra))) for extra in extra_context_ids
    )
    parts.append(("file", (filename, payload, content_type)))
    return client.post(BOOKS_PATH, files=parts, headers=headers)


def _count(engine, table: str) -> int:
    from sqlalchemy import text

    with engine.connect() as connection:
        return connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _audit_events(engine, event_type: str) -> list[dict]:
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT user_id::text, actor_user_id::text, metadata_json "
                "FROM auth_events WHERE event_type = :event_type"
            ),
            {"event_type": event_type},
        ).mappings().all()
        return [dict(row) for row in rows]


def _assert_no_book_rows(engine) -> None:
    assert _count(engine, "books") == 0
    assert _count(engine, "book_revisions") == 0
    assert _count(engine, "book_revision_contexts") == 0
    assert _count(engine, "file_assets") == 0


# --- role contract -------------------------------------------------------------------


def test_anonymous_is_refused_on_every_book_route(api_client, clean_db) -> None:
    assert api_client.get(BOOKS_PATH).status_code == 401
    assert api_client.get(f"{BOOKS_PATH}/{uuid.uuid4()}").status_code == 401
    assert api_client.post(BOOKS_PATH).status_code == 401


def test_teacher_and_student_tokens_are_refused(api_client, clean_db) -> None:
    engine = clean_db
    for header in (_teacher_header(api_client, engine), _student_header(api_client, engine)):
        response = _upload(api_client, header, uuid.uuid4())
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == _ADMIN_DETAIL

        listing = api_client.get(BOOKS_PATH, headers=header)
        assert listing.status_code == 403, listing.text
        assert listing.json()["detail"] == _ADMIN_DETAIL

    _assert_no_book_rows(engine)


# --- a successful intake -------------------------------------------------------------


def test_admin_upload_creates_book_revision_asset_and_stores_the_bytes(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)
    data = build_minimal_pdf(pages=2)

    response = _upload(api_client, admin, context_id, data=data, title="Physics S3")
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["title"] == "Physics S3"
    assert body["learning_context_ids"] == [str(context_id)]
    assert body["revision"]["status"] == "uploaded"
    assert body["revision"]["revision_number"] == 1
    assert body["revision"]["file_asset"]["checksum_sha256"] == (
        hashlib.sha256(data).hexdigest()
    )
    assert "storage_key" not in response.text

    # Rows.
    assert _count(engine, "books") == 1
    assert _count(engine, "book_revisions") == 1
    assert _count(engine, "book_revision_contexts") == 1
    assert _count(engine, "file_assets") == 1

    # Stored bytes live under the opaque key, exactly as uploaded.
    from app.services.storage import get_storage_backend

    asset_id = body["revision"]["file_asset"]["file_asset_id"]
    storage_key = f"file_assets/{asset_id}"
    backend = get_storage_backend()
    assert backend.exists(storage_key)
    assert backend.read(storage_key) == data
    # The key never leaks through the API.
    assert storage_key not in response.text

    # Audit.
    events = _audit_events(engine, "book_uploaded")
    assert len(events) == 1
    assert events[0]["user_id"] == events[0]["actor_user_id"]
    assert str(body["book_id"]) in events[0]["metadata_json"]


def test_a_second_book_starts_its_own_edition_at_one(api_client, clean_db) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)

    first = _upload(api_client, admin, context_id, title="Book A")
    second = _upload(api_client, admin, context_id, title="Book B")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["book_id"] != second.json()["book_id"]
    assert first.json()["revision"]["revision_number"] == 1
    assert second.json()["revision"]["revision_number"] == 1
    assert _count(engine, "books") == 2


# --- refusals ------------------------------------------------------------------------


def test_a_non_pdf_payload_is_422_audited_and_leaves_nothing(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)

    response = _upload(
        api_client,
        admin,
        context_id,
        data=b"definitely not a pdf",
        filename="fake.pdf",
    )
    assert response.status_code == 422, response.text
    assert "declared type" in response.json()["detail"]

    _assert_no_book_rows(engine)
    events = _audit_events(engine, "book_validation_failed")
    assert len(events) == 1
    assert "magic_mismatch" in events[0]["metadata_json"]
    assert "fake.pdf" in events[0]["metadata_json"]


def test_an_image_only_document_is_422_and_audited(api_client, clean_db) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)

    response = _upload(
        api_client, admin, context_id, data=build_minimal_pdf(None), filename="scan.pdf"
    )
    assert response.status_code == 422, response.text
    assert "extractable text" in response.json()["detail"]

    _assert_no_book_rows(engine)
    events = _audit_events(engine, "book_validation_failed")
    assert len(events) == 1
    assert "no_extractable_text" in events[0]["metadata_json"]


def test_an_encrypted_document_is_422_and_audited(api_client, clean_db) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(build_minimal_pdf())))
    writer.encrypt("locked")
    buffer = io.BytesIO()
    writer.write(buffer)

    response = _upload(
        api_client, admin, context_id, data=buffer.getvalue(), filename="locked.pdf"
    )
    assert response.status_code == 422, response.text
    assert "encrypted" in response.json()["detail"]

    _assert_no_book_rows(engine)
    events = _audit_events(engine, "book_validation_failed")
    assert len(events) == 1
    assert "encrypted_pdf" in events[0]["metadata_json"]


def test_an_unknown_learning_context_404s_and_creates_nothing(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)

    response = _upload(api_client, admin, uuid.uuid4())
    assert response.status_code == 404, response.text
    assert "learning context" in response.json()["detail"]

    _assert_no_book_rows(engine)
    # Not a validation failure: nothing about the FILE was refused.
    assert _audit_events(engine, "book_validation_failed") == []
    assert _audit_events(engine, "book_uploaded") == []


def test_a_missing_context_field_is_422(api_client, clean_db) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)

    response = api_client.post(
        BOOKS_PATH,
        files=[
            ("title", (None, "No context")),
            ("file", ("book.pdf", build_minimal_pdf(), "application/pdf")),
        ],
        headers=admin,
    )
    assert response.status_code == 422, response.text
    _assert_no_book_rows(engine)


def test_repeated_context_fields_attach_every_canonical_context(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    first_id = _seed_context(engine)
    second_id = _seed_context(engine)

    response = _upload(
        api_client,
        admin,
        first_id,
        extra_context_ids=(second_id,),
        title="Two contexts",
    )
    assert response.status_code == 201, response.text
    assert sorted(response.json()["learning_context_ids"]) == sorted(
        [str(first_id), str(second_id)]
    )
    assert _count(engine, "book_revision_contexts") == 2


# --- reads ---------------------------------------------------------------------------


def test_list_and_detail_are_admin_only_bounded_and_path_free(
    api_client, clean_db
) -> None:
    engine = clean_db
    admin = _admin_header(api_client, engine)
    context_id = _seed_context(engine)

    assert api_client.get(BOOKS_PATH, headers=admin).json() == []

    created = [
        _upload(
            api_client,
            admin,
            context_id,
            title=f"Book {label}",
        ).json()
        for label in ("One", "Two")
    ]

    listing = api_client.get(f"{BOOKS_PATH}?limit=1", headers=admin)
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1
    assert "storage_key" not in listing.text

    assert api_client.get(f"{BOOKS_PATH}?limit=101", headers=admin).status_code == 422
    assert api_client.get(f"{BOOKS_PATH}?limit=0", headers=admin).status_code == 422
    assert api_client.get(f"{BOOKS_PATH}?offset=-1", headers=admin).status_code == 422

    detail = api_client.get(
        f"{BOOKS_PATH}/{created[0]['book_id']}", headers=admin
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["title"] == "Book One"
    assert detail.json()["learning_context_ids"] == [str(context_id)]
    assert detail.json()["revision"]["file_asset"]["original_filename"] == (
        "textbook.pdf"
    )
    assert "storage_key" not in detail.text

    missing = api_client.get(f"{BOOKS_PATH}/{uuid.uuid4()}", headers=admin)
    assert missing.status_code == 404, missing.text
    assert "no book with id" in missing.json()["detail"]
