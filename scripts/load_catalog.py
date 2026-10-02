"""Catalog CSV loader for the SuperTeacher reference catalog (12 datasets).

Reads an operator-supplied directory of CSV files — one file per registry
dataset, format documented in README section "Catalog CSV loader" — and
loads it idempotently into PostgreSQL.

Safety properties:

- dry run by default; ``--apply`` performs the load, after printing the
  dry-run summary first and confirming (typing ``APPLY`` on a TTY, or
  ``CATALOG_LOAD_CONFIRM=APPLY`` for non-interactive runs);
- whole-run validation before any write: CSV parse errors and structural
  validation problems exit 1 with zero database writes;
- in ``ENVIRONMENT=production`` an apply additionally requires
  ``--allow-production`` (the seeder's refusal has no bypass — this
  script is the supported production path). The predicate and refusal
  wording are shared via ``scripts/apply_guard.py``;
- the engine's rules are used exactly as defined: natural keys are the
  match key and are never updated, ``update_policy`` decides mutable
  fields (``verify`` drift is warned, never rewritten), protected tables
  are never written, and nothing is ever deleted — database rows absent
  from the input are reported as "missing from input" and left untouched.

Transaction rule (explicit exception): this script owns its own
``commit()``/``rollback()`` — same as ``scripts/create_admin.py``. The
engine's ``single_transaction`` mode is flush-only for the whole run, so
one failing row rolls the entire load back; the commit happens here, in
``main()``, only after the whole run succeeded.

Exit codes: 0 success (including a read-only dry run), 1 any error —
parse error, validation problem, unconfirmed apply, refused production
apply, or a failed/rolled-back transaction.

Usage (from the repository root):

    python scripts/load_catalog.py --csv-dir path/to/csv                  # dry run
    python scripts/load_catalog.py --csv-dir path/to/csv --apply          # load
    CATALOG_LOAD_CONFIRM=APPLY python scripts/load_catalog.py --csv-dir path/to/csv --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.data.csv_input import (  # noqa: E402
    CsvParseError,
    load_csv_datasets,
    missing_from_input,
)
from app.data.loader import PROTECTED_TABLES, ValidationError, run_load  # noqa: E402
from app.data.validation import validate_datasets  # noqa: E402
from scripts.apply_guard import production_apply_refusal  # noqa: E402

#: Non-interactive confirmation source. Value must equal ``APPLY``.
CONFIRM_ENV = "CATALOG_LOAD_CONFIRM"
CONFIRM_PHRASE = "APPLY"

#: Cap for the per-dataset "missing from input" listing (full count is in
#: the counts line).
_MISSING_LIST_CAP = 20


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "SuperTeacher catalog CSV loader (dry run by default, "
            "idempotent, FK-safe order)."
        ),
    )
    parser.add_argument(
        "--csv-dir",
        required=True,
        metavar="PATH",
        help="directory holding one <dataset>.csv per registry dataset",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the load (default: dry run, zero writes)",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="required for --apply when ENVIRONMENT=production",
    )
    return parser.parse_args(argv)


def _confirm() -> bool:
    """True when the operator confirmed the apply (TTY or environment)."""
    if os.environ.get(CONFIRM_ENV) == CONFIRM_PHRASE:
        return True
    if sys.stdin.isatty():
        try:
            answer = input(f"Type {CONFIRM_PHRASE} to confirm the catalog load: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip() == CONFIRM_PHRASE
    return False


def _print_reports(reports, missing_reports, *, suffix: str = "") -> int:
    """Print one counts-only line per dataset (plus warnings/missing details).

    Returns the total number of writes (projected during a dry run).
    """
    missing_by_dataset = {
        report.dataset: report.missing for report in missing_reports
    }
    print()
    total_writes = 0
    for report in reports:
        missing_keys = missing_by_dataset.get(report.dataset, [])
        print(
            f"{report.dataset:<18} records={report.records:<5} "
            f"inserted={report.inserted:<5} updated={report.updated:<5} "
            f"unchanged={report.unchanged:<5} missing={len(missing_keys)}{suffix}"
        )
        for warning in report.warnings:
            print(f"    warning: {warning}")
        for key in missing_keys[:_MISSING_LIST_CAP]:
            print(f"    missing from input: {key}")
        if len(missing_keys) > _MISSING_LIST_CAP:
            print(
                f"    missing from input: ... and "
                f"{len(missing_keys) - _MISSING_LIST_CAP} more"
            )
        total_writes += report.writes
    print()
    return total_writes


def _print_protected_note() -> None:
    print(f"Protected tables (never written): {', '.join(sorted(PROTECTED_TABLES))}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    csv_dir = Path(args.csv_dir)
    settings = get_settings()
    mode = "APPLY" if args.apply else "DRY RUN"
    print("SuperTeacher catalog CSV loader")
    print(f"Mode: {mode}")
    print(f"Environment: {settings.ENVIRONMENT}")
    print(f"Database: {settings.DB_NAME} on {settings.DB_HOST}:{settings.DB_PORT}")
    print(f"CSV directory: {csv_dir}")

    # Production safety (predicate and wording shared with the seeder).
    if args.apply:
        refusal = production_apply_refusal(settings.ENVIRONMENT)
        if refusal and not args.allow_production:
            print("\n" + refusal)
            print(
                "scripts/load_catalog.py is the supported production path: "
                "pass --allow-production to apply in production."
            )
            return 1

    # 1. Parse — before any session exists, so parse errors cannot write.
    try:
        datasets = load_csv_datasets(csv_dir)
    except CsvParseError as exc:
        print(f"\nCSV PARSE FAILED — no database writes performed:\n{exc}")
        return 1
    total_records = sum(len(dataset.records) for dataset in datasets)
    print(f"CSV parse: OK ({total_records} record(s) across {len(datasets)} files)")

    # 2. Structural gate — whole-run validation before any write (the
    # engine enforces the same gate again inside run_load).
    problems = validate_datasets(datasets)
    if problems:
        print(f"\nDataset validation: {len(problems)} PROBLEM(S)")
        for problem in problems:
            print(f"  - {problem}")
        print("No database writes performed.")
        return 1
    print(
        f"Dataset validation: OK ({total_records} record(s) across "
        f"{len(datasets)} datasets)"
    )

    session = SessionLocal()
    try:
        # 3. Dry-run summary first, in both modes (read-only SELECTs).
        try:
            dry_reports = run_load(session, datasets, dry_run=True, single_transaction=True)
        except ValidationError as exc:
            print(f"\nVALIDATION FAILED — no database writes performed:\n{exc}")
            return 1
        except Exception as exc:
            print(f"\nDRY RUN FAILED — no database writes performed: {exc}")
            return 1
        projected = _print_reports(
            dry_reports,
            missing_from_input(session, datasets),
            suffix=" (dry run — no statements issued)",
        )
        print(f"Projected writes: {projected} (dry run — no statements issued)")
        session.rollback()

        if not args.apply:
            print("Dry run complete — the database was not modified.")
            _print_protected_note()
            return 0

        # 4. Confirmation before the one transaction opens.
        if not _confirm():
            print(
                f"\nNot confirmed — no database writes performed. Type "
                f"{CONFIRM_PHRASE} on a TTY or set {CONFIRM_ENV}={CONFIRM_PHRASE}."
            )
            return 1

        # 5. Apply: one transaction for the whole run, committed here.
        try:
            reports = run_load(session, datasets, single_transaction=True)
            session.commit()
        except ValidationError as exc:
            session.rollback()
            print(f"\nVALIDATION FAILED — no database writes performed:\n{exc}")
            return 1
        except Exception as exc:
            session.rollback()
            print(
                "\nLOAD FAILED — transaction rolled back, "
                f"no database writes performed: {exc}"
            )
            return 1

        total_writes = _print_reports(
            reports, missing_from_input(session, datasets)
        )
        print(f"Database writes: {total_writes}")
        _print_protected_note()
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
