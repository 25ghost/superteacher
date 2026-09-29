"""Dataset specification primitives for the reference-data seeder.

This module defines:

- ``DatasetSpec``: a static descriptor each dataset module exports as
  ``DATASET``, declaring its natural key, required fields, parent
  references, FK columns, and update policy. The seed loader and the
  structural validator are both generic over these declarations — adding a
  new dataset never requires touching their logic.
- ``Dataset``: a spec joined with its actual record list (exported by each
  dataset module as ``RECORDS``).
- ``EXPECTED_LOAD_ORDER``: the explicit, foreign-key-safe load order. It is
  deliberately hard-coded, not inferred from the filesystem: the dependency
  graph is small, known, and worth being able to read at a glance (and to
  test).

Shared reference data belongs to the whole SuperTeacher backend; Student
Registration is merely its first consumer.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DatasetSpec:
    """Static description of one reference dataset.

    Attributes:
        name: Dataset (and module) name, e.g. ``"pathways"``.
        table: SQLAlchemy table name the dataset loads into.
        model: Dotted model class path the dataset loads rows through.
        natural_key: Column names that identify a record for idempotent
            upserts. Each must be backed by a UNIQUE constraint in the
            schema (Phase 3C verified every one of them). A natural-key
            field may itself be a reference field (see ``program_versions``).
        record_fields: Field names every record dict must provide in
            addition to the natural key.
        optional_fields: Field names a record dict may provide.
        references: Maps a dataset-internal *reference field* to the dataset
            name it points at, e.g. ``{"pathway": "pathways"}``. A scalar
            value must match the parent's (single-field) natural key; a
            list value must match the parent's full natural-key tuple
            positionally (see ``version_key``). The referenced dataset must
            appear earlier in the load order.
        fk_columns: Maps each reference field to the child model's FK
            attribute it populates, e.g. ``{"level": "education_level_id"}``.
            Required for every declared reference.
        update_policy: What happens when a record matches an existing row:
            ``"update"`` applies the dataset's mutable fields in place;
            ``"verify"`` never updates — any drift is reported as a warning
            for a human to decide (used for ``academic_years`` and
            ``program_versions``, whose history must never be rewritten by
            a re-run).
        label_field: Field used by the dry-run report for context.
    """

    name: str
    table: str
    model: str
    natural_key: tuple[str, ...]
    record_fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    references: dict[str, str] = field(default_factory=dict)
    fk_columns: dict[str, str] = field(default_factory=dict)
    update_policy: str = "update"  # "update" | "verify"
    label_field: str = "name"

    @property
    def key_fields(self) -> tuple[str, ...]:
        """All required fields: natural key plus record fields."""
        return self.natural_key + self.record_fields

    def lookup_key(self, record: dict) -> tuple:
        """Return the tuple that identifies ``record`` within the dataset."""
        return tuple(record[f] for f in self.natural_key)

    def reference_value(self, record: dict, field_name: str) -> str | list[str]:
        """Return the value ``record`` uses to point at its parent record."""
        return record[field_name]


@dataclass
class Dataset:
    """A dataset spec joined with its actual records."""

    spec: DatasetSpec
    records: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.spec.name


# Explicit foreign-key-safe load order. Parents strictly before children;
# do not reorder without checking the FK graph in migration 0001.
EXPECTED_LOAD_ORDER: tuple[str, ...] = (
    "academic_years",      # referenced by program_versions
    "pathways",            # referenced by pathway_levels, program_versions
    "education_levels",    # referenced by pathway_levels, program_versions
    "pathway_levels",      # parents: pathways, education_levels
    "subjects",            # referenced by program_subjects
    "programs",            # referenced by tvet_programs, program_versions
    "tvet_sectors",        # referenced by tvet_programs
    "tvet_programs",       # parents: programs, tvet_sectors
    "program_versions",    # parents: programs, academic_years, pathways, education_levels
    "program_subjects",    # parents: program_versions, subjects
    "schools",             # referenced by school_programs
    "school_programs",     # parents: schools, program_versions
)
