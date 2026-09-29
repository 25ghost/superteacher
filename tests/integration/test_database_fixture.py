"""Integration-test scaffolding verification (PostgreSQL, opt-in).

Run with:  python -m pytest -m integration

Requires:
    1. a dedicated test database:  CREATE DATABASE super_teacher_db_test;
    2. schema applied:             alembic upgrade head  (with DB_NAME=super_teacher_db_test)
    3. guarded environment:        ENVIRONMENT=testing, DB_NAME=super_teacher_db_test

Proves the fixture machinery: connection under the guard, a temporary
insert inside a rolled-back transaction, and a clean database afterwards.
No seed data is inserted; the disposable row is rolled back.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.integration


def test_guard_blocks_development_database(monkeypatch) -> None:
    from tests.conftest import UnsafeTestDatabase, _guarded_test_url

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DB_NAME", "super_teacher_db")
    with pytest.raises(UnsafeTestDatabase):
        _guarded_test_url()


def test_guard_blocks_production(monkeypatch) -> None:
    from tests.conftest import UnsafeTestDatabase, _guarded_test_url

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DB_NAME", "super_teacher_db_test")
    with pytest.raises(UnsafeTestDatabase):
        _guarded_test_url()


def test_guard_requires_test_suffix(monkeypatch) -> None:
    from tests.conftest import UnsafeTestDatabase, _guarded_test_url

    monkeypatch.setenv("ENVIRONMENT", "testing")
    monkeypatch.setenv("DB_NAME", "super_teacher_db")
    with pytest.raises(UnsafeTestDatabase):
        _guarded_test_url()


def test_guard_accepts_test_database(monkeypatch) -> None:
    from tests.conftest import _guarded_test_url

    monkeypatch.setenv("ENVIRONMENT", "testing")
    monkeypatch.setenv("DB_NAME", "super_teacher_db_test")
    monkeypatch.setenv("DATABASE_URL", "")
    url = _guarded_test_url()
    assert "super_teacher_db_test" in url


def test_transaction_rollback_leaves_no_rows(db_session) -> None:
    """A temporary insert committed inside the savepoint still rolls back."""
    from app.models.academic_year import AcademicYear
    from datetime import date

    db_session.add(AcademicYear(
        name="zz-disposable-fixture-probe",
        start_date=date(2000, 1, 1),
        end_date=date(2000, 12, 31),
        status="planned",
    ))
    db_session.commit()  # commits the SAVEPOINT, not the outer transaction

    found = db_session.execute(
        select(AcademicYear).filter_by(name="zz-disposable-fixture-probe")
    ).scalar_one_or_none()
    assert found is not None  # visible inside the transaction

    # After this test, conftest rolls back the outer transaction — the row
    # never reaches permanent storage. The clean_db-style post-check in
    # test_clean_db_leaves_zero_rows proves it.


def test_clean_db_leaves_zero_rows(clean_db) -> None:
    """After truncate + rollback of any test inserts: every table is empty."""
    from tests.conftest import APPLICATION_TABLES

    with clean_db.begin() as connection:
        for table in APPLICATION_TABLES:
            rows = connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            assert rows == 0, f"{table} should be empty, has {rows}"
