"""Reference dataset: program_versions.

Natural key:            (program, academic_year, pathway, level)
                        — mirrors uq_program_versions_offering_key
                        (program_id, academic_year_id, pathway_id,
                         education_level_id)
Load order:             after programs, academic_years, pathways,
                        education_levels, tvet_programs;
                        before program_subjects, school_programs
Update policy:          verify — versions are create-or-verify-only. The loader
                        NEVER rewrites an existing version merely because source
                        data changed; drift is reported for a human to decide.

Record shape:
    program: str        resolves against programs.code
    academic_year: str  resolves against academic_years.name
    pathway: str        resolves against pathways.code
    level: str          resolves against education_levels.code
    code: str, optional          human-facing version code (defaults to program)
    name: str, optional          human-facing version name (defaults to program)
    description: str, optional
    effective_from: str, optional    ISO date
    effective_until: str, optional   ISO date, >= effective_from (DB CHECK)
    status: str, optional    RecordStatus: active (default) | inactive

``code`` is a human-facing label and is NEVER used as identity.

Phase 4B (2026-09-17), verified-minimum load:
    INTENTIONALLY EMPTY — inherits the programs dataset blocker: every
    version references a program, and zero program rows were authorable
    (no official current combination/program list retrievable). Identity
    remains the four-column offering tuple; when programs are unblocked,
    versions are plain INSERTs keyed on that tuple.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="program_versions",
    table="program_versions",
    model="app.models.program_version.ProgramVersion",
    natural_key=("program", "academic_year", "pathway", "level"),
    optional_fields=(
        "code", "name", "description",
        "effective_from", "effective_until", "status",
    ),
    references={
        "program": "programs",
        "academic_year": "academic_years",
        "pathway": "pathways",
        "level": "education_levels",
    },
    fk_columns={
        "program": "program_id",
        "academic_year": "academic_year_id",
        "pathway": "pathway_id",
        "level": "education_level_id",
    },
    update_policy="verify",
    label_field="code",
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   No records authored; no provenance to attach. See app.data.programs
#   for the blocking source gap (official combination/program list not
#   retrievable; 2026-08-10 MINEDUC reform impact unresolved).
PROVENANCE = {
    "sources": [],
    "status": "EMPTY — inherits the programs dataset blocker (no offering rows authorable)",
}

RECORDS: list[dict] = []
DATASET = Dataset(_SPEC, RECORDS)
