"""Reference dataset: tvet_programs.

Natural key:            program       (uq_tvet_programs_program_id_key)
Load order:             after programs and tvet_sectors; before program_versions
Update policy:          update — mutable: sector (reassignment is explicit).

Record shape:
    program: str    resolves against programs.code; one TVET profile per program
    sector: str     resolves against tvet_sectors.code

This is a profile/association dataset: it attaches a TVET sector to a program
whose program_type is "tvet_program". Whether the referenced program really
has that type is a source-data correctness question for the authoring phase;
the structural validator intentionally does not encode it.

Phase 4B (2026-09-17), verified-minimum load:
    INTENTIONALLY EMPTY — doubly blocked: needs both a program row of type
    "tvet_program" (blocked: no official trade list retrievable) and an
    official sector assignment (blocked: RTB sector catalogue not
    retrievable). No trade/sector relationship is invented.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="tvet_programs",
    table="tvet_programs",
    model="app.models.tvet_program.TVETProgram",
    natural_key=("program",),
    references={"program": "programs", "sector": "tvet_sectors"},
    fk_columns={"program": "program_id", "sector": "sector_id"},
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   No records authored; no provenance to attach. Blockers: official RTB
#   sector catalogue AND official trade/program list both not retrievable
#   (see app.data.tvet_sectors and app.data.programs).
PROVENANCE = {
    "sources": [],
    "status": "EMPTY — inherits the RTB sector-catalogue and programs blockers",
}

RECORDS: list[dict] = []
DATASET = Dataset(_SPEC, RECORDS)
