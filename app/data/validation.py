"""Structural validation of reference datasets.

Validates every dataset's records against its ``DatasetSpec`` *without* any
database connection:

- record shape: dict values, required fields present, no unknown keys
- natural-key uniqueness within the dataset (duplicate detection)
- parent references resolve: a scalar reference matches the parent's single
  natural-key value; a list reference (``version_key``) matches the parent's
  full natural-key tuple positionally; the referenced dataset must appear
  earlier in the load order
- load order matches the declared FK-safe order

This runs as the pre-write gate inside the seed loader and standalone in the
unit-test suite. It deliberately knows nothing about individual datasets: new
datasets only need a correct ``DatasetSpec``.
"""
from __future__ import annotations

from collections.abc import Iterable

from app.data._spec import Dataset
from app.data.registry import validate_registry_order


def _reference_pool(parent: Dataset) -> set:
    """Values a child may use to point at a parent record.

    Single-field parents expose their natural-key values directly; composite
    parents expose full natural-key tuples (for positional list references).
    """
    if len(parent.spec.natural_key) == 1:
        return {record[parent.spec.natural_key[0]] for record in parent.records}
    return {tuple(record[f] for f in parent.spec.natural_key) for record in parent.records}


def validate_datasets(datasets: Iterable[Dataset]) -> list[str]:
    """Validate all datasets against their specs; return a list of problems.

    An empty result means every dataset satisfies its structural contract.
    """
    datasets = list(datasets)
    problems = validate_registry_order(datasets)
    by_name: dict[str, Dataset] = {dataset.name: dataset for dataset in datasets}

    for position, dataset in enumerate(datasets):
        spec = dataset.spec

        # Every referenced dataset must exist and be loaded earlier (FK safety).
        for field_name, ref_name in sorted(spec.references.items()):
            parent = by_name.get(ref_name)
            if parent is None:
                problems.append(
                    f"{spec.name}: references unknown dataset {ref_name!r} (field {field_name!r})"
                )
                continue
            parent_position = next(
                i for i, d in enumerate(datasets) if d.name == ref_name
            )
            if parent_position >= position:
                problems.append(
                    f"{spec.name}: reference {field_name!r} -> {ref_name!r} "
                    f"must be loaded earlier (order violation)"
                )

        seen: dict[tuple, int] = {}
        for index, record in enumerate(dataset.records):
            label = f"{spec.name}[{index}]"

            if not isinstance(record, dict):
                problems.append(
                    f"{label}: record must be a dict, got {type(record).__name__}"
                )
                continue

            # Required fields (natural key + record fields).
            for field_name in spec.key_fields:
                if field_name not in record:
                    problems.append(f"{label}: missing required field {field_name!r}")

            # Unknown fields are typos waiting to happen — reject them.
            allowed = set(spec.key_fields) | set(spec.optional_fields)
            for field_name in sorted(set(record) - allowed):
                problems.append(
                    f"{label}: unknown field {field_name!r} "
                    f"(allowed: {', '.join(sorted(allowed))})"
                )

            # Natural-key values must be present and non-empty.
            try:
                key = spec.lookup_key(record)
            except KeyError:
                continue  # already reported as a missing required field
            if any(value is None or value == "" for value in key):
                problems.append(
                    f"{label}: natural key {spec.natural_key} has an empty value"
                )
                continue
            # A natural key may contain a list (composite reference such as
            # program_versions' "version_key"); normalize it so the key is
            # hashable for the duplicate lookup.
            hashable_key = tuple(
                tuple(value) if isinstance(value, list) else value
                for value in key
            )
            duplicate_index = seen.get(hashable_key)
            if duplicate_index is not None:
                problems.append(
                    f"{label}: duplicate natural key {key} "
                    f"(already used by {spec.name}[{duplicate_index}])"
                )
            else:
                seen[hashable_key] = index

            # Every declared reference must have an fk_columns mapping...
            for field_name in spec.references:
                if field_name not in spec.fk_columns:
                    problems.append(
                        f"{spec.name}: reference {field_name!r} has no fk_columns mapping"
                    )

            # ...and every reference value must resolve to a parent record.
            for field_name, ref_name in sorted(spec.references.items()):
                parent = by_name.get(ref_name)
                if parent is None or field_name not in record:
                    continue  # unknown-dataset / missing-field already reported
                value = record[field_name]
                candidate = tuple(value) if isinstance(value, list) else value
                if candidate not in _reference_pool(parent):
                    problems.append(
                        f"{label}: reference {field_name!r} does not resolve "
                        f"in dataset {ref_name!r}: {value!r}"
                    )

    return problems
