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


# --- single-transaction mode (flush-only, caller owns the commit) -------------


def _registry_with(records_by_name: dict[str, list[dict]]) -> list[Dataset]:
    """Full 12-dataset registry with explicit (default: empty) records."""
    return [
        Dataset(dataset.spec, list(records_by_name.get(dataset.name, ())))
        for dataset in load_registry()
    ]


def _sqlite_session():
    from sqlalchemy.orm import Session

    from app.core.database import Base
    import app.models  # noqa: F401  (registers every table)

    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    return Session(bind=engine), engine


def _count(session, model) -> int:
    from sqlalchemy import func, select

    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_single_transaction_flushes_but_never_commits() -> None:
    """The engine is flush-only in this mode: a rollback discards the run."""
    from app.models.pathway import Pathway

    session, engine = _sqlite_session()
    try:
        datasets = _registry_with({"pathways": [{"code": "P1", "name": "Path 1"}]})
        reports = run_load(session, datasets, single_transaction=True)
        assert sum(r.writes for r in reports) == 1
        # Flushed: visible inside the caller's still-open transaction...
        assert _count(session, Pathway) == 1
        # ...but never committed: the caller's rollback erases everything.
        session.rollback()
        assert _count(session, Pathway) == 0
    finally:
        session.close()
        engine.dispose()


def test_single_transaction_failure_propagates_and_rolls_back_everything() -> None:
    """A record-level failure aborts the WHOLE run, not just its dataset.

    The bad row (invalid ``status`` — passes structural validation, fails
    the DB CHECK) sits in dataset 4; the valid ``pathways`` insert from
    dataset 2 must be rolled back with it.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.education_level import EducationLevel
    from app.models.pathway import Pathway

    session, engine = _sqlite_session()
    try:
        datasets = _registry_with({
            "pathways": [{"code": "P1", "name": "Path 1"}],
            "education_levels": [
                {"code": "L1", "name": "Level 1", "level_number": 1, "status": "bogus"},
            ],
        })
        with pytest.raises(IntegrityError):
            run_load(session, datasets, single_transaction=True)
        session.rollback()
        assert _count(session, Pathway) == 0
        assert _count(session, EducationLevel) == 0
    finally:
        session.close()
        engine.dispose()


def test_default_mode_still_downgrades_record_errors_to_warnings() -> None:
    """Default behavior unchanged: no raise, error becomes a warning."""
    from app.models.education_level import EducationLevel
    from app.models.pathway import Pathway

    session, engine = _sqlite_session()
    try:
        datasets = _registry_with({
            "pathways": [{"code": "P1", "name": "Path 1"}],
            "education_levels": [
                {"code": "L1", "name": "Level 1", "level_number": 1, "status": "bogus"},
            ],
        })
        reports = run_load(session, datasets)  # default mode — must not raise
        by_name = {report.dataset: report for report in reports}
        assert by_name["pathways"].inserted == 1
        assert by_name["education_levels"].inserted == 0
        assert by_name["education_levels"].warnings, "record error must be reported"
        assert _count(session, EducationLevel) == 0
    finally:
        session.close()
        engine.dispose()


def test_single_transaction_dry_run_writes_nothing() -> None:
    from app.models.pathway import Pathway

    session, engine = _sqlite_session()
    try:
        datasets = _registry_with({"pathways": [{"code": "P1", "name": "Path 1"}]})
        reports = run_load(session, datasets, dry_run=True, single_transaction=True)
        assert sum(r.writes for r in reports) == 1  # projected...
        assert _count(session, Pathway) == 0  # ...but zero rows
    finally:
        session.close()
        engine.dispose()


def test_composite_version_key_resolves_through_its_parent_natural_key() -> None:
    """Regression: program_versions' natural-key fields are relationships
    ("program", "level" is not even an attribute), so the lookup must go
    through FK columns — never filter_by(program=...)."""
    import datetime

    from app.data.loader import _resolve_column_values
    from app.models.academic_year import AcademicYear
    from app.models.education_level import EducationLevel
    from app.models.pathway import Pathway
    from app.models.program import Program
    from app.models.program_version import ProgramVersion
    from app.models.subject import Subject

    session, engine = _sqlite_session()
    try:
        program = Program(code="P1", name="Program One")
        year = AcademicYear(
            name="2099/2100",
            start_date=datetime.date(2099, 9, 1),
            end_date=datetime.date(2100, 7, 31),
        )
        pathway = Pathway(code="OL", name="O-Level")
        level = EducationLevel(code="L1", name="Level 1", level_number=1)
        session.add_all([program, year, pathway, level])
        session.flush()
        version = ProgramVersion(
            program_id=program.id,
            academic_year_id=year.id,
            pathway_id=pathway.id,
            education_level_id=level.id,
            code="V1",
            name="Version 1",
        )
        subject = Subject(code="S1", name="Subject One")
        session.add_all([version, subject])
        session.flush()

        datasets_by_name = {
            dataset.name: dataset for dataset in load_registry()
        }
        spec = datasets_by_name["program_subjects"].spec
        record = {
            "version_key": ["P1", "2099/2100", "OL", "L1"],
            "subject": "S1",
        }
        values = _resolve_column_values(session, datasets_by_name, spec, record)
        assert values["program_version_id"] == version.id
        assert values["subject_id"] == subject.id

        bad = dict(record, version_key=["NOPE", "2099/2100", "OL", "L1"])
        with pytest.raises(LookupError):
            _resolve_column_values(session, datasets_by_name, spec, bad)
    finally:
        session.close()
        engine.dispose()
