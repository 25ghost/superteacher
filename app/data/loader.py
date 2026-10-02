"""Reference-data seed loader core.

Design contract (Phases 3C / 4A):

- INSERT if absent, matching on each dataset's declared natural key
  (backed by UNIQUE constraints verified in Phase 3C).
- UPDATE only where the spec's ``update_policy`` allows it:
    * ``update``  — apply the dataset's mutable fields in place;
    * ``verify``  — never update (academic_years, program_versions);
      any drift between dataset and database is reported as a warning for
      a human to decide. Historical meaning is never rewritten by a re-run.
- NEVER delete catalog records.
- NEVER touch users, students, student_enrollments, student_subjects —
  enrollment data belongs to the registration module, never to the seeder.
- program_versions identity is the four-column offering tuple; the
  ``code`` column is a label, never identity.
- Default: one transaction per dataset; a failing dataset aborts its own
  transaction and stops the run before later datasets are attempted.
- ``single_transaction=True``: flush-only for the whole run — this module
  never commits; the caller owns the single ``commit()``/``rollback()``
  (the same explicit transaction exception scripts/create_admin.py and
  scripts/load_catalog.py document). A record-level failure is re-raised
  instead of downgraded to a warning, because the session may open a fresh
  transaction after the failed flush and continue — which would let a
  partially-loaded catalog commit. Propagation makes the caller roll the
  whole run back.
- ``dry_run`` issues SELECTs only — zero INSERT/UPDATE/DELETE statements.

Datasets are intentionally EMPTY in Phase 4A: this is the foundation the
authoritative Rwanda catalog will be loaded through in a later phase. With
empty datasets, a run performs zero database writes.

Whole-system framing: this is shared SuperTeacher reference-data
infrastructure, not a Student Registration service.
"""
from __future__ import annotations

import datetime
import importlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data._spec import Dataset, DatasetSpec

# Tables the seeder must never write under any circumstances.
PROTECTED_TABLES: frozenset[str] = frozenset(
    {"users", "students", "student_enrollments", "student_subjects"}
)


class ValidationError(Exception):
    """Raised before any database write when datasets fail structural validation."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            f"{len(problems)} dataset validation problem(s):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


# Throwaway value used only during dry-run for FKs whose parent row the run
# itself would insert (so it cannot exist in the database yet). Never bound
# to a query against a real column and never written.
_DRY_RUN_FK_PLACEHOLDER = object()


@dataclass
class LoadReport:
    """Per-dataset outcome of one loader run."""

    dataset: str
    table: str
    records: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: bool = False  # True during dry-run / after a failed earlier dataset
    warnings: list[str] = field(default_factory=list)

    @property
    def writes(self) -> int:
        return self.inserted + self.updated


def _import_model(dotted: str):
    module_path, class_name = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), class_name)


def _coerce_like(existing, value):
    """Coerce a dataset value into the shape of the stored column value.

    Dataset literals are plain strings (e.g. ISO dates "2025-09-01"), while
    the ORM returns typed objects (e.g. ``datetime.date``). Comparing them
    raw makes every equal value look like drift under the verify policy.
    Dates are parsed from ISO strings; everything else compares as-is.
    """
    if isinstance(existing, (datetime.date, datetime.datetime)) and isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _resolve_column_values(
    session: Session,
    datasets_by_name: dict[str, Dataset],
    spec: DatasetSpec,
    record: dict,
    dry_run: bool = False,
) -> dict:
    """Map a dataset record onto real model column values.

    Reference fields are resolved to the parent row's UUID PK by the parent's
    natural key (scalar) or natural-key tuple (composite ``version_key``).

    Dry-run subtlety: a dry run issues no INSERT statements, so a parent that
    the run itself would create is not yet in the database. When the parent
    row is absent from the database *but* the reference resolves against the
    (structurally validated) parent dataset, a throwaway placeholder is used
    so the report can still project what a live load would do. The
    structural validation gate has already proven the reference resolves
    within the datasets, so this placeholder can never mask a broken
    reference in a live run — live loads still require the real row.
    """
    values: dict = {}
    for key, value in record.items():
        if key not in spec.references:
            values[key] = value
            continue

        parent = datasets_by_name[spec.references[key]]
        parent_model = _import_model(parent.spec.model)
        if isinstance(value, list):
            if len(value) != len(parent.spec.natural_key):
                raise ValueError(
                    f"reference {key!r} has {len(value)} parts but parent "
                    f"{parent.name} natural key has {len(parent.spec.natural_key)}"
                )
            filters = dict(zip(parent.spec.natural_key, value))
        else:
            if len(parent.spec.natural_key) != 1:
                raise ValueError(
                    f"reference {key!r} is scalar but parent {parent.name} "
                    f"has a composite natural key"
                )
            filters = {parent.spec.natural_key[0]: value}

        instance = session.execute(
            select(parent_model).filter_by(**filters)
        ).scalar_one_or_none()
        if instance is None:
            if dry_run:
                # Resolve against the parent dataset (validation gate has
                # already proven this succeeds) instead of the un-inserted
                # database row.
                candidate = tuple(value) if isinstance(value, list) else value
                parent_pool = {
                    tuple(r[f] for f in parent.spec.natural_key)
                    if len(parent.spec.natural_key) > 1
                    else r[parent.spec.natural_key[0]]
                    for r in parent.records
                }
                if candidate in parent_pool:
                    values[spec.fk_columns[key]] = _DRY_RUN_FK_PLACEHOLDER
                    continue
            raise LookupError(
                f"reference {key!r}={value!r} not found in {parent.name} "
                f"(run structural validation before loading)"
            )
        values[spec.fk_columns[key]] = instance.id
    return values


def load_dataset(
    session: Session,
    dataset: Dataset,
    datasets_by_name: dict[str, Dataset],
    dry_run: bool = False,
    *,
    fail_fast: bool = False,
) -> LoadReport:
    """Load one dataset idempotently inside the caller's transaction.

    With ``fail_fast=True`` a record-level failure is re-raised instead of
    being downgraded to a report warning — used by the single-transaction
    mode of :func:`run_load`, where swallowing the error could commit a
    partially-loaded catalog.
    """
    spec = dataset.spec
    report = LoadReport(dataset=spec.name, table=spec.table, records=len(dataset.records))

    if spec.table in PROTECTED_TABLES:
        report.warnings.append(f"dataset targets protected table {spec.table!r}; refusing")
        return report

    model = _import_model(spec.model)
    mutable = tuple(
        f for f in (*spec.record_fields, *spec.optional_fields) if f not in spec.natural_key
    )

    for index, record in enumerate(dataset.records):
        label = f"{spec.name}[{index}]"
        try:
            values = _resolve_column_values(
                session, datasets_by_name, spec, record, dry_run=dry_run
            )
            # Existence-lookup columns: a natural-key field that is a declared
            # reference resolves through its FK column (e.g. "pathway" ->
            # "pathway_id"); a plain field resolves under its own name.
            # (Before Phase 4B this line used the reference field name itself,
            # which had never executed because every dataset was empty.)
            filters = {
                spec.fk_columns.get(field_name, field_name): values[
                    spec.fk_columns.get(field_name, field_name)
                ]
                for field_name in spec.natural_key
            }

            # Dry-run only: a natural key containing a placeholder FK means
            # the parent row would be created by this very run, so the child
            # row cannot exist yet — skip the existence SELECT and project
            # the would-be insert.
            if dry_run and any(
                values[spec.fk_columns.get(field_name, field_name)]
                is _DRY_RUN_FK_PLACEHOLDER
                for field_name in spec.natural_key
            ):
                report.inserted += 1
                continue

            instance = session.execute(
                select(model).filter_by(**filters)
            ).scalar_one_or_none()

            if instance is None:
                if not dry_run:
                    session.add(model(**values))
                    session.flush()
                report.inserted += 1
                continue

            if spec.update_policy == "verify":
                drifted = {
                    field_name: (getattr(instance, field_name), values[field_name])
                    for field_name in mutable
                    if field_name in values
                    and _coerce_like(getattr(instance, field_name), values[field_name])
                    != getattr(instance, field_name)
                }
                if drifted and not dry_run:
                    report.warnings.append(
                        f"{label}: existing row differs from dataset (policy=verify, "
                        f"not rewritten): {drifted}"
                    )
                report.unchanged += 1
                continue

            changed = {
                field_name: values[field_name]
                for field_name in mutable
                if field_name in values
                and _coerce_like(getattr(instance, field_name), values[field_name])
                != getattr(instance, field_name)
            }
            if changed:
                if not dry_run:
                    for field_name, value in changed.items():
                        setattr(instance, field_name, value)
                report.updated += 1
            else:
                report.unchanged += 1
        except Exception as exc:
            if fail_fast:
                raise
            report.warnings.append(f"{label}: {exc}")

    return report


def run_load(
    session: Session,
    datasets: Iterable[Dataset],
    dry_run: bool = False,
    *,
    single_transaction: bool = False,
) -> list[LoadReport]:
    """Load every dataset in declared order.

    Structural validation is a hard pre-write gate: any problem raises
    :class:`ValidationError` before the first database statement.

    Default (``single_transaction=False``): one transaction per dataset.
    A dataset whose transaction fails aborts its own transaction and stops
    the run; later datasets are reported as ``skipped`` rather than
    silently omitted.

    ``single_transaction=True``: flush-only for the entire run — no
    ``commit()`` here. The caller owns the single commit (or rollback) of
    the whole run; a record-level failure propagates so the caller can
    roll everything back. See the module docstring for why the failure
    must not be downgraded to a warning in this mode.
    """
    from app.data.validation import validate_datasets

    datasets = list(datasets)
    datasets_by_name = {dataset.name: dataset for dataset in datasets}

    problems = validate_datasets(datasets)
    if problems:
        raise ValidationError(problems)

    reports: list[LoadReport] = []
    aborted = False
    for dataset in datasets:
        if aborted:
            report = LoadReport(
                dataset=dataset.name, table=dataset.spec.table,
                records=len(dataset.records),
            )
            report.skipped = True
            reports.append(report)
            continue
        if single_transaction:
            # Flush-only inside the caller's transaction; any failure must
            # abort the whole run (fail_fast) so a partial catalog can
            # never reach a commit.
            report = load_dataset(
                session,
                dataset,
                datasets_by_name,
                dry_run=dry_run,
                fail_fast=True,
            )
            reports.append(report)
            continue
        try:
            with session.begin():
                report = load_dataset(session, dataset, datasets_by_name, dry_run=dry_run)
        except Exception as exc:
            report = LoadReport(
                dataset=dataset.name, table=dataset.spec.table,
                records=len(dataset.records),
            )
            report.warnings.append(f"transaction aborted: {exc}")
            reports.append(report)
            aborted = True
            continue
        reports.append(report)
    return reports
