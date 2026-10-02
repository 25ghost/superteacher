"""CSV front-end for the catalog loader (scripts/load_catalog.py).

Format contract (documented in README, section "Catalog CSV loader"):

- One file per registry dataset: ``<dataset>.csv`` inside the operator's
  ``--csv-dir``. All 12 files are required; any other ``*.csv`` in the
  directory is rejected, so a typo like ``school.csv`` can never silently
  mean "no input".
- The header is exactly the dataset's spec field names — natural key,
  record and optional fields — in any order. Database ids never appear;
  foreign references carry the referenced row's natural key, resolved by
  the loader (never by the operator).
- A composite natural key (the ``version_key`` field) is the parent's
  natural-key parts joined with ``|``, e.g.
  ``TEST-COMBO|TEST-2099/2100|TEST-OL|TEST-L1``.
- Dates are ISO 8601 (``YYYY-MM-DD``), booleans are ``true``/``false``
  (anything else is an error), integers are plain digits. Cell values
  are trimmed of surrounding whitespace.
- An empty cell means NULL only where the column is nullable; an empty
  cell in a non-nullable column is an error. An empty natural-key cell is
  ALWAYS an error — including nullable natural keys such as
  ``schools.school_code`` (nullable in the model, rejected by the CSV:
  the same rejection rule as ``app/data/validation.py`` applies).
- Parse errors report file, line and column.

Nothing here touches the database: this module produces registry-shaped
``Dataset`` objects. The structural validator (``app/data/validation.py``)
and the loader engine (``app/data/loader.py``) own everything after that,
including the pre-write gate and reference resolution against the DB.
"""
from __future__ import annotations

import csv
import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Boolean, Date, Integer, select

from app.core.database import Base
import app.models  # noqa: F401  (registers every table in Base.metadata)
from app.data._spec import Dataset, DatasetSpec
from app.data.registry import EXPECTED_LOAD_ORDER, load_registry

# Sentinel csv.DictReader key for rows carrying more cells than the header.
_EXTRA_CELLS = "__extra_cells__"


class CsvParseError(Exception):
    """Raised before any database input is built when CSV files are malformed."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            f"{len(problems)} CSV parse problem(s):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def allowed_columns(spec: DatasetSpec) -> list[str]:
    """Every column a ``<dataset>.csv`` header may contain, in spec order."""
    return list(dict.fromkeys([*spec.key_fields, *spec.optional_fields]))


def required_columns(spec: DatasetSpec) -> list[str]:
    """Columns the header must contain.

    Natural key + record fields, plus any allowed field whose model column
    is NOT NULL without a default — those cannot be left to the database
    (e.g. ``program_versions.code``/``name``).
    """
    required = list(spec.key_fields)
    for field in allowed_columns(spec):
        if field in required:
            continue
        column = _model_column(spec, field)
        if column is not None and not column.nullable and _has_no_default(column):
            required.append(field)
    return required


def _has_no_default(column) -> bool:
    return column.default is None and column.server_default is None


def _model_column(spec: DatasetSpec, field: str):
    """The child table column a spec field writes into (FK column for references)."""
    table = Base.metadata.tables.get(spec.table)
    if table is None:
        return None
    return table.columns.get(spec.fk_columns.get(field, field))


def _type_error(field: str, column, raw: str) -> str | None:
    """Validate ``raw`` against the column type; return an error message or None."""
    if isinstance(column.type, Boolean):
        if raw.lower() not in ("true", "false"):
            return f"expected true or false, got {raw!r}"
        return None
    if isinstance(column.type, Integer):
        try:
            int(raw)
        except ValueError:
            return f"expected an integer, got {raw!r}"
        return None
    if isinstance(column.type, Date):
        try:
            datetime.date.fromisoformat(raw)
        except ValueError:
            return f"expected an ISO 8601 date (YYYY-MM-DD), got {raw!r}"
        return None
    return None


def _convert(column, raw: str):
    """Convert a validated cell into the record value the engine expects."""
    if isinstance(column.type, Boolean):
        return raw.lower() == "true"
    if isinstance(column.type, Integer):
        return int(raw)
    if isinstance(column.type, Date):
        return raw  # ISO string; the engine compares/inserts it like the shipped datasets
    return raw


def _coerce_cell(
    spec: DatasetSpec,
    specs_by_name: dict[str, DatasetSpec],
    field: str,
    raw: str,
    location: str,
) -> tuple[object, str | None]:
    """Turn one CSV cell into a record value, or return a formatted problem."""
    raw = raw.strip()
    column = _model_column(spec, field)
    if column is None:
        return None, f"{location}, column '{field}': field has no matching column in table {spec.table!r}"

    is_reference = field in spec.references

    # Natural-key cells are rejected when empty even if the model column is
    # nullable (schools.school_code) — same rule as app/data/validation.py.
    if field in spec.natural_key and raw == "":
        return None, (
            f"{location}, column '{field}': empty value in natural-key column "
            f"(natural keys are required even where the model column is nullable)"
        )

    if is_reference:
        if raw == "":
            if column.nullable:
                return None, None
            return None, f"{location}, column '{field}': empty value in non-nullable column"
        parent_spec = specs_by_name[spec.references[field]]
        if len(parent_spec.natural_key) > 1:
            parts = raw.split("|")
            if len(parts) != len(parent_spec.natural_key):
                return None, (
                    f"{location}, column '{field}': composite reference must have "
                    f"{len(parent_spec.natural_key)} parts joined with '|' "
                    f"(got {len(parts)})"
                )
            if any(part == "" for part in parts):
                return None, f"{location}, column '{field}': composite reference has an empty part"
            return parts, None
        return raw, None

    if raw == "":
        if column.nullable:
            return None, None
        return None, f"{location}, column '{field}': empty value in non-nullable column"

    error = _type_error(field, column, raw)
    if error:
        return None, f"{location}, column '{field}': {error}"
    return _convert(column, raw), None


def _read_file(
    path: Path,
    spec: DatasetSpec,
    specs_by_name: dict[str, DatasetSpec],
) -> tuple[list[dict], list[str]]:
    """Parse one CSV file into records; collect every problem found."""
    name = path.name
    problems: list[str] = []
    records: list[dict] = []
    allowed = allowed_columns(spec)

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, restkey=_EXTRA_CELLS, restval="")
        header = reader.fieldnames
        if not header:
            return [], [f"{name}: line 1, column 'header': missing header row"]

        duplicates = sorted({col for col in header if header.count(col) > 1})
        for column in duplicates:
            problems.append(f"{name}: line 1, column '{column}': duplicate header column")
        for column in header:
            if column not in allowed:
                problems.append(
                    f"{name}: line 1, column '{column}': unknown column "
                    f"(allowed: {', '.join(allowed)})"
                )
        for column in required_columns(spec):
            if column not in header:
                problems.append(f"{name}: line 1, column '{column}': required column is missing")
        for column in header:
            if column in allowed and _model_column(spec, column) is None:
                problems.append(
                    f"{name}: line 1, column '{column}': field has no matching "
                    f"column in table {spec.table!r}"
                )
        if problems:
            return [], problems

        for row in reader:
            line = reader.line_num
            location = f"{name}: line {line}"
            row_problems: list[str] = []
            if _EXTRA_CELLS in row:
                row_problems.append(
                    f"{location}, column '(row)': more cells than header columns"
                )
            record: dict = {}
            for column in header:
                value, error = _coerce_cell(
                    spec, specs_by_name, column, row.get(column, ""), location
                )
                if error:
                    row_problems.append(error)
                else:
                    record[column] = value
            if row_problems:
                problems.extend(row_problems)
            else:
                records.append(record)

    return records, problems


def load_csv_datasets(csv_dir: Path) -> list[Dataset]:
    """Parse every dataset file in ``csv_dir`` into registry-shaped datasets.

    Raises :class:`CsvParseError` (with all problems collected across all
    files) when the directory or any file violates the format contract.
    Never touches the database.
    """
    registry = load_registry()
    specs = {dataset.name: dataset.spec for dataset in registry}

    if not csv_dir.is_dir():
        raise CsvParseError([f"{csv_dir}: --csv-dir is not a directory"])

    files = {candidate.stem: candidate for candidate in csv_dir.glob("*.csv")}
    problems: list[str] = []
    for dataset_name in EXPECTED_LOAD_ORDER:
        if dataset_name not in files:
            problems.append(f"{csv_dir}: missing required file '{dataset_name}.csv'")
    for stem in sorted(files):
        if stem not in specs:
            problems.append(
                f"{files[stem].name}: unknown dataset file "
                f"(expected one of: {', '.join(d + '.csv' for d in EXPECTED_LOAD_ORDER)})"
            )
    if problems:
        raise CsvParseError(problems)

    datasets: list[Dataset] = []
    for dataset_name in EXPECTED_LOAD_ORDER:
        records, file_problems = _read_file(files[dataset_name], specs[dataset_name], specs)
        problems.extend(file_problems)
        datasets.append(Dataset(specs[dataset_name], records))
    if problems:
        raise CsvParseError(problems)
    return datasets


@dataclass
class MissingReport:
    """Database rows a dataset's input file does not contain (left untouched)."""

    dataset: str
    table: str
    missing: list[str]


def _norm(value) -> tuple | str:
    if isinstance(value, (list, tuple)):
        return tuple(str(part) for part in value)
    return str(value)


def _format_key(spec: DatasetSpec, key: tuple) -> str:
    if len(spec.natural_key) == 1:
        return str(key[0])
    parts = []
    for field, value in zip(spec.natural_key, key):
        rendered = "|".join(value) if isinstance(value, tuple) else str(value)
        parts.append(f"{field}={rendered}")
    return ", ".join(parts)


def missing_from_input(session, datasets: Iterable[Dataset]) -> list[MissingReport]:
    """Report database rows that are absent from the input datasets.

    The loader never deletes: every such row stays untouched, and this
    report is how an operator learns about it. A row's natural key is
    compared in the CSV's own representation (references resolved through
    their parents, composite keys joined with ``|``).
    """
    from app.data.loader import _import_model

    datasets_list = list(datasets)
    specs = {dataset.name: dataset.spec for dataset in datasets_list}
    reports: list[MissingReport] = []

    for dataset in datasets_list:
        spec = dataset.spec
        model = _import_model(spec.model)
        input_keys = {
            tuple(_norm(part) for part in spec.lookup_key(record))
            for record in dataset.records
        }

        reference_fields = [
            field for field in spec.natural_key if field in spec.references
        ]
        parent_specs = [
            specs[spec.references[field]] for field in reference_fields
        ]
        parent_models = [_import_model(parent.model) for parent in parent_specs]

        if parent_models:
            entities = [model, *parent_models]
            statement = select(*entities)
            for parent_model, field in zip(parent_models, reference_fields):
                statement = statement.join(
                    parent_model,
                    getattr(model, spec.fk_columns[field]) == parent_model.id,
                )
        else:
            entities = [model]
            statement = select(model)

        db_keys: set[tuple] = set()
        for row in session.execute(statement):
            instance = row[0]
            parents = row[1:] if parent_models else ()
            parts = []
            parent_index = 0
            for field in spec.natural_key:
                if field in spec.references:
                    parent_spec = parent_specs[parent_index]
                    parent_instance = parents[parent_index]
                    parent_index += 1
                    if len(parent_spec.natural_key) == 1:
                        parts.append(
                            getattr(parent_instance, parent_spec.natural_key[0])
                        )
                    else:
                        parts.append(
                            tuple(
                                getattr(parent_instance, key_field)
                                for key_field in parent_spec.natural_key
                            )
                        )
                else:
                    parts.append(getattr(instance, field))
            db_keys.add(tuple(_norm(part) for part in parts))

        missing = sorted(
            _format_key(spec, key) for key in db_keys - input_keys
        )
        reports.append(MissingReport(dataset=spec.name, table=spec.table, missing=missing))
    return reports
