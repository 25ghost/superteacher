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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

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
