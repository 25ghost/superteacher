"""Dataset registry — explicit import of every reference dataset module.

The load order here is the single source of truth for the seeder (import
order, never filesystem order) and is validated against the FK-safe
``EXPECTED_LOAD_ORDER`` declared in ``app.data._spec``.
"""
from __future__ import annotations

import importlib

from app.data._spec import EXPECTED_LOAD_ORDER, Dataset

# Ordered dataset modules; parents strictly before children.
DATASET_MODULES: tuple[str, ...] = (
    "app.data.academic_years",
    "app.data.pathways",
    "app.data.education_levels",
    "app.data.pathway_levels",
    "app.data.subjects",
    "app.data.programs",
    "app.data.tvet_sectors",
    "app.data.tvet_programs",
    "app.data.program_versions",
    "app.data.program_subjects",
    "app.data.schools",
    "app.data.school_programs",
)


def load_registry() -> list[Dataset]:
    """Import every dataset module in order and return its ``DATASET``."""
    datasets: list[Dataset] = []
    for module_name in DATASET_MODULES:
        module = importlib.import_module(module_name)
        dataset = getattr(module, "DATASET", None)
        if not isinstance(dataset, Dataset):
            raise TypeError(f"{module_name} does not export a Dataset named DATASET")
        datasets.append(dataset)
    return datasets


def validate_registry_order(datasets: list[Dataset]) -> list[str]:
    """Return problems if the registry order deviates from the declared order."""
    problems: list[str] = []
    names = [dataset.name for dataset in datasets]
    if names != list(EXPECTED_LOAD_ORDER):
        problems.append(
            f"dataset load order {names} does not match the declared "
            f"FK-safe order {list(EXPECTED_LOAD_ORDER)}"
        )
    return problems
