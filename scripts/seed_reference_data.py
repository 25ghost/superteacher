"""Reference-data seeder for the SuperTeacher backend.

Loads the shared education reference datasets (academic years, pathways,
education levels, subjects, programs, program versions, TVET catalog,
schools, school offerings) into PostgreSQL, idempotently and in explicit
foreign-key-safe order.

Whole-system framing: this is shared SuperTeacher reference-data
infrastructure, consumed first by the Student Registration module and
reused by future modules (Curriculum, Learning, Progress, ...). It never
touches users, students, enrollments, or student subjects.

Usage (from backend/):

    python scripts/seed_reference_data.py --dry-run   # report only, no writes
    python scripts/seed_reference_data.py             # load (empty datasets => 0 writes)

Datasets are intentionally EMPTY in Phase 4A; the authoritative Rwanda
catalog is added in a later phase through this same loader.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.data.loader import PROTECTED_TABLES, ValidationError, run_load  # noqa: E402
from app.data.registry import load_registry  # noqa: E402
from app.data.validation import validate_datasets  # noqa: E402
from scripts.apply_guard import production_apply_refusal  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SuperTeacher reference-data seeder (idempotent, FK-safe order).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report only; issue zero INSERT/UPDATE/DELETE statements",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "DRY RUN" if args.dry_run else "APPLY"
    print("SuperTeacher reference-data seeder")
    print(f"Mode: {mode}")
    settings = get_settings()
    print(f"Environment: {settings.ENVIRONMENT}")
    print(f"Database: {settings.DB_NAME} on {settings.DB_HOST}:{settings.DB_PORT}")

    # Production safety: refuse APPLY in production. Dry-run remains
    # available for read-only inspection. The predicate and the wording
    # live in scripts/apply_guard.py, shared with load_catalog.py.
    if not args.dry_run:
        refusal = production_apply_refusal(settings.ENVIRONMENT)
        if refusal:
            print("\n" + refusal)
            return 1

    datasets = load_registry()

    # Structural gate: always run, even in dry-run mode, so the report shows
    # what a real load would do — and whether it would be allowed at all.
    problems = validate_datasets(datasets)
    if problems:
        print(f"\nDataset validation: {len(problems)} PROBLEM(S)")
        for problem in problems:
            print(f"  - {problem}")
        print("No database writes performed.")
        return 1
    total_records = sum(len(dataset.records) for dataset in datasets)
    print(f"Dataset validation: OK ({total_records} record(s) across {len(datasets)} datasets)")

    session = SessionLocal()
    try:
        reports = run_load(session, datasets, dry_run=args.dry_run)
    except ValidationError as exc:
        print(f"\nVALIDATION FAILED — no database writes performed:\n{exc}")
        return 1
    except Exception as exc:
        print(f"\nSEED FAILED: {exc}")
        return 1
    finally:
        session.close()

    print()
    total_writes = 0
    for report in reports:
        suffix = " (dry run — no statements issued)" if report.skipped else ""
        print(
            f"{report.dataset:<18} records={report.records:<5} "
            f"inserted={report.inserted:<5} updated={report.updated:<5} "
            f"unchanged={report.unchanged}{suffix}"
        )
        for warning in report.warnings:
            print(f"    warning: {warning}")
        total_writes += 0 if report.skipped else report.writes

    print()
    print(f"Database writes: {total_writes}")
    if args.dry_run:
        print("Dry run complete — the database was not modified.")
    elif total_writes == 0:
        print("Nothing to load (all datasets empty or already present).")
    protected_note = ", ".join(sorted(PROTECTED_TABLES))
    print(f"Protected tables (never written): {protected_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
