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

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect, text
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
