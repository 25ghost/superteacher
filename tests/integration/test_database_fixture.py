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

from unittest.mock import MagicMock, Mock

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


def test_guard_accepts_matching_postgresql_url(monkeypatch) -> None:
    from tests.conftest import _guarded_test_url

    url = "postgresql+psycopg://tester@localhost/super_teacher_db_test?sslmode=require"
    monkeypatch.setenv("ENVIRONMENT", "testing")
    monkeypatch.setenv("DB_NAME", "super_teacher_db_test")
    monkeypatch.setenv("DATABASE_URL", url)

    assert _guarded_test_url() == url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://tester@localhost/super_teacher_db",
        "postgresql+psycopg://tester@localhost/another_test",
        "postgresql+psycopg://tester@localhost/SUPER_TEACHER_DB_test",
        "postgresql+psycopg://tester@localhost/",
        "postgresql+psycopg://tester:dummy-password@localhost:bad/super_teacher_db_test",
        "sqlite:///super_teacher_db_test",
        "postgresql+unknown://tester@localhost/super_teacher_db_test",
        "postgresql+psycopg://tester@localhost/super_teacher_db_test?dbname=another_test",
    ],
)
def test_guard_rejects_unsafe_url_before_creating_an_engine(monkeypatch, url: str) -> None:
    from tests import conftest

    monkeypatch.setenv("ENVIRONMENT", "testing")
    monkeypatch.setenv("DB_NAME", "super_teacher_db_test")
    monkeypatch.setenv("DATABASE_URL", url)
    create_engine = Mock()
    monkeypatch.setattr(conftest, "create_engine", create_engine)

    with pytest.raises(conftest.UnsafeTestDatabase) as exc_info:
        next(conftest.pg_engine.__wrapped__())

    assert "dummy-password" not in str(exc_info.value)
    create_engine.assert_not_called()


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


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("offline", "PostgreSQL test setup failed"),
        ("server", "server identity differs"),
        ("role", "privileges outside"),
        ("revision", "expected Alembic head"),
    ],
)
def test_engine_preflight_fails_and_disposes(monkeypatch, failure, message) -> None:
    from app.core.config import Settings
    from tests import conftest

    url = "postgresql+psycopg://tester:dummy-password@localhost/super_teacher_db_test"
    monkeypatch.setenv("ENVIRONMENT", "testing")
    monkeypatch.setenv("DB_NAME", "super_teacher_db_test")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SUPERTEACHER_TEST_PG_SYSTEM_ID", "123")
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(_env_file=None))

    engine = Mock()
    connection = Mock()
    manager = MagicMock()
    manager.__enter__.return_value = connection
    engine.connect.return_value = manager
    if failure == "offline":
        engine.connect.side_effect = OSError(url)
    connection.execute.side_effect = [
        Mock(**{"one.return_value": (
            "super_teacher_db_test", "tester", "456" if failure == "server" else "123"
        )}),
        Mock(**{"scalar_one.return_value": failure != "role"}),
        Mock(**{"scalars.return_value": ["0001"]}),
    ]
    monkeypatch.setattr(conftest, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(
        conftest, "inspect", Mock(return_value=Mock(get_table_names=lambda: ["alembic_version"]))
    )

    with pytest.raises(conftest.UnsafeTestDatabase, match=message) as exc:
        conftest._verified_pg_engine()

    assert "dummy-password" not in str(exc.value)
    engine.dispose.assert_called_once_with()
    engine.begin.assert_not_called()
    assert manager.__exit__.called is (failure != "offline")


@pytest.mark.parametrize("real_commit", [False, True])
def test_fixture_cleanup_after_test_failure(pg_engine, real_commit) -> None:
    from tests import conftest

    name = "zz-explicit-fixture-cleanup"
    factory = conftest.clean_db if real_commit else conftest.db_session
    fixture = factory.__wrapped__(pg_engine)
    resource = next(fixture)
    insert = text("""
        INSERT INTO academic_years (id, name, start_date, end_date, status)
        VALUES (gen_random_uuid(), :name, '2000-01-01', '2000-12-31', 'planned')
    """)
    find = text("SELECT count(*) FROM academic_years WHERE name = :name")
    if real_commit:
        with resource.begin() as connection:
            connection.execute(insert, {"name": name})
    else:
        resource.execute(insert, {"name": name})
        resource.commit()
    with pg_engine.connect() as connection:
        assert connection.execute(find, {"name": name}).scalar_one() == int(real_commit)

    with pytest.raises(ValueError, match="synthetic test failure"):
        fixture.throw(ValueError("synthetic test failure"))

    with pg_engine.connect() as connection:
        assert connection.execute(find, {"name": name}).scalar_one() == 0


def test_fixture_postgresql_notifications_follow_real_commit(pg_engine) -> None:
    from sqlalchemy.orm import Session

    from app.realtime.event_bus import CHANNEL, RealtimeEventBus

    bus = RealtimeEventBus()
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as listener:
        listener.exec_driver_sql(f"LISTEN {CHANNEL}")
        driver = listener.connection.driver_connection
        try:
            with Session(pg_engine) as writer:
                bus.publish(writer, {"type": "fixture.rollback"})
                writer.rollback()
                assert list(driver.notifies(timeout=0.2)) == []

                bus.publish(writer, {"type": "fixture.commit"})
                assert list(driver.notifies(timeout=0.2)) == []
                writer.commit()
                notification, = driver.notifies(timeout=2, stop_after=1)
                assert notification.payload == '{"type":"fixture.commit","class_id":null}'
        finally:
            listener.exec_driver_sql(f"UNLISTEN {CHANNEL}")
