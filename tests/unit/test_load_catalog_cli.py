"""Unit tests: load_catalog CLI (no PostgreSQL required).

Proves dry-run-by-default, whole-run validation before any write (the
session is never even opened on a parse/validation error), confirmation
requirements, the shared production guard with --allow-production, the
counts-only report line, and that missing input rows are reported but
never deleted.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401
from scripts.apply_guard import PRODUCTION_APPLY_REFUSAL
from scripts.load_catalog import main
from tests.unit.test_csv_input import _csv_dir


@pytest.fixture()
def sqlite_engine(monkeypatch: pytest.MonkeyPatch):
    """In-memory database wired in place of scripts.load_catalog.SessionLocal."""
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "scripts.load_catalog.SessionLocal", sessionmaker(bind=engine)
    )
    # These tests cover CLI behavior with SQLite. PostgreSQL target
    # verification is exercised separately against mocks and the live fixture.
    monkeypatch.setattr("scripts.load_catalog._verify_target", Mock())
    try:
        yield engine
    finally:
        engine.dispose()


class _FakeSettings:
    def __init__(self, environment: str = "development") -> None:
        self.ENVIRONMENT = environment
        self.DB_NAME = "super_teacher_db"
        self.DB_HOST = "127.0.0.1"
        self.DB_PORT = 5432


class _FakeTTY:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _valid_dir(tmp_path: Path) -> Path:
    return _csv_dir(tmp_path, {"pathways.csv": "code,name\nTEST-OL,Test O-Level\n"})


def _count(engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text(f'SELECT count(*) FROM "{table}"')
        ).scalar_one()


def _refuse_session(monkeypatch: pytest.MonkeyPatch) -> Mock:
    factory = Mock(side_effect=AssertionError("SessionLocal must not be called"))
    monkeypatch.setattr("scripts.load_catalog.SessionLocal", factory)
    return factory


def _capture_write_statements(engine) -> list[str]:
    """Record every INSERT/UPDATE/DELETE executed through this engine."""
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        keyword = statement.strip().split(None, 1)[0].upper()
        if keyword in {"INSERT", "UPDATE", "DELETE"}:
            statements.append(statement)

    return statements


def test_dry_run_is_the_default_and_writes_nothing(
    sqlite_engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--csv-dir", str(_valid_dir(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Mode: DRY RUN" in out
    assert "CSV parse: OK (1 record(s) across 12 files)" in out
    assert "Dataset validation: OK" in out
    assert "Projected writes: 1 (dry run — no statements issued)" in out
    assert "Dry run complete — the database was not modified." in out
    assert _count(sqlite_engine, "pathways") == 0


def test_parse_error_exits_1_without_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _refuse_session(monkeypatch)
    csv_dir = _csv_dir(tmp_path, {"schools.csv": "school_code,name\n,Test School\n"})
    rc = main(["--csv-dir", str(csv_dir)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "CSV PARSE FAILED" in out
    assert "schools.csv: line 2, column 'school_code'" in out
    factory.assert_not_called()


def test_validation_error_exits_1_without_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The structural gate fires before a session exists — zero writes."""
    factory = _refuse_session(monkeypatch)
    csv_dir = _csv_dir(
        tmp_path, {"subjects.csv": "code,name\nTEST-SUB,One\nTEST-SUB,Two\n"}
    )
    rc = main(["--csv-dir", str(csv_dir)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Dataset validation: 1 PROBLEM(S)" in out
    assert "duplicate natural key" in out
    assert "No database writes performed." in out
    factory.assert_not_called()


def test_missing_csv_dir_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _refuse_session(monkeypatch)
    rc = main(["--csv-dir", str(tmp_path / "nope")])
    assert rc == 1
    assert "CSV PARSE FAILED" in capsys.readouterr().out
    factory.assert_not_called()


def test_unexpected_dry_run_exception_exits_1_with_counts_only_output(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-ValidationError blow-up in the dry run: exit 1, zero writes,
    and the failure message carries no record payloads."""
    monkeypatch.setattr(
        "scripts.load_catalog.run_load",
        Mock(side_effect=RuntimeError("unexpected dry-run failure")),
    )
    write_statements = _capture_write_statements(sqlite_engine)

    rc = main(["--csv-dir", str(_valid_dir(tmp_path)), "--apply"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "DRY RUN FAILED — no database writes performed." in out
    assert "unexpected dry-run failure" not in out
    assert "Dataset validation: OK" in out
    # counts-only output: no row contents anywhere, including the message
    assert "TEST-OL" not in out
    assert "Test O-Level" not in out
    assert "records=" not in out
    assert write_statements == []
    assert _count(sqlite_engine, "pathways") == 0


def test_apply_without_confirmation_is_refused(
    sqlite_engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """pytest stdin is not a TTY and no env var is set → exit 1, no writes."""
    rc = main(["--csv-dir", str(_valid_dir(tmp_path)), "--apply"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Not confirmed — no database writes performed." in out
    assert "CATALOG_LOAD_CONFIRM=APPLY" in out
    assert _count(sqlite_engine, "pathways") == 0


def test_apply_with_env_confirmation_loads_and_reruns_as_noop(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATALOG_LOAD_CONFIRM", "APPLY")
    csv_dir = _valid_dir(tmp_path)

    rc = main(["--csv-dir", str(csv_dir), "--apply"])
    first = capsys.readouterr().out
    assert rc == 0
    assert "Database writes: 1" in first
    assert _count(sqlite_engine, "pathways") == 1

    rc = main(["--csv-dir", str(csv_dir), "--apply"])
    second = capsys.readouterr().out
    assert rc == 0
    assert "Database writes: 0" in second
    assert "inserted=0" in second
    assert "unchanged=1" in second
    assert _count(sqlite_engine, "pathways") == 1


def test_counts_line_is_counts_only_and_in_order(
    sqlite_engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["--csv-dir", str(_valid_dir(tmp_path))])
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("pathways")]
    assert len(lines) == 1
    assert "records=1" in lines[0]
    assert "inserted=1" in lines[0]
    assert "updated=0" in lines[0]
    assert "unchanged=0" in lines[0]
    assert "missing=0" in lines[0]
    # no record payloads in the report lines
    assert "TEST-OL" not in lines[0]


def test_missing_input_rows_are_reported_but_never_deleted(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATALOG_LOAD_CONFIRM", "APPLY")
    csv_dir = _valid_dir(tmp_path)
    assert main(["--csv-dir", str(csv_dir), "--apply"]) == 0
    capsys.readouterr()

    # Same directory, now with an empty pathways file: the DB row is
    # absent from input — reported as missing, left untouched.
    (csv_dir / "pathways.csv").write_text("code,name\n", encoding="utf-8")
    rc = main(["--csv-dir", str(csv_dir), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "missing from input: TEST-OL" in out
    assert "missing=1" in out
    assert _count(sqlite_engine, "pathways") == 1


def test_tty_confirmation_accepts_apply(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeTTY(True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "APPLY")
    rc = main(["--csv-dir", str(_valid_dir(tmp_path)), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Database writes: 1" in out
    assert _count(sqlite_engine, "pathways") == 1


def test_tty_confirmation_rejects_anything_but_apply(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeTTY(True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "apply")
    rc = main(["--csv-dir", str(_valid_dir(tmp_path)), "--apply"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Not confirmed" in out
    assert _count(sqlite_engine, "pathways") == 0


@pytest.mark.parametrize(
    "error",
    [EOFError, KeyboardInterrupt],
    ids=["eof", "keyboard-interrupt"],
)
def test_production_confirmation_interrupted_exits_1_with_no_writes_or_commit(
    error: type[BaseException],
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF or Ctrl-C at the production TTY prompt: exit 1, nothing persisted."""
    monkeypatch.setattr(
        "scripts.load_catalog.get_settings",
        lambda: _FakeSettings("production"),
    )
    monkeypatch.delenv("CATALOG_LOAD_CONFIRM", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeTTY(True))
    monkeypatch.setattr("builtins.input", Mock(side_effect=error()))

    write_statements = _capture_write_statements(sqlite_engine)
    commits: list = []

    def _record_commit(session: Session) -> None:
        commits.append(session)

    event.listen(Session, "after_commit", _record_commit)
    try:
        rc = main(
            [
                "--csv-dir",
                str(_valid_dir(tmp_path)),
                "--apply",
                "--allow-production",
            ]
        )
    finally:
        event.remove(Session, "after_commit", _record_commit)

    out = capsys.readouterr().out
    assert rc == 1
    assert "Not confirmed — no database writes performed." in out
    assert write_statements == []
    assert commits == []
    assert _count(sqlite_engine, "pathways") == 0


def test_wrong_confirmation_env_value_is_refused(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATALOG_LOAD_CONFIRM", "yes")
    rc = main(["--csv-dir", str(_valid_dir(tmp_path)), "--apply"])
    assert rc == 1
    assert "Not confirmed" in capsys.readouterr().out
    assert _count(sqlite_engine, "pathways") == 0


def test_production_apply_is_refused_without_allow_production(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.load_catalog.get_settings",
        lambda: _FakeSettings("production"),
    )
    monkeypatch.setenv("CATALOG_LOAD_CONFIRM", "APPLY")
    rc = main(["--csv-dir", str(_valid_dir(tmp_path)), "--apply"])
    out = capsys.readouterr().out
    assert rc == 1
    assert PRODUCTION_APPLY_REFUSAL in out
    assert "--allow-production" in out
    assert _count(sqlite_engine, "pathways") == 0


def test_production_apply_is_refused_case_insensitively(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.load_catalog.get_settings",
        lambda: _FakeSettings("PRODUCTION"),
    )
    rc = main(["--csv-dir", str(_valid_dir(tmp_path)), "--apply"])
    assert rc == 1
    assert PRODUCTION_APPLY_REFUSAL in capsys.readouterr().out


def test_production_apply_allowed_with_allow_production(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.load_catalog.get_settings",
        lambda: _FakeSettings("production"),
    )
    monkeypatch.setenv("CATALOG_LOAD_CONFIRM", "APPLY")
    rc = main(
        ["--csv-dir", str(_valid_dir(tmp_path)), "--apply", "--allow-production"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Mode: APPLY" in out
    assert "Database writes: 1" in out
    assert _count(sqlite_engine, "pathways") == 1


def test_production_dry_run_is_allowed_without_any_flag(
    sqlite_engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.load_catalog.get_settings",
        lambda: _FakeSettings("production"),
    )
    rc = main(["--csv-dir", str(_valid_dir(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Dry run complete" in out
    assert _count(sqlite_engine, "pathways") == 0


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [
        ("conflicting_name", "DB_NAME and the effective database name differ"),
        ("wrong_binding", "session binding differs"),
        ("wrong_database", "connected database or role differs"),
        ("wrong_role", "connected database or role differs"),
        ("unreachable", "PostgreSQL availability"),
        ("invalid_url", "effective connection configuration"),
        ("ambiguous_hosts", "single explicit PostgreSQL host"),
        ("routing_override", "connection routing overrides"),
    ],
)
def test_target_refusal_precedes_catalog_access(
    monkeypatch, tmp_path, capsys, failure, diagnostic,
) -> None:
    from sqlalchemy.engine import URL, make_url

    from app.core.config import Settings
    from scripts import load_catalog

    secret = "synthetic-password@:/?#"
    target = URL.create(
        "postgresql+psycopg", username="fixture", password=secret,
        host="configured-host", port=5545, database="intended_test",
    )
    url = target.render_as_string(hide_password=False)
    if failure == "conflicting_name":
        url = target.set(database="different_test").render_as_string(hide_password=False)
    elif failure == "invalid_url":
        url = url.replace(":5545/", ":invalid/")
    elif failure == "ambiguous_hosts":
        url = target.set(host="first,second").render_as_string(hide_password=False)
    elif failure == "routing_override":
        url += "?dbname=different_test"
    settings = Settings(_env_file=None, DB_NAME="intended_test", DATABASE_URL=url)

    session = Mock()
    session.get_bind.return_value.url = target
    connection = session.connection.return_value
    connection.execute.return_value.one.return_value = (
        "different_test" if failure == "wrong_database" else "intended_test",
        "fixture",
        "different_role" if failure == "wrong_role" else "fixture",
        "192.0.2.1", 5545,
    )
    if failure == "wrong_binding":
        session.get_bind.return_value.url = make_url(url).set(database="drift_test")
    if failure == "unreachable":
        session.connection.side_effect = RuntimeError(url)

    monkeypatch.setattr(load_catalog, "get_settings", lambda: settings)
    monkeypatch.setattr(load_catalog, "SessionLocal", Mock(return_value=session))
    run_load = Mock()
    monkeypatch.setattr(load_catalog, "run_load", run_load)
    monkeypatch.setenv("CATALOG_LOAD_CONFIRM", "APPLY")

    assert main(["--csv-dir", str(_valid_dir(tmp_path)), "--apply"]) == 1
    output = capsys.readouterr().out
    assert "Database target verification failed" in output
    assert diagnostic in output
    assert secret not in output
    assert target.render_as_string(hide_password=False) not in output
    assert "synthetic-password" not in output
    run_load.assert_not_called()
    session.commit.assert_not_called()
    session.close.assert_called_once()
    if failure in {"wrong_database", "wrong_role"}:
        connection.execute.assert_called_once()
        assert str(connection.execute.call_args.args[0]).startswith("SELECT ")
    elif failure != "unreachable":
        session.connection.assert_not_called()


def test_dry_run_displays_the_effective_target_with_encoded_password(monkeypatch, tmp_path, capsys):
    from sqlalchemy.engine import URL

    from app.core.config import Settings
    from scripts import load_catalog

    target = URL.create(
        "postgresql+psycopg", username="fixture", password="synthetic@:/?#",
        host="effective-host", port=5545, database="effective_test",
    )
    settings = Settings(
        _env_file=None, DB_NAME="legacy_test", DB_HOST="legacy-host",
        DATABASE_URL=target.render_as_string(hide_password=False),
    )
    session = Mock()
    session.get_bind.return_value.url = target
    session.connection.return_value.execute.return_value.one.return_value = (
        "effective_test", "fixture", "fixture", "192.0.2.1", 5545,
    )
    monkeypatch.setattr(load_catalog, "get_settings", lambda: settings)
    monkeypatch.setattr(load_catalog, "SessionLocal", Mock(return_value=session))
    monkeypatch.setattr(load_catalog, "run_load", Mock(return_value=[]))
    monkeypatch.setattr(load_catalog, "missing_from_input", Mock(return_value=[]))

    assert main(["--csv-dir", str(_valid_dir(tmp_path))]) == 0
    output = capsys.readouterr().out
    assert "Database: 'effective_test' via 'effective-host':5545" in output
    assert "user 'fixture', server endpoint 192.0.2.1:5545" in output
    assert "legacy-host" not in output
    assert "legacy_test" not in output
    assert "synthetic" not in output
    session.connection.assert_called_once()
    session.commit.assert_not_called()
