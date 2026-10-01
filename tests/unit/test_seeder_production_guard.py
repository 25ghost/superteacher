"""Unit tests: seeder production safety guard.

Proves that the seeder refuses APPLY in production, allows dry-run in
production, and allows normal apply in development/testing — without
touching a real database.
"""
from __future__ import annotations

import pytest

from scripts.seed_reference_data import main


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch: pytest.MonkeyPatch):
    """Keep these unit tests away from PostgreSQL.

    The guard tests run ``main()`` past the environment gate (development,
    testing, production dry-run), which reaches ``SessionLocal()``; the
    factory is replaced with an in-memory SQLite session so no unit test can
    ever open a real database connection.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.database import Base
    import app.models  # noqa: F401  (registers every table)

    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "scripts.seed_reference_data.SessionLocal", sessionmaker(bind=engine)
    )
    try:
        yield
    finally:
        engine.dispose()


class _FakeSettings:
    """Minimal settings object for controlling the seeder guard."""

    def __init__(
        self,
        environment: str = "development",
        db_name: str = "super_teacher_db",
        db_host: str = "127.0.0.1",
        db_port: int = 5432,
    ) -> None:
        self.ENVIRONMENT = environment
        self.DB_NAME = db_name
        self.DB_HOST = db_host
        self.DB_PORT = db_port


def test_development_apply_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply in development environment should proceed (datasets empty → 0 writes)."""
    monkeypatch.setattr(
        "scripts.seed_reference_data.get_settings",
        lambda: _FakeSettings(environment="development"),
    )
    rc = main([])
    assert rc == 0


def test_testing_apply_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply in testing environment should proceed (datasets empty → 0 writes)."""
    monkeypatch.setattr(
        "scripts.seed_reference_data.get_settings",
        lambda: _FakeSettings(environment="testing"),
    )
    rc = main([])
    assert rc == 0


def test_production_apply_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply in production environment must be refused."""
    monkeypatch.setattr(
        "scripts.seed_reference_data.get_settings",
        lambda: _FakeSettings(environment="production"),
    )
    rc = main([])
    assert rc == 1


def test_production_dry_run_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run in production should be allowed (read-only inspection)."""
    monkeypatch.setattr(
        "scripts.seed_reference_data.get_settings",
        lambda: _FakeSettings(environment="production"),
    )
    rc = main(["--dry-run"])
    assert rc == 0


def test_production_apply_refused_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production guard must be case-insensitive (PRODUCTION = production)."""
    monkeypatch.setattr(
        "scripts.seed_reference_data.get_settings",
        lambda: _FakeSettings(environment="PRODUCTION"),
    )
    rc = main([])
    assert rc == 1


def test_staging_apply_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-production environments (e.g. staging) allow apply."""
    monkeypatch.setattr(
        "scripts.seed_reference_data.get_settings",
        lambda: _FakeSettings(environment="staging"),
    )
    rc = main([])
    assert rc == 0
