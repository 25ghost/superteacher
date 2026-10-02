"""Unit tests: the structural validator catches real dataset mistakes.

These tests require no database. They build tiny synthetic datasets to prove
each detection rule, without introducing any real catalog data.
"""
from __future__ import annotations

from app.data._spec import Dataset, DatasetSpec
from app.data.validation import validate_datasets

_PARENT_SPEC = DatasetSpec(
    name="parents",
    table="parents_table",
    model="app.models.subject.Subject",
    natural_key=("code",),
    record_fields=("name",),
)
_CHILD_SPEC = DatasetSpec(
    name="children",
    table="children_table",
    model="app.models.subject.Subject",
    natural_key=("parent", "code"),
    references={"parent": "parents"},
    fk_columns={"parent": "parent_id"},
)

# The validator compares load order against the declared EXPECTED_LOAD_ORDER,
# so synthetic datasets are injected via monkeypatched order in real datasets'
# tests; for synthetic ones we validate shape only by monkeypatching the
# registry-order checker.


def _validate_ignoring_global_order(datasets):
    from app.data import validation as validation_module

    original = validation_module.validate_registry_order
    validation_module.validate_registry_order = lambda _datasets: []
    try:
        return validate_datasets(datasets)
    finally:
        validation_module.validate_registry_order = original


def test_detects_duplicate_natural_key() -> None:
    parents = Dataset(_PARENT_SPEC, [
        {"code": "X", "name": "first"},
        {"code": "X", "name": "second"},
    ])
    problems = _validate_ignoring_global_order([parents])
    assert any("duplicate natural key" in p for p in problems)


def test_detects_missing_required_field() -> None:
    parents = Dataset(_PARENT_SPEC, [{"code": "X"}])  # name missing
    problems = _validate_ignoring_global_order([parents])
    assert any("missing required field 'name'" in p for p in problems)


def test_detects_unknown_field() -> None:
    parents = Dataset(_PARENT_SPEC, [{"code": "X", "name": "ok", "typo": 1}])
    problems = _validate_ignoring_global_order([parents])
    assert any("unknown field 'typo'" in p for p in problems)


def test_detects_empty_natural_key_value() -> None:
    parents = Dataset(_PARENT_SPEC, [{"code": "", "name": "ok"}])
    problems = _validate_ignoring_global_order([parents])
    assert any("empty value" in p for p in problems)


def test_detects_unresolvable_parent_reference() -> None:
    parents = Dataset(_PARENT_SPEC, [{"code": "A", "name": "ok"}])
    children = Dataset(_CHILD_SPEC, [{"parent": "NOPE", "code": "c1"}])
    problems = _validate_ignoring_global_order([parents, children])
    assert any("does not resolve" in p for p in problems)


def test_detects_reference_without_fk_mapping() -> None:
    broken_spec = DatasetSpec(
        name="children",
        table="children_table",
        model="app.models.subject.Subject",
        natural_key=("parent", "code"),
        references={"parent": "parents"},
        # fk_columns deliberately missing
    )
    parents = Dataset(_PARENT_SPEC, [{"code": "A", "name": "ok"}])
    children = Dataset(broken_spec, [{"parent": "A", "code": "c1"}])
    problems = _validate_ignoring_global_order([parents, children])
    assert any("no fk_columns mapping" in p for p in problems)


def test_detects_unknown_parent_dataset() -> None:
    broken_spec = DatasetSpec(
        name="children",
        table="children_table",
        model="app.models.subject.Subject",
        natural_key=("parent", "code"),
        references={"parent": "ghost_dataset"},
        fk_columns={"parent": "parent_id"},
    )
    children = Dataset(broken_spec, [{"parent": "A", "code": "c1"}])
    problems = _validate_ignoring_global_order([children])
    assert any("unknown dataset" in p for p in problems)


def test_valid_synthetic_datasets_pass() -> None:
    parents = Dataset(_PARENT_SPEC, [{"code": "A", "name": "ok"}])
    children = Dataset(_CHILD_SPEC, [{"parent": "A", "code": "c1"}])
    assert _validate_ignoring_global_order([parents, children]) == []


_VERSIONS_SPEC = DatasetSpec(
    name="versions",
    table="versions_table",
    model="app.models.subject.Subject",
    natural_key=("program", "code"),
    record_fields=("name",),
)
_COMPOSITE_CHILD_SPEC = DatasetSpec(
    name="children",
    table="children_table",
    model="app.models.subject.Subject",
    natural_key=("version_key", "code"),
    references={"version_key": "versions"},
    fk_columns={"version_key": "version_id"},
)


def test_composite_list_natural_key_is_hashable() -> None:
    """A list-valued natural-key part must not crash the duplicate lookup."""
    versions = Dataset(_VERSIONS_SPEC, [{"program": "P", "code": "V1", "name": "n"}])
    children = Dataset(_COMPOSITE_CHILD_SPEC, [
        {"version_key": ["P", "V1"], "code": "c1"},
    ])
    assert _validate_ignoring_global_order([versions, children]) == []


def test_detects_duplicate_composite_natural_key() -> None:
    versions = Dataset(_VERSIONS_SPEC, [{"program": "P", "code": "V1", "name": "n"}])
    children = Dataset(_COMPOSITE_CHILD_SPEC, [
        {"version_key": ["P", "V1"], "code": "c1"},
        {"version_key": ["P", "V1"], "code": "c1"},
    ])
    problems = _validate_ignoring_global_order([versions, children])
    assert any("duplicate natural key" in p for p in problems)
