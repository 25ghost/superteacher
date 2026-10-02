"""Catalog CSV loader end-to-end tests (PostgreSQL, guarded test DB).

Proves on the real database:

- the synthetic TEST-* catalog applies through ``scripts/load_catalog.py``
  and a rerun issues ZERO INSERT/UPDATE/DELETE statements (statement
  counter) — idempotency, not anecdote;
- the dry run issues no write statements and leaves the database empty;
- the 40 shipped "verified minimum" rows coexist: an identical CSV
  roundtrip is a no-op, an ``update``-policy change updates, and a
  ``verify``-policy drift is warned but never rewritten;
- rows absent from the input are reported as "missing from input" and
  never deleted;
- one transaction: a single bad row rolls the WHOLE run back (zero rows
  anywhere), and validator-invalid input exits 1 with ZERO statements;
- end to end: a student registers through the API against the loaded
  catalog and is enrolled into the CSV-defined subjects;
- the catalog API serves the loaded rows: every one of the 10
  GET /catalog/* routes returns 200 with a stable shape, the loaded
  rows, and deterministic ordering.
"""
from __future__ import annotations

import contextlib
import csv as csv_mod
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.data.csv_input import required_columns
from app.data.registry import EXPECTED_LOAD_ORDER, load_registry
from app.schemas.catalog import (
    AcademicYearRead,
    EducationLevelRead,
    PathwayRead,
    ProgramRead,
    ProgramVersionRead,
    SchoolProgramRead,
    SchoolRead,
    SubjectRead,
    TVETProgramRead,
    TVETSectorRead,
)
from tests.conftest import APPLICATION_TABLES
from tests.integration.test_seeder_idempotency import (
    EXPECTED_DATASET_COUNTS,
    _run_seeder,
    _truncate_all,
    capture_row_counts,
)

pytestmark = pytest.mark.integration

SPECS = {dataset.name: dataset.spec for dataset in load_registry()}

#: The synthetic catalog: obviously fake TEST-* codes, FK-consistent,
#: every one of the 12 datasets represented.
SYNTHETIC: dict[str, str] = {
    "academic_years": (
        "name,start_date,end_date,status\n"
        "TEST-2099/2100,2099-09-01,2100-07-31,active\n"
    ),
    "pathways": "code,name\nTEST-OL,Test O-Level\n",
    "education_levels": "code,name,level_number\nTEST-L1,Test Level 1,1\n",
    "pathway_levels": "pathway,level\nTEST-OL,TEST-L1\n",
    "subjects": (
        "code,name\n"
        "TEST-SUB-MATH,Test Mathematics\n"
        "TEST-SUB-ENG,Test English\n"
    ),
    "programs": (
        "code,name,program_type\n"
        "TEST-COMBO,Test Combination,combination\n"
        "TEST-TVET-PROG,Test TVET Programme,tvet_program\n"
    ),
    "tvet_sectors": "code,name\nTEST-SECTOR-ICT,Test ICT Sector\n",
    "tvet_programs": "program,sector\nTEST-TVET-PROG,TEST-SECTOR-ICT\n",
    "program_versions": (
        "program,academic_year,pathway,level,code,name\n"
        "TEST-COMBO,TEST-2099/2100,TEST-OL,TEST-L1,"
        "TEST-COMBO-V1,Test Combination 2099/2100\n"
    ),
    "program_subjects": (
        "version_key,subject,subject_type,display_order\n"
        "TEST-COMBO|TEST-2099/2100|TEST-OL|TEST-L1,TEST-SUB-MATH,core,1\n"
        "TEST-COMBO|TEST-2099/2100|TEST-OL|TEST-L1,TEST-SUB-ENG,core,2\n"
    ),
    "schools": "school_code,name\nTEST-SCHOOL-001,Test School One\n",
    "school_programs": (
        "school,version_key\n"
        "TEST-SCHOOL-001,TEST-COMBO|TEST-2099/2100|TEST-OL|TEST-L1\n"
    ),
}

SYNTHETIC_COUNTS: dict[str, int] = {
    "academic_years": 1,
    "pathways": 1,
    "education_levels": 1,
    "pathway_levels": 1,
    "subjects": 2,
    "programs": 2,
    "tvet_sectors": 1,
    "tvet_programs": 1,
    "program_versions": 1,
    "program_subjects": 2,
    "schools": 1,
    "school_programs": 1,
}


def _write_catalog(csv_dir: Path, files: dict[str, str]) -> Path:
    """Write all 12 files; ``files`` overrides individual datasets.

    Override keys are dataset names, with or without the ``.csv`` suffix.
    """
    csv_dir.mkdir(parents=True, exist_ok=True)
    normalized = {
        key.removesuffix(".csv"): value for key, value in files.items()
    }
    for name in EXPECTED_LOAD_ORDER:
        content = normalized.get(name, ",".join(required_columns(SPECS[name])) + "\n")
        (csv_dir / f"{name}.csv").write_text(content, encoding="utf-8")
    return csv_dir


def _export_registry(csv_dir: Path) -> Path:
    """CSV roundtrip of the shipped registry records (test utility).

    Optional columns are included only when every record carries a
    non-empty value, so the export parses back without empty-cell errors.
    """
    csv_dir.mkdir(parents=True, exist_ok=True)
    for dataset in load_registry():
        spec = dataset.spec
        fields = list(required_columns(spec))
        for field in spec.optional_fields:
            if field in fields:
                continue
            if dataset.records and all(
                record.get(field) not in (None, "") for record in dataset.records
            ):
                fields.append(field)
        with (csv_dir / f"{spec.name}.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv_mod.writer(handle)
            writer.writerow(fields)
            for record in dataset.records:
                row = []
                for field in fields:
                    value = record.get(field, "")
                    if isinstance(value, list):
                        value = "|".join(value)
                    row.append(value)
                writer.writerow(row)
    return csv_dir


def _edit_cell(path: Path, key_field: str, key_value: str, changes: dict) -> None:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv_mod.reader(handle))
    header = rows[0]
    for row in rows[1:]:
        if row[header.index(key_field)] == key_value:
            for field, value in changes.items():
                row[header.index(field)] = value
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv_mod.writer(handle).writerows(rows)


@contextlib.contextmanager
def _count_statements(engine):
    """Count statements executed on ``engine`` (total and INSERT/UPDATE/DELETE)."""
    counts = {"total": 0, "writes": 0}

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        counts["total"] += 1
        verb = statement.lstrip().split(" ", 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            counts["writes"] += 1

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield counts
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)


def _run_cli(engine, csv_dir: Path, monkeypatch, capsys, *, apply: bool = True):
    from scripts import load_catalog

    monkeypatch.setattr(load_catalog, "SessionLocal", sessionmaker(bind=engine))
    if apply:
        monkeypatch.setenv("CATALOG_LOAD_CONFIRM", "APPLY")
    args = ["--csv-dir", str(csv_dir)] + (["--apply"] if apply else [])
    rc = load_catalog.main(args)
    return rc, capsys.readouterr().out


@contextlib.contextmanager
def _catalog_api_client(pg_engine):
    """TestClient bound to ``pg_engine``; catalog routes need no auth."""
    from fastapi.testclient import TestClient

    from app.core.database import SessionLocal, get_db
    from app.main import app

    def _override_get_db():
        db = SessionLocal(bind=pg_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_apply_then_rerun_issues_zero_write_statements(
    clean_db, tmp_path: Path, monkeypatch, capsys
) -> None:
    """The core idempotency proof, with a statement counter."""
    csv_dir = _write_catalog(tmp_path / "csv", SYNTHETIC)

    with _count_statements(clean_db) as first:
        rc, out = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0
    assert first["writes"] == sum(SYNTHETIC_COUNTS.values())
    assert "Database writes: 15" in out
    counts_after_first = capture_row_counts(clean_db)
    for table, expected in SYNTHETIC_COUNTS.items():
        assert counts_after_first[table] == expected, table

    with _count_statements(clean_db) as second:
        rc, out = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0
    # Zero writes — while SELECTs prove the counter is not vacuous.
    assert second["writes"] == 0
    assert second["total"] > 0
    assert "Database writes: 0" in out
    assert "inserted=0" in out
    assert "warning" not in out
    assert capture_row_counts(clean_db) == counts_after_first

    _truncate_all(clean_db)


def test_dry_run_issues_no_write_statements(
    clean_db, tmp_path: Path, monkeypatch, capsys
) -> None:
    csv_dir = _write_catalog(tmp_path / "csv", SYNTHETIC)
    with _count_statements(clean_db) as counter:
        rc, out = _run_cli(clean_db, csv_dir, monkeypatch, capsys, apply=False)
    assert rc == 0
    assert counter["writes"] == 0
    assert "Dry run complete — the database was not modified." in out
    assert "Projected writes: 15" in out
    counts = capture_row_counts(clean_db)
    assert all(counts[table] == 0 for table in APPLICATION_TABLES)


def test_single_bad_row_rolls_back_the_whole_run(
    clean_db, tmp_path: Path, monkeypatch, capsys
) -> None:
    """One transaction: the failing subjects row erases the 4 earlier datasets."""
    files = dict(SYNTHETIC)
    files["subjects"] = (
        "code,name,status\n"
        "TEST-SUB-MATH,Test Mathematics,active\n"
        "TEST-SUB-ENG,Test English,bogus\n"
    )
    csv_dir = _write_catalog(tmp_path / "csv", files)
    with _count_statements(clean_db) as counter:
        rc, out = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 1
    assert "LOAD FAILED — transaction rolled back" in out
    assert "no database writes performed" in out
    counts = capture_row_counts(clean_db)
    assert counts == {table: 0 for table in counts}, counts
    assert counter["writes"] >= 5  # writes were attempted, then undone


def test_validation_gate_exits_1_with_zero_statements(
    clean_db, tmp_path: Path, monkeypatch, capsys
) -> None:
    """Validator-invalid input: exit 1 and not a single SQL statement."""
    files = dict(SYNTHETIC)
    files["subjects"] = (
        "code,name\n"
        "TEST-SUB-MATH,Test Mathematics\n"
        "TEST-SUB-ENG,Test English\n"
        "TEST-DUP,Duplicate One\n"
        "TEST-DUP,Duplicate Two\n"
    )
    csv_dir = _write_catalog(tmp_path / "csv", files)
    with _count_statements(clean_db) as counter:
        rc, out = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 1
    assert "Dataset validation: 1 PROBLEM(S)" in out
    assert "duplicate natural key" in out
    assert "No database writes performed." in out
    assert counter["total"] == 0
    counts = capture_row_counts(clean_db)
    assert all(counts[table] == 0 for table in APPLICATION_TABLES)


def test_missing_from_input_is_reported_and_rows_are_never_deleted(
    clean_db, tmp_path: Path, monkeypatch, capsys
) -> None:
    csv_dir = _write_catalog(tmp_path / "csv", SYNTHETIC)
    rc, _ = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0

    # Two datasets become header-only: their rows are now "missing input".
    # (They are chosen because nothing else references them, so the
    # structural gate still passes.)
    (csv_dir / "pathway_levels.csv").write_text("pathway,level\n", encoding="utf-8")
    (csv_dir / "school_programs.csv").write_text(
        "school,version_key\n", encoding="utf-8"
    )
    rc, out = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0
    assert "missing from input: pathway=TEST-OL, level=TEST-L1" in out
    assert (
        "missing from input: school=TEST-SCHOOL-001, "
        "version_key=TEST-COMBO|TEST-2099/2100|TEST-OL|TEST-L1" in out
    )
    counts = capture_row_counts(clean_db)
    assert counts["pathway_levels"] == 1  # untouched, never deleted
    assert counts["school_programs"] == 1

    _truncate_all(clean_db)


def test_shipped_catalog_roundtrip_is_a_noop(
    clean_db, tmp_path: Path, monkeypatch, capsys
) -> None:
    """The 40 verified-minimum rows coexist: identical input = zero writes."""
    _run_seeder(clean_db)
    counts_seeded = capture_row_counts(clean_db)
    assert sum(EXPECTED_DATASET_COUNTS.values()) == 40

    csv_dir = _export_registry(tmp_path / "csv")
    with _count_statements(clean_db) as counter:
        rc, out = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0
    assert counter["writes"] == 0
    assert "Database writes: 0" in out
    assert "warning" not in out
    assert capture_row_counts(clean_db) == counts_seeded

    _truncate_all(clean_db)


def test_update_policy_updates_while_verify_policy_never_rewrites(
    clean_db, tmp_path: Path, monkeypatch, capsys
) -> None:
    _run_seeder(clean_db)
    csv_dir = _export_registry(tmp_path / "csv")

    # subjects: update policy → the rename must be applied.
    _edit_cell(csv_dir / "subjects.csv", "code", "SUB_MATH", {"name": "Renamed Mathematics"})
    # academic_years: verify policy → the drift must only be warned.
    _edit_cell(
        csv_dir / "academic_years.csv", "name", "2025/2026", {"start_date": "2024-09-01"}
    )

    rc, out = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0
    assert "policy=verify" in out  # drift warning for academic_years
    assert "updated=1" in out  # the subjects rename was applied

    with clean_db.connect() as connection:
        renamed = connection.execute(
            text("SELECT name FROM subjects WHERE code = 'SUB_MATH'")
        ).scalar_one()
        assert renamed == "Renamed Mathematics"
        start_date = connection.execute(
            text("SELECT start_date FROM academic_years WHERE name = '2025/2026'")
        ).scalar_one()
        assert str(start_date) == "2025-09-01"  # never rewritten

    _truncate_all(clean_db)


def test_e2e_registration_against_the_csv_loaded_catalog(
    clean_db, pg_engine, tmp_path: Path, monkeypatch, capsys
) -> None:
    """A student registers through the API against the synthetic catalog."""
    from app.core.database import Base
    import app.models  # noqa: F401

    csv_dir = _write_catalog(tmp_path / "csv", SYNTHETIC)
    rc, _ = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0

    Base.metadata.create_all(pg_engine)

    with _catalog_api_client(pg_engine) as client:
        try:
            response = client.post(
                "/api/v1/auth/register",
                json={
                    "email": "csv-e2e@example.com",
                    "password": "correct horse battery staple",
                    "full_name": "Catalog End To End Student",
                    "date_of_birth": "2012-04-10",
                    "gender": "female",
                    "country": "Rwanda",
                },
            )
            assert response.status_code == 201, response.text
            header = {"Authorization": f"Bearer {response.json()['access_token']}"}

            with clean_db.connect() as connection:
                year_id = connection.execute(
                    text("SELECT id FROM academic_years WHERE name = 'TEST-2099/2100'")
                ).scalar_one()
                version_id = connection.execute(
                    text("SELECT id FROM program_versions WHERE code = 'TEST-COMBO-V1'")
                ).scalar_one()
                school_id = connection.execute(
                    text("SELECT id FROM schools WHERE school_code = 'TEST-SCHOOL-001'")
                ).scalar_one()

            registration = client.post(
                "/api/v1/me/registrations",
                json={
                    "academic_year_id": str(year_id),
                    "pathway": "TEST-OL",
                    "education_level": "TEST-L1",
                    "program_version_id": str(version_id),
                    "school_id": str(school_id),
                },
                headers=header,
            )
            assert registration.status_code == 201, registration.text
            body = registration.json()
            assert body["status"] == "pending"
            assert body["program_code"] == "TEST-COMBO"
            assert body["school_name"] == "Test School One"
            assert sorted(subject["code"] for subject in body["subjects"]) == [
                "TEST-SUB-ENG",
                "TEST-SUB-MATH",
            ]
        finally:
            _truncate_all(clean_db)


# --- catalog API sweep over the loaded catalog ---------------------------------


def _sweep_catalog() -> dict[str, str]:
    """SYNTHETIC plus a second row in every dataset backing a list route.

    Two rows per route make the ordering assertions meaningful; the rows
    stay FK-consistent (validated by the loader gate before any write).
    """
    files = dict(SYNTHETIC)
    files["academic_years"] = (
        SYNTHETIC["academic_years"]
        + "TEST-2100/2101,2100-09-01,2101-07-31,planned\n"
    )
    files["pathways"] = SYNTHETIC["pathways"] + "TEST-AL,Test A-Level\n"
    files["education_levels"] = (
        SYNTHETIC["education_levels"] + "TEST-L2,Test Level 2,2\n"
    )
    files["pathway_levels"] = SYNTHETIC["pathway_levels"] + "TEST-AL,TEST-L2\n"
    files["programs"] = (
        SYNTHETIC["programs"]
        + "TEST-TVET-PROG-2,Test TVET Programme Two,tvet_program\n"
    )
    files["tvet_sectors"] = (
        SYNTHETIC["tvet_sectors"] + "TEST-SECTOR-ENG,Test Engineering Sector\n"
    )
    files["tvet_programs"] = (
        SYNTHETIC["tvet_programs"] + "TEST-TVET-PROG-2,TEST-SECTOR-ENG\n"
    )
    files["program_versions"] = (
        SYNTHETIC["program_versions"]
        + "TEST-COMBO,TEST-2100/2101,TEST-AL,TEST-L2,"
        "TEST-COMBO-V2,Test Combination 2100/2101\n"
    )
    files["schools"] = SYNTHETIC["schools"] + "TEST-SCHOOL-002,Test School Two\n"
    files["school_programs"] = (
        SYNTHETIC["school_programs"]
        + "TEST-SCHOOL-001,TEST-COMBO|TEST-2100/2101|TEST-AL|TEST-L2\n"
        + "TEST-SCHOOL-002,TEST-COMBO|TEST-2100/2101|TEST-AL|TEST-L2\n"
    )
    return files


def _assert_list_route(
    client, path: str, model, expected: list[str], *, field: str = "code"
) -> list[dict]:
    """GET a list route: 200, stable shape, expected rows in documented order.

    Each route is fetched twice; identical payloads prove the ordering is
    deterministic across calls (the documented ORDER BY).
    """
    first = client.get(f"/api/v1/catalog{path}")
    assert first.status_code == 200, f"{path}: {first.text}"
    payload = first.json()
    assert isinstance(payload, list), path
    fields = set(model.model_fields)
    for item in payload:
        assert set(item) == fields, f"{path}: unstable shape {set(item) ^ fields}"
    assert [row[field] for row in payload] == expected, path
    second = client.get(f"/api/v1/catalog{path}")
    assert second.status_code == 200, path
    assert second.json() == payload, f"{path}: non-deterministic ordering"
    return payload


def test_catalog_api_sweep_returns_loaded_rows_in_documented_order(
    clean_db, pg_engine, tmp_path: Path, monkeypatch, capsys
) -> None:
    """All 10 GET /catalog/* routes against the CSV-loaded sweep catalog."""
    csv_dir = _write_catalog(tmp_path / "csv", _sweep_catalog())
    rc, _ = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0

    with _catalog_api_client(pg_engine) as client:
        # start_date descending, then name
        _assert_list_route(
            client, "/academic-years", AcademicYearRead,
            ["TEST-2100/2101", "TEST-2099/2100"], field="name",
        )
        # code
        _assert_list_route(client, "/pathways", PathwayRead, ["TEST-AL", "TEST-OL"])
        # level_number, then code
        _assert_list_route(
            client, "/education-levels", EducationLevelRead, ["TEST-L1", "TEST-L2"]
        )
        # name, then code
        _assert_list_route(
            client, "/subjects", SubjectRead,
            ["Test English", "Test Mathematics"], field="name",
        )
        # code
        _assert_list_route(
            client, "/programs", ProgramRead,
            ["TEST-COMBO", "TEST-TVET-PROG", "TEST-TVET-PROG-2"],
        )
        # program code, start_date descending, level_number
        _assert_list_route(
            client, "/program-versions", ProgramVersionRead,
            ["TEST-COMBO-V2", "TEST-COMBO-V1"],
        )
        # code
        _assert_list_route(
            client, "/tvet/sectors", TVETSectorRead,
            ["TEST-SECTOR-ENG", "TEST-SECTOR-ICT"],
        )
        # program code — the first of the two TVET routes
        _assert_list_route(
            client, "/tvet/programs", TVETProgramRead,
            ["TEST-TVET-PROG", "TEST-TVET-PROG-2"], field="program_code",
        )
        # name, then school_code
        _assert_list_route(
            client, "/schools", SchoolRead,
            ["TEST-SCHOOL-001", "TEST-SCHOOL-002"], field="school_code",
        )
        # A school's offerings — the second TVET-style detail route
        # (/schools/{school_code}/programs). School name and school code
        # tie for offerings of the SAME school, so the intra-school order
        # is pinned to the program-version id tiebreaker in the
        # repository's ORDER BY (a two-offering school makes it real).
        first = client.get("/api/v1/catalog/schools/TEST-SCHOOL-001/programs")
        assert first.status_code == 200, first.text
        rows = first.json()
        fields = set(SchoolProgramRead.model_fields)
        for item in rows:
            assert set(item) == fields, f"school offerings: unstable shape {set(item) ^ fields}"
        assert sorted(row["code"] for row in rows) == [
            "TEST-COMBO-V1",
            "TEST-COMBO-V2",
        ]
        version_ids = [row["program_version_id"] for row in rows]
        assert version_ids == sorted(version_ids), (
            f"school offerings: not ordered by program_version_id: {version_ids}"
        )
        second = client.get("/api/v1/catalog/schools/TEST-SCHOOL-001/programs")
        assert second.json() == rows, "school offerings: non-deterministic ordering"
        _assert_list_route(
            client, "/schools/TEST-SCHOOL-002/programs", SchoolProgramRead,
            ["TEST-COMBO-V2"],
        )

    _truncate_all(clean_db)


def test_catalog_api_filter_and_unknown_value_pins(
    clean_db, pg_engine, tmp_path: Path, monkeypatch, capsys
) -> None:
    """Pin current filter semantics: identity vs attribute unknown values."""
    csv_dir = _write_catalog(tmp_path / "csv", _sweep_catalog())
    rc, _ = _run_cli(clean_db, csv_dir, monkeypatch, capsys)
    assert rc == 0

    with _catalog_api_client(pg_engine) as client:
        # ?pathway= / ?level= / ?sector= resolve through real relationships.
        response = client.get("/api/v1/catalog/education-levels?pathway=TEST-OL")
        assert response.status_code == 200
        assert [row["code"] for row in response.json()] == ["TEST-L1"]
        response = client.get("/api/v1/catalog/education-levels?pathway=TEST-AL")
        assert [row["code"] for row in response.json()] == ["TEST-L2"]

        response = client.get(
            "/api/v1/catalog/programs?pathway=TEST-OL&level=TEST-L1"
        )
        assert response.status_code == 200
        assert [row["code"] for row in response.json()] == ["TEST-COMBO"]
        response = client.get(
            "/api/v1/catalog/programs?pathway=TEST-AL&level=TEST-L2"
        )
        assert [row["code"] for row in response.json()] == ["TEST-COMBO"]

        response = client.get("/api/v1/catalog/program-versions?pathway=TEST-OL")
        assert response.status_code == 200
        assert [row["code"] for row in response.json()] == ["TEST-COMBO-V1"]

        response = client.get("/api/v1/catalog/tvet/programs?sector=TEST-SECTOR-ENG")
        assert response.status_code == 200
        assert [row["program_code"] for row in response.json()] == [
            "TEST-TVET-PROG-2"
        ]
        response = client.get("/api/v1/catalog/tvet/programs?sector=TEST-SECTOR-ICT")
        assert [row["program_code"] for row in response.json()] == ["TEST-TVET-PROG"]

        # Unknown *identities* are pinned as 404 (documented behavior).
        for path in (
            "/education-levels?pathway=NOPE",
            "/programs?pathway=NOPE",
            "/programs?level=NOPE",
            "/program-versions?pathway=NOPE",
            "/tvet/programs?sector=NOPE",
            "/schools/NOPE/programs",
        ):
            response = client.get(f"/api/v1/catalog{path}")
            assert response.status_code == 404, path

        # Unknown *attribute values* are pinned as 200 with [] — asymmetric
        # with the identity filters above, but the documented contract
        # ("404/400 are reserved for invalid or unknown requested identities").
        for path in (
            "/academic-years?status=BOGUS",
            "/subjects?status=BOGUS",
            "/schools?province=NOPE",
            "/programs?program_type=BOGUS",
        ):
            response = client.get(f"/api/v1/catalog{path}")
            assert response.status_code == 200, path
            assert response.json() == [], path

    _truncate_all(clean_db)
