"""Verify the checked-in synthetic catalog on the guarded PostgreSQL fixture."""
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.data.csv_input import load_csv_datasets
from app.data.validation import validate_datasets
from app.models.academic_year import AcademicYear
from app.models.enrollment import StudentEnrollment
from app.models.school import School
from app.models.student_subject import StudentSubject
from app.models.subject import Subject
from tests.integration.test_catalog_csv_load import (
    _catalog_api_client,
    _count_statements,
    _run_cli,
)
from tests.integration.test_registration_end_to_end import _register_account
from tests.integration.test_seeder_idempotency import _run_seeder, capture_row_counts

pytestmark = pytest.mark.integration

DEMO_DIR = Path(__file__).resolve().parents[2] / "examples" / "mvp_demo_catalog"
READINESS = "/api/v1/registrations/readiness"
REGISTRATION = "/api/v1/me/registrations"


def test_shipped_catalog_cannot_supply_registration_context(clean_db):
    _run_seeder(clean_db)
    with clean_db.connect() as connection:
        assert connection.scalars(select(AcademicYear.status)).all() == ["closed"]
        assert connection.execute(text("SELECT count(*) FROM schools")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM school_programs")).scalar_one() == 0

    with _catalog_api_client(clean_db) as client:
        response = client.get(READINESS)
        assert response.status_code == 200
        assert response.json()["ready"] is False
        assert response.json()["open_academic_year"] is None
        assert response.json()["school_catalog_available"] is False


def test_demo_catalog_dry_run_and_idempotent_apply(clean_db, monkeypatch, capsys):
    datasets = load_csv_datasets(DEMO_DIR)
    assert len(datasets) == 12
    assert validate_datasets(datasets) == []
    expected = {"tvet_sectors": 0, "tvet_programs": 0}
    expected.update({dataset.name: 1 for dataset in datasets if dataset.name not in expected})
    assert {dataset.name: len(dataset.records) for dataset in datasets} == expected
    empty = capture_row_counts(clean_db)

    commits = []

    def record_commit(connection):
        commits.append(connection)

    event.listen(clean_db, "commit", record_commit)
    try:
        for apply, writes, commit_count in ((False, 0, 0), (True, 10, 1), (True, 0, 2)):
            with _count_statements(clean_db) as statements:
                result, output = _run_cli(clean_db, DEMO_DIR, monkeypatch, capsys, apply=apply)
            assert result == 0, output
            assert "CSV parse: OK" in output
            assert "Dataset validation: OK" in output
            assert statements["writes"] == writes
            assert len(commits) == commit_count
            counts = capture_row_counts(clean_db)
            if apply:
                assert {name: counts[name] for name in expected} == expected
                assert f"Database writes: {writes}" in output
            else:
                assert counts == empty
    finally:
        event.remove(clean_db, "commit", record_commit)


def test_demo_catalog_registration_and_invalid_contexts(clean_db, monkeypatch, capsys):
    result, output = _run_cli(clean_db, DEMO_DIR, monkeypatch, capsys)
    assert result == 0, output

    with _catalog_api_client(clean_db) as client:
        year, = _catalog_rows(client, "/academic-years")
        school, = _catalog_rows(client, "/schools")
        offering, = _catalog_rows(client, "/schools/DEMO-SCHOOL/programs")
        assert year["name"] == "DEMO-2099/2100"
        assert school["school_code"] == "DEMO-SCHOOL"
        assert offering["code"] == "DEMO-PROGRAM-V1"

        response = client.get(READINESS)
        assert response.status_code == 200
        readiness = response.json()
        assert readiness["ready"] is True
        assert readiness["open_academic_year"]["id"] == year["id"]
        assert readiness["school_catalog_available"] is True
        assert readiness["program_catalog_available"] is True

        tokens = _register_account(client, "demo-registration@example.com")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        payload = {
            "academic_year_id": year["id"],
            "school_id": school["id"],
            "pathway": "DEMO-PATHWAY",
            "education_level": "DEMO-LEVEL-1",
            "program_version_id": offering["program_version_id"],
        }

        # Add negative-control rows only in the disposable test transaction.
        with Session(clean_db, expire_on_commit=False) as session:
            other_school = School(name="Demo School Without Offering", school_code="DEMO-NO-OFFER")
            other_year = AcademicYear(
                name="DEMO-OTHER-YEAR",
                start_date=date(2100, 9, 1),
                end_date=date(2101, 7, 31),
                status="planned",
            )
            session.add_all([other_school, other_year])
            session.commit()

            for change, reason in (
                ({"school_id": str(other_school.id)}, "does not offer"),
                ({"academic_year_id": str(other_year.id)}, "does not belong"),
            ):
                rejected = client.post(REGISTRATION, headers=headers, json=payload | change)
                assert rejected.status_code == 422, rejected.text
                assert reason in rejected.json()["detail"]

            other_year.status = "closed"
            session.commit()
            closed = client.post(
                REGISTRATION, headers=headers,
                json=payload | {"academic_year_id": str(other_year.id)},
            )
            assert closed.status_code == 503, closed.text
            assert "closed" in closed.json()["detail"]

        created = client.post(REGISTRATION, headers=headers, json=payload)
        assert created.status_code == 201, created.text
        enrollment = created.json()
        assert enrollment["status"] == "pending"
        assert enrollment["program_code"] == "DEMO-PROGRAM"
        assert [subject["code"] for subject in enrollment["subjects"]] == ["DEMO-MATH"]

        duplicate = client.post(REGISTRATION, headers=headers, json=payload)
        assert duplicate.status_code == 409, duplicate.text

    with Session(clean_db) as session:
        saved = session.scalars(select(StudentEnrollment)).one()
        link = session.scalars(select(StudentSubject)).one()
        assert str(saved.id) == enrollment["enrollment_id"]
        assert link.enrollment_id == saved.id
        assert session.get(Subject, link.subject_id).code == "DEMO-MATH"


def _catalog_rows(client, path):
    response = client.get(f"/api/v1/catalog{path}")
    assert response.status_code == 200, response.text
    return response.json()
