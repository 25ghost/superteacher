"""Unit tests: the seed CLI entry point (no PostgreSQL required).

``main`` is exercised with ``--dry-run`` (which performs zero database
statements) and, for the validation-failure path, before any connection is
opened.
"""
from __future__ import annotations

import pytest

import importlib
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

seed_cli = importlib.import_module("scripts.seed_reference_data") if (
    BACKEND_DIR / "scripts" / "__init__.py"
).exists() else None

if seed_cli is None:
    # scripts/ is not a package; load the file directly by path.
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "seed_reference_data", BACKEND_DIR / "scripts" / "seed_reference_data.py"
    )
    seed_cli = importlib.util.module_from_spec(_spec)
    sys.modules.setdefault("seed_reference_data", seed_cli)
    _spec.loader.exec_module(seed_cli)


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch: pytest.MonkeyPatch):
    """Keep this unit test away from PostgreSQL.

    ``seed_reference_data.main()`` opens ``SessionLocal()`` on every run
    (dry-run included), so the factory is replaced with an in-memory SQLite
    session that is schema-identical and empty — projections read 0s and no
    real connection can be reached.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.database import Base
    import app.models  # noqa: F401  (registers every table)

    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(seed_cli, "SessionLocal", sessionmaker(bind=engine))
    try:
        yield
    finally:
        engine.dispose()


def test_dry_run_report_is_internally_consistent(capsys) -> None:
    """Dry-run projects what a live run would do; zero statements execute.

    The projected total must equal the sum of the per-dataset
    inserted+updated counts in the same report. The absolute number depends
    on live database state (0 after a load, N on an empty database) — the
    invariant under test is the report's internal consistency, plus the
    "database was not modified" guarantee. Zero-executed-statements against
    PostgreSQL is proven by the integration suite's dry-run test.
    """
    import re

    exit_code = seed_cli.main(["--dry-run"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Mode: DRY RUN" in output
    assert "Dry run complete" in output
    assert "the database was not modified" in output

    per_dataset = re.findall(
        r"^\S+\s+records=\d+\s+inserted=(\d+)\s+updated=(\d+)\s+unchanged=\d+",
        output,
        flags=re.MULTILINE,
    )
    projected = sum(int(ins) + int(upd) for ins, upd in per_dataset)
    total = int(re.search(r"Database writes: (\d+)", output).group(1))
    assert total == projected
    # Every dataset appears in the report.
    for name in (
        "academic_years", "pathways", "education_levels", "pathway_levels",
        "subjects", "programs", "tvet_sectors", "tvet_programs",
        "program_versions", "program_subjects", "schools", "school_programs",
    ):
        assert name in output


def test_protected_tables_are_declared_in_output(capsys) -> None:
    exit_code = seed_cli.main(["--dry-run"])
    output = capsys.readouterr().out
    assert exit_code == 0
    for table in ("users", "students", "student_enrollments", "student_subjects"):
        assert table in output


def test_validation_failure_exits_nonzero_without_writes(monkeypatch, capsys) -> None:
    from app.data.registry import load_registry

    datasets = load_registry()
    datasets[0].records.append({"name": "dup", "start_date": "2026-01-01",
                                "end_date": "2026-12-31", "status": "planned"})
    datasets[0].records.append({"name": "dup", "start_date": "2027-01-01",
                                "end_date": "2027-12-31", "status": "planned"})
    try:
        exit_code = seed_cli.main(["--dry-run"])
    finally:
        datasets[0].records.clear()
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "PROBLEM" in output
    assert "No database writes performed." in output
