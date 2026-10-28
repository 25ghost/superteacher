"""Shared pytest fixtures for the SuperTeacher backend test suite.

Safety model:

- Unit tests (default) never touch PostgreSQL.
- Integration tests (``-m integration``) run against a **test database**
  only. The guard below refuses to run them unless
  ``ENVIRONMENT=testing`` and the database name ends with ``_test``
  (e.g. ``super_teacher_db_test``). This makes it structurally impossible
  for fixtures to truncate or write into the development database
  (``super_teacher_db``) or any production database.

Integration fixtures:

- ``pg_engine``: session-scoped engine bound to the guarded test DB URL.
- ``clean_db``: function-scoped; truncates all application tables
  (RESTART IDENTITY CASCADE) before the test and again in the fixture
  finalizer, so a failing assertion can never leak rows into the next test.
- ``db_session``: a session wrapped in an outer transaction that is always
  rolled back — temporary inserts never persist.

Test-database creation is deliberately manual (``CREATE DATABASE
super_teacher_db_test;``) — the suite never creates or destroys databases.
"""
from __future__ import annotations

import io
import os
import uuid as uuid_module
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# app.main refuses to import when ENVIRONMENT is not set explicitly (startup
# fail-fast, Phase B2). A fresh checkout has no .env yet, so the suite states
# its own stage here instead of inheriting the development class default.
# An externally provided value (e.g. ENVIRONMENT=testing for integration
# runs) is never overridden.
os.environ.setdefault("ENVIRONMENT", "development")

# The application tables, children first for TRUNCATE CASCADE simplicity.
APPLICATION_TABLES: tuple[str, ...] = (
    # Administrator book intake (0018/0019/0021) - assessment children
    # (answers → attempts, options → questions) before units, then
    # context links before revisions before books; all before the
    # file_assets/users they reference.
    "book_attempt_answers",
    "book_attempts",
    "book_question_options",
    "book_questions",
    "book_units",
    "book_revision_contexts",
    "book_revisions",
    "books",
    # Phase 3 slice 3A - classroom children before the class sessions they
    # hang off (messages/segments/tickets cascade from online_class_sessions).
    "class_messages",
    "class_attendance_segments",
    "class_ws_tickets",
    "online_class_sessions",
    # Phase 2 slice 2C - personal material progress (children of materials).
    "material_progress",
    # Phase 2 slice 2B — materials: moderation rows before materials before
    # file assets before the offerings they hang off, then slice 2A's
    # curriculum and Phase 1's marketplace tables.
    "material_moderations",
    "materials",
    "file_assets",
    "lessons",
    "topics",
    "learning_enrollments",
    "teaching_offerings",
    # Verification policy (0017): checks/assessments/evidence before their
    # applications; approved contexts and application contexts before the
    # applications; all before learning_contexts/teachers they reference.
    "teacher_verification_checks",
    "teacher_assessments",
    "teacher_evidence_contexts",
    "teacher_evidence",
    "teacher_approved_contexts",
    "teacher_application_contexts",
    "teacher_applications",
    "learning_contexts",
    "student_subjects",
    "student_enrollments",
    "student_profile_history",
    "school_programs",
    "schools",
    "program_subjects",
    "program_versions",
    "tvet_programs",
    "tvet_sectors",
    "subjects",
    "programs",
    "pathway_levels",
    "education_levels",
    "pathways",
    "academic_years",
    "students",
    "teachers",
    "invite_tokens",
    # auth children of users — truncated too, so an audit/session row can
    # never leak from one integration test into the next.
    "auth_events",
    "password_reset_tokens",
    "auth_sessions",
    "users",
)


class UnsafeTestDatabase(RuntimeError):
    """Raised when integration tests target a database that is not clearly a test database."""


def _escape_pdf_text(text: str) -> bytes:
    """Literal-string body escaping (ASCII/latin-1 fixtures only)."""
    return (
        text.encode("latin-1")
        .replace(b"\\", b"\\\\")
        .replace(b"(", b"\\(")
        .replace(b")", b"\\)")
    )


def _assemble_pdf(page_bodies: list[bytes]) -> bytes:
    """Byte-exact minimal PDF around one content-stream body per page.

    The object layout and xref offsets are computed for real — a
    hand-waved xref would be rejected as corrupt, which is precisely
    what some tests want (they corrupt the result deliberately).
    """
    pages = len(page_bodies)
    # Object layout: 1 catalog, 2 pages node, then one page object and one
    # content-stream object per page, then the font.
    page_ids = list(range(3, 3 + pages))
    content_ids = list(range(3 + pages, 3 + 2 * pages))
    font_id = 3 + 2 * pages

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{i} 0 R".encode() for i in page_ids)
            + f"] /Count {pages} >>".encode()
        ),
    ]
    for page_id, content_id in zip(page_ids, content_ids):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
    for body in page_bodies:
        objects.append(
            b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"endstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def build_minimal_pdf(text: str | None = "Hello Book Content", *, pages: int = 1) -> bytes:
    """Byte-exact minimal PDF that ``pypdf`` really parses.

    The legacy ``b"%PDF-1.4 fake pdf content"`` fixtures satisfy the
    teacher-material prefix check but cannot survive the administrator
    book intake probe (a real ``pypdf`` parse plus an extractable-text
    requirement), so book tests build structurally valid documents here
    instead.

    ``text=None`` produces an image-only style page: a content stream
    with no text-showing operator, so extraction yields no text.
    """
    content_ops: list[bytes] = []
    for _ in range(pages):
        if text is None:
            content_ops.append(b"")  # a page with a stream but no text
        else:
            escaped = _escape_pdf_text(text)
            content_ops.append(b"BT /F1 12 Tf 72 720 Td (" + escaped + b") Tj ET\n")
    return _assemble_pdf(content_ops)


def build_multiline_pdf(pages: list[list[str]]) -> bytes:
    """One PDF page per inner list; each string is its own printed line.

    Extraction fixtures need real multi-line layout (``pypdf`` emits one
    line per separate ``Td``/``Tj`` write, which is what lets a parser
    see chapter headings, body text and exercise lines separately), and
    a single ``build_minimal_pdf`` page can only carry one line. Each
    ``Td``/``Tj`` operator pair is whitespace-separated exactly as real
    PDF producers emit them — pypdf's content parser mis-tokenises
    missing separators (``Tj0``), which is a property of pypdf, not of
    the document under test. Text is latin-1/ASCII like the minimal
    builder; an empty inner list yields an empty (but present) content
    stream.
    """
    bodies: list[bytes] = []
    for lines in pages:
        ops: list[bytes] = [b"BT /F1 12 Tf 72 720 Td"]
        for index, line in enumerate(lines):
            if index:
                ops.append(b"0 -16 Td")
            ops.append(b"(" + _escape_pdf_text(line) + b") Tj")
        ops.append(b"ET")
        bodies.append((b" ".join(ops) + b"\n") if lines else b"")
    return _assemble_pdf(bodies)


def build_outline_pdf(
    pages: list[list[str]],
    outline: list[tuple[str, int, list[tuple[str, int]] | None]],
) -> bytes:
    """``build_multiline_pdf`` plus a real ``/Outlines`` bookmark tree.

    ``outline`` entries are ``(title, zero_based_page_index, children)``
    and become the PDF outline pypdf returns as ``Destination`` objects:
    the extraction pipeline prefers that authoritative structure over
    printed headings, so fixtures must be able to provide it. Requires
    at least one page; child lists may be ``None``.
    """
    reader = PdfReader(io.BytesIO(build_multiline_pdf(pages)))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for title, page_index, children in outline:
        parent = writer.add_outline_item(title, page_index)
        for child_title, child_page in children or ():
            writer.add_outline_item(child_title, child_page, parent=parent)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def grant_teaching_approval(
    session: Session,
    teacher,
    *,
    learning_context_ids: Iterable[uuid_module.UUID],
    category: str = "formal_teacher",
):
    """Give one teacher the durable approval state the policy enforcement reads.

    ``teachers.verification_status = 'approved'`` alone is no longer enough:
    :func:`app.services.teaching_authorization` additionally requires an
    approved verification category and a completed ``approved`` application,
    and every teaching surface (publishing, materials, curriculum, classes,
    classroom, marketplace, enrollment) requires the offering's canonical
    context to sit in ``teacher_approved_contexts``.

    Test setups that create a teacher and an offering directly (bypassing
    the application HTTP flow) must call this helper so the created state
    is one the policy would actually grant. Creates the
    ``TeacherApplication`` (approved, decided), one approved
    ``TeacherApplicationContext`` per context, one
    ``TeacherApprovedContext`` row per context, and sets the profile's
    ``verification_status``/``verification_category``. Works on both the
    unit-test in-memory SQLite sessions and the integration PostgreSQL
    sessions. Returns the created application row.
    """
    from app.models.enums import (
        ApplicationContextStatus,
        TeacherApplicationStatus,
        TeacherVerificationStatus,
    )
    from app.models.teacher_application import TeacherApplication
    from app.models.teacher_application_context import TeacherApplicationContext
    from app.models.teacher_approved_context import TeacherApprovedContext

    context_ids = list(learning_context_ids)
    now = datetime.now(timezone.utc)
    application = session.scalar(
        select(TeacherApplication).where(
            TeacherApplication.teacher_id == teacher.id,
            TeacherApplication.status == TeacherApplicationStatus.APPROVED.value,
        )
    )
    if application is None:
        application = TeacherApplication(
            teacher_id=teacher.id,
            status=TeacherApplicationStatus.APPROVED.value,
            category=category,
            submitted_at=now,
            decided_at=now,
            created_by=teacher.user_id,
        )
        session.add(application)
        session.flush()
    existing = {
        row[0]
        for row in session.execute(
            select(TeacherApprovedContext.learning_context_id).where(
                TeacherApprovedContext.teacher_id == teacher.id
            )
        )
    }
    for context_id in context_ids:
        if context_id in existing:
            continue
        session.add(
            TeacherApplicationContext(
                application_id=application.id,
                learning_context_id=context_id,
                status=ApplicationContextStatus.APPROVED.value,
                decided_at=now,
            )
        )
        session.add(
            TeacherApprovedContext(
                teacher_id=teacher.id,
                learning_context_id=context_id,
                application_id=application.id,
                approved_at=now,
            )
        )
    teacher.verification_status = TeacherVerificationStatus.APPROVED.value
    teacher.verification_category = category
    session.flush()
    return application


def approve_teacher_for_payloads(
    session: Session,
    user,
    payloads: Iterable,
) -> None:
    """Full policy approval for a teacher who will offer these payloads.

    Resolves each ``TeachingOfferingCreate``-shaped payload to its
    canonical ``learning_contexts`` row (creating it the first time, the
    same way ``create_offering`` would), then applies
    :func:`grant_teaching_approval` for every resolved context. Call this
    in a test setup AFTER the teacher profile exists and BEFORE invoking
    ``create_offering`` (or the HTTP publish endpoint).
    """
    from app.models.teacher import Teacher
    from app.services import learning_context_service

    profile = session.scalar(select(Teacher).where(Teacher.user_id == user.id))
    if profile is None:
        raise RuntimeError("no teacher profile for this user")
    context_ids: list[uuid_module.UUID] = []
    for payload in payloads:
        context = learning_context_service.resolve_context(session, payload)
        if context.id not in context_ids:
            context_ids.append(context.id)
    grant_teaching_approval(session, profile, learning_context_ids=context_ids)


def _guarded_test_url() -> str:
    """Return the SQLAlchemy URL for integration tests, or raise.

    The rule is intentionally simple and strict:
      ENVIRONMENT == "testing"  AND  DB_NAME ends with "_test"
    (an explicit DATABASE_URL is honored but must also point at a *_test
    database; production is always refused).
    """
    # Read config fresh, without the cached app settings, so environment
    # overrides set by tests/CI apply deterministically.
    from app.core.config import Settings

    settings = Settings(
        _env_file=os.environ.get("SUPERTEACHER_TEST_ENV_FILE", ".env"),
        ENVIRONMENT=os.environ.get("ENVIRONMENT", ""),
        DB_NAME=os.environ.get("DB_NAME", ""),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
    )
    environment = settings.ENVIRONMENT.lower()
    if environment == "production":
        raise UnsafeTestDatabase("ENVIRONMENT=production — integration tests are refused.")
    if environment != "testing":
        raise UnsafeTestDatabase(
            f"ENVIRONMENT must be 'testing' for integration tests (got {environment!r})."
        )
    db_name = settings.DB_NAME
    if not db_name.endswith("_test"):
        raise UnsafeTestDatabase(
            f"DB_NAME must end with '_test' for integration tests (got {db_name!r}). "
            "Create a dedicated test database, e.g.: CREATE DATABASE super_teacher_db_test;"
        )
    return settings.database_url


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    """Session-scoped engine for the guarded PostgreSQL test database."""
    try:
        url = _guarded_test_url()
    except UnsafeTestDatabase as exc:
        pytest.skip(f"integration test database not configured: {exc}")
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    # The test database must already have the schema applied (alembic upgrade head).
    try:
        inspector = inspect(engine)
        missing = [t for t in APPLICATION_TABLES if t not in set(inspector.get_table_names())]
    except Exception as exc:
        engine.dispose()
        pytest.skip(
            f"test database not reachable ({exc}); create it and apply the schema first"
        )
    if missing:
        engine.dispose()
        pytest.skip(
            f"test database schema incomplete (missing {missing}); "
            "run 'alembic upgrade head' against it first"
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def clean_db(pg_engine: Engine) -> Iterator[Engine]:
    """Provide an empty test database for the duration of one test.

    Truncates all application tables before the test and again in the
    finalizer after it. The finalizer runs whether the test passed OR
    failed, so a failing assertion can neither leak rows into the next
    test nor trigger a teardown error of its own.
    """
    with pg_engine.begin() as connection:
        for table in APPLICATION_TABLES:
            connection.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))
    yield pg_engine
    with pg_engine.begin() as connection:
        for table in APPLICATION_TABLES:
            connection.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))


@pytest.fixture()
def db_session(pg_engine: Engine) -> Iterator[Session]:
    """A session bound to an outer transaction that is always rolled back.

    Everything the test does through this session — including ``commit()``
    on the nested SAVEPOINT — is discarded at teardown.
    """
    connection = pg_engine.connect()
    transaction = connection.begin()
    # autoflush=False matches app.core.database.SessionLocal: production code
    # must not depend on SQLAlchemy silently pushing pending rows mid-query.
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        autoflush=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def _reset_rate_limiter(request: pytest.FixtureRequest) -> Iterator[None]:
    """Give every integration test a fresh slowapi bucket.

    All integration tests share one remote address (127.0.0.1), so the
    in-memory limiter tokens would leak across tests — e.g. five registers
    in an early test would 429 every later register. Unit tests are
    unaffected (no HTTP traffic through the limiter).
    """
    if request.node.get_closest_marker("integration"):
        from app.core.rate_limit import limiter

        limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _class_end_sweeper_idle() -> Iterator[None]:
    """Keep the slice 3E background class-end sweeper out of every test.

    The sweeper starts with the application lifespan, which the
    integration fixtures enter (``with TestClient(app)``). Left running it
    would race tests that deliberately make a class overdue and assert a
    specific transition — the same isolation the stale-socket timeout gets
    from its own fixture. Tests that exercise automatic ending start the
    sweeper explicitly (after shrinking its interval) or call
    ``sweep_once()`` directly, so every sweep in the suite is deliberate.
    """
    from app.services.class_end_sweeper import class_end_sweeper

    class_end_sweeper.stop()
    yield
    class_end_sweeper.stop()


@pytest.fixture(autouse=True)
def _book_extraction_sweeper_idle() -> Iterator[None]:
    """Keep the Phase 4 background book-extraction sweeper out of every test.

    Same isolation as the class-end sweeper above, same reason: the
    extraction sweep runs on the application lifespan, and a thread that
    extracts ``uploaded`` revisions in the background would race tests
    that deliberately place a revision in a specific state and assert one
    specific transition (or one specific row count). Extraction tests
    drive :meth:`sweep_once` directly, so every sweep in the suite is
    deliberate.
    """
    from app.services.book_extraction_sweeper import book_extraction_sweeper

    book_extraction_sweeper.stop()
    yield
    book_extraction_sweeper.stop()
