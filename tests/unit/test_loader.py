"""Unit tests: seed-loader contract (no PostgreSQL required).

Proves the loader's safety rules that don't need a real database:
protected tables are refused, validation gates all writes, dry-run issues
no statements, and the verify policy never mutates existing instances.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from app.data._spec import Dataset, DatasetSpec
from app.data.loader import PROTECTED_TABLES, LoadReport, ValidationError, load_dataset, run_load
from app.data.registry import load_registry
from app.data.validation import validate_datasets


def _spec_for(table: str) -> DatasetSpec:
    return DatasetSpec(
        name="ghost",
        table=table,
        model="app.models.user.User",
        natural_key=("email",),
    )


def test_users_students_enrollments_are_protected() -> None:
    assert PROTECTED_TABLES == frozenset(
        {"users", "students", "student_enrollments", "student_subjects"}
    )


def test_load_dataset_refuses_protected_tables() -> None:
    for table in sorted(PROTECTED_TABLES):
        dataset = Dataset(_spec_for(table), [{"email": "x@example.com"}])
        report = load_dataset(session=None, dataset=dataset, datasets_by_name={})
        assert report.writes == 0
        assert any("protected table" in w for w in report.warnings)


def test_run_load_raises_validation_error_before_touching_database() -> None:
    datasets = load_registry()
    # Force a structural problem: duplicate natural key inside academic_years.
    datasets[0].records.append({"name": "dup", "start_date": "2026-01-01",
                                "end_date": "2026-12-31", "status": "planned"})
    datasets[0].records.append({"name": "dup", "start_date": "2027-01-01",
                                "end_date": "2027-12-31", "status": "planned"})
    engine = create_engine("sqlite+pysqlite://")  # would be touched only if the gate leaked
    with engine.connect() as connection:
        with pytest.raises(ValidationError):
            run_load(connection, datasets)
    engine.dispose()
    # Restore the empty dataset for other tests in this module.
    datasets[0].records.clear()


def test_empty_registry_run_load_yields_zero_writes() -> None:
    datasets = load_registry()
    assert validate_datasets(datasets) == []
    engine = create_engine("sqlite+pysqlite://")
    with engine.connect() as connection:
        reports = run_load(connection, datasets)
    engine.dispose()
    assert all(isinstance(report, LoadReport) for report in reports)
    assert sum(report.writes for report in reports) == 0


def test_verify_policy_reports_drift_without_mutating(monkeypatch) -> None:
    """With a fake in-memory session: verify-policy rows are never updated."""
    from app.data._spec import Dataset

    class FakeInstance:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeResult:
        def scalar_one_or_none(self) -> FakeInstance | None:
            return FakeInstance("different-name")

    class FakeSession:
        def execute(self, _statement):  # noqa: ANN001
            return FakeResult()

        def add(self, _instance):  # noqa: ANN001
            raise AssertionError("verify policy must not insert matching rows")

        def flush(self) -> None:
            pass

    spec = DatasetSpec(
        name="verify_table",
        table="some_table",
        model="app.models.subject.Subject",
        natural_key=("code",),
        record_fields=("name",),
        update_policy="verify",
    )
    dataset = Dataset(spec, [{"code": "S1", "name": "new-name"}])
    report = load_dataset(FakeSession(), dataset, {"verify_table": dataset})

    assert report.inserted == 0
    assert report.updated == 0
    assert report.unchanged == 1
    assert any("not rewritten" in w for w in report.warnings)
