"""Unit tests: CSV catalog parser (no PostgreSQL required).

Proves the format contract: exact headers, typed values, empty-cell rules
(including the schools.school_code special case), composite reference
encoding, and that every parse error reports file, line and column.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.data.csv_input import (
    CsvParseError,
    allowed_columns,
    load_csv_datasets,
    required_columns,
)
from app.data.registry import EXPECTED_LOAD_ORDER, load_registry
from app.data.validation import validate_datasets

SPECS = {dataset.name: dataset.spec for dataset in load_registry()}


def _header_only(name: str) -> str:
    return ",".join(required_columns(SPECS[name])) + "\n"


def _csv_dir(tmp_path: Path, overrides: dict[str, str] | None = None) -> Path:
    """A directory holding all 12 required files; overrides replace entries.

    Override keys are dataset names, with or without the ``.csv`` suffix.
    """
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    normalized = {
        key.removesuffix(".csv"): value for key, value in (overrides or {}).items()
    }
    for name in EXPECTED_LOAD_ORDER:
        content = normalized.get(name, _header_only(name))
        (csv_dir / f"{name}.csv").write_text(content, encoding="utf-8")
    return csv_dir


def test_required_and_allowed_columns_match_specs() -> None:
    for name in EXPECTED_LOAD_ORDER:
        spec = SPECS[name]
        assert allowed_columns(spec) == [*spec.key_fields, *spec.optional_fields]


def test_program_versions_requires_code_and_name_despite_optional_spec() -> None:
    """NOT NULL columns without defaults must be CSV columns."""
    spec = SPECS["program_versions"]
    assert "code" in required_columns(spec)
    assert "name" in required_columns(spec)
    assert "status" not in required_columns(spec)  # has a database default


def test_tvet_programs_requires_sector() -> None:
    assert "sector" in required_columns(SPECS["tvet_programs"])


def test_load_csv_datasets_returns_all_12_in_declared_order(tmp_path: Path) -> None:
    datasets = load_csv_datasets(_csv_dir(tmp_path))
    assert [dataset.name for dataset in datasets] == list(EXPECTED_LOAD_ORDER)
    assert all(dataset.records == [] for dataset in datasets)


def test_missing_directory_is_a_parse_error(tmp_path: Path) -> None:
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(tmp_path / "nope")
    assert "not a directory" in str(excinfo.value)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    csv_dir = _csv_dir(tmp_path)
    (csv_dir / "schools.csv").unlink()
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    assert "missing required file 'schools.csv'" in str(excinfo.value)


def test_unknown_file_name_is_reported(tmp_path: Path) -> None:
    csv_dir = _csv_dir(tmp_path)
    (csv_dir / "school.csv").write_text(_header_only("schools"), encoding="utf-8")
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    assert "school.csv: unknown dataset file" in str(excinfo.value)


def test_unknown_header_column_reports_file_line_column(tmp_path: Path) -> None:
    csv_dir = _csv_dir(tmp_path, {"pathways.csv": "code,name,id\nP1,Path,9\n"})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "pathways.csv: line 1, column 'id'" in problem
    assert "unknown column" in problem


def test_missing_required_column_reports_file_line_column(tmp_path: Path) -> None:
    csv_dir = _csv_dir(tmp_path, {"schools.csv": "school_code\nTEST-SCHOOL-001\n"})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "schools.csv: line 1, column 'name'" in problem
    assert "required column is missing" in problem


def test_file_without_header_row_reports_exact_message(tmp_path: Path) -> None:
    csv_dir = _csv_dir(tmp_path)
    (csv_dir / "subjects.csv").write_text("", encoding="utf-8")
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    assert excinfo.value.problems == [
        "subjects.csv: line 1, column 'header': missing header row"
    ]


def test_duplicate_header_column_reports_exact_message(tmp_path: Path) -> None:
    csv_dir = _csv_dir(tmp_path, {"subjects.csv": "code,name,code\n"})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    assert excinfo.value.problems == [
        "subjects.csv: line 1, column 'code': duplicate header column"
    ]


def test_empty_school_code_is_rejected_even_though_column_is_nullable(
    tmp_path: Path,
) -> None:
    """The README rule: school_code may be nullable in the model, never in the CSV."""
    csv_dir = _csv_dir(
        tmp_path, {"schools.csv": "school_code,name\n,Test School\n"}
    )
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "schools.csv: line 2, column 'school_code'" in problem
    assert "empty value in natural-key column" in problem


def test_empty_nullable_cell_becomes_none(tmp_path: Path) -> None:
    csv_dir = _csv_dir(
        tmp_path, {"schools.csv": "school_code,name,province\nTEST-SCHOOL-001,Test School,\n"}
    )
    datasets = load_csv_datasets(csv_dir)
    record = _records(datasets, "schools")[0]
    assert record["province"] is None


def test_empty_non_nullable_cell_is_rejected(tmp_path: Path) -> None:
    csv_dir = _csv_dir(tmp_path, {"pathways.csv": "code,name\nTEST-OL,\n"})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "pathways.csv: line 2, column 'name'" in problem
    assert "empty value in non-nullable column" in problem


def test_empty_reference_cell_reports_exact_message(tmp_path: Path) -> None:
    csv_dir = _csv_dir(
        tmp_path, {"tvet_programs.csv": "program,sector\nTEST-PROG,\n"}
    )
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    assert excinfo.value.problems == [
        "tvet_programs.csv: line 2, column 'sector': empty value in non-nullable column"
    ]


def test_bad_boolean_reports_file_line_column(tmp_path: Path) -> None:
    content = "version_key,subject,is_required\nA|B|C|D,S1,yes\n"
    csv_dir = _csv_dir(tmp_path, {"program_subjects.csv": content})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "program_subjects.csv: line 2, column 'is_required'" in problem
    assert "expected true or false" in problem


def test_boolean_true_false_parse_to_bool(tmp_path: Path) -> None:
    content = "version_key,subject,is_required,display_order\nA|B|C|D,S1,false,2\n"
    csv_dir = _csv_dir(tmp_path, {"program_subjects.csv": content})
    record = _records(load_csv_datasets(csv_dir), "program_subjects")[0]
    assert record["is_required"] is False
    assert record["display_order"] == 2


def test_bad_date_reports_file_line_column(tmp_path: Path) -> None:
    content = "name,start_date,end_date,status\nTEST-YEAR,01/09/2099,31/07/2100,active\n"
    csv_dir = _csv_dir(tmp_path, {"academic_years.csv": content})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "academic_years.csv: line 2, column 'start_date'" in problem
    assert "expected an ISO 8601 date (YYYY-MM-DD)" in problem


def test_iso_date_is_accepted_as_string(tmp_path: Path) -> None:
    content = "name,start_date,end_date,status\nTEST-YEAR,2099-09-01,2100-07-31,active\n"
    csv_dir = _csv_dir(tmp_path, {"academic_years.csv": content})
    record = _records(load_csv_datasets(csv_dir), "academic_years")[0]
    assert record["start_date"] == "2099-09-01"


def test_bad_integer_reports_file_line_column(tmp_path: Path) -> None:
    content = "code,name,level_number\nTEST-L1,Test Level,one\n"
    csv_dir = _csv_dir(tmp_path, {"education_levels.csv": content})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "education_levels.csv: line 2, column 'level_number'" in problem
    assert "expected an integer" in problem


def test_composite_reference_parses_to_list(tmp_path: Path) -> None:
    content = "school,version_key\nTEST-SCHOOL-001,A|B|C|D\n"
    csv_dir = _csv_dir(tmp_path, {"school_programs.csv": content})
    record = _records(load_csv_datasets(csv_dir), "school_programs")[0]
    assert record["version_key"] == ["A", "B", "C", "D"]


def test_composite_reference_wrong_part_count_reports_file_line_column(
    tmp_path: Path,
) -> None:
    content = "school,version_key\nTEST-SCHOOL-001,A|B\n"
    csv_dir = _csv_dir(tmp_path, {"school_programs.csv": content})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "school_programs.csv: line 2, column 'version_key'" in problem
    assert "4 parts joined with '|'" in problem


def test_composite_reference_empty_part_reports_exact_message(
    tmp_path: Path,
) -> None:
    csv_dir = _csv_dir(
        tmp_path,
        {
            "school_programs.csv": (
                "school,version_key\n"
                "TEST-SCHOOL-001,TEST-COMBO||TEST-OL|TEST-L1\n"
            )
        },
    )
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    assert excinfo.value.problems == [
        "school_programs.csv: line 2, column 'version_key': "
        "composite reference has an empty part"
    ]


def test_row_with_more_cells_than_header_is_rejected(tmp_path: Path) -> None:
    csv_dir = _csv_dir(tmp_path, {"pathways.csv": "code,name\nTEST-OL,Path,extra\n"})
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problem = str(excinfo.value)
    assert "pathways.csv: line 2" in problem
    assert "more cells than header columns" in problem


def test_all_parse_errors_are_collected_before_raising(tmp_path: Path) -> None:
    csv_dir = _csv_dir(
        tmp_path,
        {
            "pathways.csv": "code,name\nTEST-OL,\n",
            "subjects.csv": "code,name\n,Test Subject\n",
        },
    )
    with pytest.raises(CsvParseError) as excinfo:
        load_csv_datasets(csv_dir)
    problems = excinfo.value.problems
    assert len(problems) == 2
    assert any(p.startswith("pathways.csv") for p in problems)
    assert any(p.startswith("subjects.csv") for p in problems)


def test_parser_allows_structural_errors_for_the_validation_gate(tmp_path: Path) -> None:
    """Duplicate natural keys parse fine — validation.py owns that gate."""
    content = "code,name\nTEST-SUB,One\nTEST-SUB,Two\n"
    csv_dir = _csv_dir(tmp_path, {"subjects.csv": content})
    datasets = load_csv_datasets(csv_dir)
    problems = validate_datasets(datasets)
    assert any("duplicate natural key" in problem for problem in problems)


def test_missing_from_input_reports_untouched_db_rows(tmp_path: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.core.database import Base
    import app.models  # noqa: F401
    from app.data.csv_input import missing_from_input
    from app.models.education_level import EducationLevel
    from app.models.pathway import Pathway
    from app.models.pathway_level import PathwayLevel

    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    try:
        pathway_a = Pathway(code="A", name="Path A")
        pathway_b = Pathway(code="B", name="Path B")
        level_one = EducationLevel(code="L1", name="One", level_number=1)
        level_two = EducationLevel(code="L2", name="Two", level_number=2)
        session.add_all([pathway_a, pathway_b, level_one, level_two])
        session.flush()
        session.add_all([
            PathwayLevel(pathway_id=pathway_a.id, education_level_id=level_one.id),
            PathwayLevel(pathway_id=pathway_a.id, education_level_id=level_two.id),
        ])
        session.commit()

        datasets = load_csv_datasets(
            _csv_dir(
                tmp_path,
                {
                    "pathways.csv": "code,name\nA,Path A\n",
                    "education_levels.csv": "code,name,level_number\nL1,One,1\n",
                    "pathway_levels.csv": "pathway,level\nA,L1\n",
                },
            )
        )
        reports = {report.dataset: report for report in missing_from_input(session, datasets)}
        assert reports["pathways"].missing == ["B"]
        assert reports["education_levels"].missing == ["L2"]
        assert reports["pathway_levels"].missing == ["pathway=A, level=L2"]
        assert reports["subjects"].missing == []
    finally:
        session.close()
        engine.dispose()


def _records(datasets, name: str) -> list[dict]:
    return [dataset.records for dataset in datasets if dataset.name == name][0]
