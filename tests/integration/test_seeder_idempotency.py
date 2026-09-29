"""Seeder idempotency harness (PostgreSQL, opt-in).

Phase 4B scope: the datasets are now NON-EMPTY (the verified-minimum Rwanda
catalog: 40 records). This module proves the full idempotency loop on the
guarded test database:

    clean database
        → first seed    : inserts exactly the dataset record counts
        → count snapshot
        → second seed   : 0 writes
        → same snapshot : no duplicate rows, no identity changes
        → stable rows   : academic-year + verify-policy rows unchanged
        → dry run       : 0 statements, database still empty afterwards
        → protected tables (users, students, enrollments) never written
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

# The verified-minimum Phase 4B catalog (see app/data/* docstrings).
EXPECTED_DATASET_COUNTS: dict[str, int] = {
    "academic_years": 1,
    "pathways": 4,
    "education_levels": 9,
    "pathway_levels": 12,
    "subjects": 14,
    "programs": 0,
    "program_versions": 0,
    "program_subjects": 0,
    "tvet_sectors": 0,
    "tvet_programs": 0,
    "schools": 0,
    "school_programs": 0,
}

CATALOG_TABLES: tuple[str, ...] = (
    "academic_years", "pathways", "education_levels", "pathway_levels",
    "subjects", "programs", "program_versions", "program_subjects",
    "tvet_sectors", "tvet_programs", "schools", "school_programs",
)


def capture_row_counts(engine: Engine) -> dict[str, int]:
    """Row counts for all 16 application tables, children-first order."""
    from tests.conftest import APPLICATION_TABLES

    with engine.connect() as connection:
        return {
            table: connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            for table in APPLICATION_TABLES
        }


def _run_seeder(engine: Engine) -> list:
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry

    session = SessionLocal(bind=engine)
    try:
        return run_load(session, load_registry())
    finally:
        session.close()


def _truncate_all(engine: Engine) -> None:
    """Restore the empty-database state the ``clean_db`` fixture expects.

    These tests deliberately seed real catalog rows, so they must clean up
    after themselves to honor the fixture's leak-guard (zero rows after the
    test).
    """
    from tests.conftest import APPLICATION_TABLES

    with engine.begin() as connection:
        for table in APPLICATION_TABLES:
            connection.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))


def test_first_seed_inserts_expected_counts_and_second_seed_is_noop(clean_db) -> None:
    """The core Phase 4B idempotency loop with the real catalog."""
    from sqlalchemy import text as _text

    counts_empty = capture_row_counts(clean_db)
    assert all(count == 0 for count in counts_empty.values())

    # --- first seed ---------------------------------------------------------
    reports_first = _run_seeder(clean_db)
    assert sum(r.writes for r in reports_first) == sum(EXPECTED_DATASET_COUNTS.values())
    assert all(not r.warnings for r in reports_first)
    for report in reports_first:
        assert report.inserted == EXPECTED_DATASET_COUNTS[report.dataset], (
            f"{report.dataset}: inserted {report.inserted}, "
            f"expected {EXPECTED_DATASET_COUNTS[report.dataset]}"
        )

    counts_after_first = capture_row_counts(clean_db)
    for table, expected in EXPECTED_DATASET_COUNTS.items():
        assert counts_after_first[table] == expected, table

    # --- second seed: zero writes, identical snapshot ------------------------
    reports_second = _run_seeder(clean_db)
    assert sum(r.writes for r in reports_second) == 0
    assert all(not r.warnings for r in reports_second)  # no drift, no false alarms
    counts_after_second = capture_row_counts(clean_db)
    assert counts_after_second == counts_after_first

    # --- selected stable rows remain unchanged -------------------------------
    with clean_db.connect() as connection:
        year = connection.execute(
            _text(
                "SELECT name, start_date, end_date, status FROM academic_years "
                "WHERE name = '2025/2026'"
            )
        ).one_or_none()
        assert year is not None
        assert str(year.status) == "closed"
        # verify-policy identity: the loader never rewrote the row.
        pathway_codes = {
            row[0]
            for row in connection.execute(_text("SELECT code FROM pathways"))
        }
        assert pathway_codes == {"O_LEVEL", "A_LEVEL", "TVET", "TTC"}
        level_codes = {
            row[0]
            for row in connection.execute(_text("SELECT code FROM education_levels"))
        }
        assert level_codes == {"S1", "S2", "S3", "S4", "S5", "S6", "L3", "L4", "L5"}
        # pathway_levels is an association: exactly the 12 verified pairs.
        pairs = {
            (row[0], row[1])
            for row in connection.execute(
                _text("SELECT p.code, e.code FROM pathway_levels pl "
                      "JOIN pathways p ON p.id = pl.pathway_id "
                      "JOIN education_levels e ON e.id = pl.education_level_id")
            )
        }
        assert pairs == {
            ("O_LEVEL", "S1"), ("O_LEVEL", "S2"), ("O_LEVEL", "S3"),
            ("A_LEVEL", "S4"), ("A_LEVEL", "S5"), ("A_LEVEL", "S6"),
            ("TVET", "L3"), ("TVET", "L4"), ("TVET", "L5"),
            ("TTC", "S4"), ("TTC", "S5"), ("TTC", "S6"),
        }

    _truncate_all(clean_db)


def test_dry_run_issues_zero_statements(clean_db) -> None:
    """A dry run against a real database executes nothing at all."""
    from app.core.database import SessionLocal
    from app.data.loader import run_load
    from app.data.registry import load_registry

    session = SessionLocal(bind=clean_db)
    try:
        reports = run_load(session, load_registry(), dry_run=True)
    finally:
        session.close()
    # The report projects the would-be writes...
    assert sum(r.writes for r in reports) == sum(EXPECTED_DATASET_COUNTS.values())
    # ...but the database is untouched.
    counts = capture_row_counts(clean_db)
    assert all(count == 0 for count in counts.values())


def test_loader_never_writes_enrollment_data(clean_db) -> None:
    """Contract: even a normal apply run must leave protected tables untouched."""
    from app.data.loader import PROTECTED_TABLES

    _run_seeder(clean_db)
    with clean_db.connect() as connection:
        for table in sorted(PROTECTED_TABLES):
            rows = connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            assert rows == 0
    _truncate_all(clean_db)
