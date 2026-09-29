"""Reference dataset: programs.

Natural key:            code          (uq_programs_code_key)
Load order:             after subjects; before tvet_programs, program_versions
Update policy:          update — mutable: name, program_type, description, status.

Record shape:
    code: str                    unique program code — the authoritative, stable
                                 program identity (uq_programs_code_key)
    name: str                    human-readable program name
    program_type: str, optional  ProgramType: combination | tvet_program |
                                 stream | other (default: other)
    description: str, optional
    status: str, optional        RecordStatus: active (default) | inactive

``programs.code`` is the identity every consumer must resolve programs by —
never by a program_versions label (Phase 3C §8).

Phase 4B (2026-09-17), verified-minimum load:
    INTENTIONALLY EMPTY — BLOCKED ON SOURCE DATA.
    General-education A-Level subject combinations (PCM, PCB, HGL, ...) and
    their officially applicable status could not be established from a
    retrievable official source this session. The MINEDUC reform announced
    2026-08-10 reportedly restructures upper-secondary general education
    toward pathways, and the phase rules forbid assuming historical
    combinations are current without the official transition position —
    which was not retrievable. Loading historical combinations as current,
    or inventing the transition date, are both prohibited; so zero program
    rows are authored.

    Downstream consequence (accepted, deliberate): program_versions,
    program_subjects, tvet_programs and school_programs also stay empty
    because every one of them references a program.

    Unblocking requirement: retrieve the official position — either the
    MINEDUC/REB reform circular establishing the new structure and its
    first applicable academic year, or (if combinations remain applicable
    for a given cohort) the official combination list for that cohort.
    Adding programs later is a plain INSERT; no loaded row changes.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="programs",
    table="programs",
    model="app.models.program.Program",
    natural_key=("code",),
    record_fields=("name",),
    optional_fields=("program_type", "description", "status"),
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   - MINEDUC newsroom, https://www.mineduc.gov.rw/updates — item dated
#     2026-08-10: "The Ministry of education introduces new reforms to
#     strengthen teaching, learning and student development". Headline
#     topics: secondary education, primary education. Full text NOT
#     retrievable; the upper-secondary restructure (if any) and its
#     effective academic year are UNRESOLVED.
#   - No official enumeration of current A-Level combinations was
#     retrievable (REB curriculum pages and NESA school/combination lists
#     were unreachable this session).
PROVENANCE = {
    "sources": [
        {
            "organization": "MINEDUC",
            "title": "News — new reforms announced 2026-08-10 (full text not retrievable)",
            "url": "https://www.mineduc.gov.rw/updates",
            "date": "2026-08-10",
            "accessed": "2026-09-17",
            "extracted": "reform existence only; combination structure impact UNRESOLVED",
        },
    ],
    "status": "EMPTY — BLOCKER: official current combination/program list not retrievable",
}

RECORDS: list[dict] = []
DATASET = Dataset(_SPEC, RECORDS)
