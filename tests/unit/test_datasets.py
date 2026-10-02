"""Unit tests: reference dataset modules satisfy their structural contract.

These tests require no database.
"""
from __future__ import annotations

import importlib

import pytest

from app.data._spec import EXPECTED_LOAD_ORDER, Dataset, DatasetSpec
from app.data.registry import DATASET_MODULES, load_registry, validate_registry_order
from app.data.validation import validate_datasets


def test_all_dataset_modules_import() -> None:
    for module_name in DATASET_MODULES:
        module = importlib.import_module(module_name)
        assert isinstance(module.DATASET, Dataset), f"{module_name} must export a Dataset"


def test_registry_contains_exactly_the_12_declared_datasets() -> None:
    datasets = load_registry()
    assert [d.name for d in datasets] == list(EXPECTED_LOAD_ORDER)
    assert len(datasets) == 12


def test_load_order_matches_declared_fk_safe_order() -> None:
    datasets = load_registry()
    assert validate_registry_order(datasets) == []


def test_every_dataset_exports_valid_spec_and_records() -> None:
    for dataset in load_registry():
        spec = dataset.spec
        assert isinstance(spec, DatasetSpec)
        assert spec.natural_key, f"{spec.name}: natural key must be declared"
        assert spec.table, f"{spec.name}: table must be declared"
        assert spec.model.startswith("app.models."), (
            f"{spec.name}: model path must point at app.models"
        )
        assert isinstance(dataset.records, list)


def test_every_reference_has_fk_mapping_and_earlier_parent() -> None:
    datasets = load_registry()
    names = [d.name for d in datasets]
    for dataset in datasets:
        for field_name, ref_name in dataset.spec.references.items():
            assert field_name in dataset.spec.fk_columns, (
                f"{dataset.name}.{field_name}: missing fk_columns mapping"
            )
            assert ref_name in names, f"{dataset.name}: unknown parent dataset {ref_name}"
            assert names.index(ref_name) < names.index(dataset.name), (
                f"{dataset.name}: parent {ref_name} must load earlier"
            )


def test_no_dataset_targets_a_protected_table() -> None:
    from app.data.loader import PROTECTED_TABLES

    for dataset in load_registry():
        assert dataset.spec.table not in PROTECTED_TABLES


def test_program_versions_natural_key_is_not_code() -> None:
    """Phase 3C contract: version identity is the offering tuple, never code."""
    program_versions = {d.name: d for d in load_registry()}["program_versions"]
    assert program_versions.spec.natural_key == ("program", "academic_year", "pathway", "level")
    assert "code" in program_versions.spec.optional_fields
    assert program_versions.spec.update_policy == "verify"


def test_academic_years_use_verify_policy() -> None:
    academic_years = {d.name: d for d in load_registry()}["academic_years"]
    assert academic_years.spec.natural_key == ("name",)
    assert academic_years.spec.update_policy == "verify"


def test_tvet_programs_requires_sector_record_field() -> None:
    """sector is NOT NULL in the model and unconditional in the record shape.

    With it missing from the allowed fields, the validator rejected every
    record carrying it (unknown field) while omitting it failed NOT NULL.
    """
    tvet_programs = {d.name: d for d in load_registry()}["tvet_programs"]
    assert "sector" in tvet_programs.spec.key_fields
    assert "sector" in tvet_programs.spec.references


@pytest.mark.parametrize(
    "dataset_name,natural_key",
    [
        ("academic_years", ("name",)),
        ("pathways", ("code",)),
        ("education_levels", ("code",)),
        ("pathway_levels", ("pathway", "level")),
        ("subjects", ("code",)),
        ("programs", ("code",)),
        ("tvet_sectors", ("code",)),
        ("tvet_programs", ("program",)),
        ("program_versions", ("program", "academic_year", "pathway", "level")),
        ("program_subjects", ("version_key", "subject")),
        ("schools", ("school_code",)),
        ("school_programs", ("school", "version_key")),
    ],
)
def test_declared_natural_keys(dataset_name: str, natural_key: tuple) -> None:
    datasets = {d.name: d for d in load_registry()}
    assert datasets[dataset_name].spec.natural_key == natural_key


def test_empty_datasets_pass_validation() -> None:
    assert validate_datasets(load_registry()) == []
