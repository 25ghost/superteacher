"""Reference dataset: school_programs.

Natural key:            (school, version_key)
Load order:             after schools and program_versions (last dataset)
Update policy:          update — mutable: status only.

Record shape:
    school: str      resolves against schools.school_code
    version_key: [program, academic_year, pathway, level]
                     resolves positionally against one program_versions
                     record's natural key
    status: str, optional    RecordStatus: active (default) | inactive

Idempotent via uq_school_programs_school_id_program_version_id_key.

Phase 4B (2026-09-17), verified-minimum load:
    INTENTIONALLY EMPTY — inherits two blockers: no schools rows exist
    (official NESA school register with codes not retrievable) and no
    program_versions rows exist (official program/combination list not
    retrievable). The phase rule stands: a school offering is recorded ONLY
    when a source establishes that specific offering; nothing is inferred
    from a school's type or level.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="school_programs",
    table="school_programs",
    model="app.models.school_program.SchoolProgram",
    natural_key=("school", "version_key"),
    optional_fields=("status",),
    references={"school": "schools", "version_key": "program_versions"},
    fk_columns={"school": "school_id", "version_key": "program_version_id"},
    label_field="school",
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   No records authored; no provenance to attach. Blockers inherited from
#   app.data.schools (no official school register) and app.data.programs
#   (no official program/combination list).
PROVENANCE = {
    "sources": [],
    "status": "EMPTY — inherits the schools and programs blockers",
}

RECORDS: list[dict] = []
DATASET = Dataset(_SPEC, RECORDS)
