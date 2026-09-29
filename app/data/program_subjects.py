"""Reference dataset: program_subjects.

Natural key:            (version_key, subject)
Load order:             after program_versions and subjects
Update policy:          update — mutable: subject_type, is_required,
                        display_order, but only before enrollments reference
                        the version (service-layer concern, not the seeder's).

Record shape:
    version_key: [program, academic_year, pathway, level]
                         resolves positionally against one program_versions
                         record's natural key
    subject: str         resolves against subjects.code
    subject_type: str, optional  ProgramSubjectType: core | elective |
                                 optional | module (default: core)
    is_required: bool, optional  default True
    display_order: int, optional default 0

Idempotent via uq_program_subjects_program_version_id_subject_id_key.

Phase 4B (2026-09-17), verified-minimum load:
    INTENTIONALLY EMPTY — doubly blocked: needs both a program_versions row
    (itself blocked on programs) and an authoritative subject-role mapping
    (core/elective/optional per subject per combination), which no
    retrievable official source establishes. The 14 loaded subjects are a
    shared vocabulary; which subjects belong to which offering is NOT
    inferable and is therefore not fabricated.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="program_subjects",
    table="program_subjects",
    model="app.models.program_subject.ProgramSubject",
    natural_key=("version_key", "subject"),
    optional_fields=("subject_type", "is_required", "display_order"),
    references={"version_key": "program_versions", "subject": "subjects"},
    fk_columns={"version_key": "program_version_id", "subject": "subject_id"},
    label_field="subject",
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   No records authored; no provenance to attach. Blockers: the programs /
#   program_versions source gap (see app.data.programs) plus the absence of
#   any retrievable official subject-role (core/elective) mapping.
PROVENANCE = {
    "sources": [],
    "status": "EMPTY — inherits the programs blocker; subject-role mapping also not retrievable",
}

RECORDS: list[dict] = []
DATASET = Dataset(_SPEC, RECORDS)
