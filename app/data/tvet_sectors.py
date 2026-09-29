"""Reference dataset: tvet_sectors.

Natural key:            code          (uq_tvet_sectors_code_key)
Load order:             after programs; before tvet_programs
Update policy:          update — mutable: name, description, status.

Record shape:
    code: str                unique sector code
    name: str                human-readable sector name
    description: str, optional
    status: str, optional    RecordStatus: active (default) | inactive

Phase 4B (2026-09-17), verified-minimum load:
    INTENTIONALLY EMPTY — BLOCKED ON SOURCE DATA.
    RTB's official list of TVET sectors (and the trades within each) was
    not retrievable this session: the RTB site's sector/curriculum pages
    returned 404 and web search was unavailable. Well-known sector names
    circulate in secondary sources, but the phase rules forbid treating
    secondary lists as authoritative when an official source exists, and
    forbid inventing official-looking codes. Zero rows are authored.
    Unblocking requirement: the RTB official sector/trade catalogue
    (e.g. the RTB curriculum department's published list).
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="tvet_sectors",
    table="tvet_sectors",
    model="app.models.tvet_sector.TVETSector",
    natural_key=("code",),
    record_fields=("name",),
    optional_fields=("description", "status"),
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   - RTB (Rwanda TVET Board), home page + Training Management Department,
#     https://www.rtb.gov.rw/ , https://www.rtb.gov.rw/index.php/training-management-department
#     — 366 TVET schools, 102,485 trainees, 5 special academies; a
#     "Curriculum & instructional materials development" department is the
#     stated custodian of TVET curricula. The sector/trade catalogue itself
#     was NOT retrievable (department/curriculum pages returned 404).
PROVENANCE = {
    "sources": [
        {
            "organization": "RTB (Rwanda TVET Board)",
            "title": "Training management department",
            "url": "https://www.rtb.gov.rw/index.php/training-management-department",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "TVET school/trainee statistics only; sector list NOT retrievable",
        },
    ],
    "status": "EMPTY — BLOCKER: official RTB sector catalogue not retrievable",
}

RECORDS: list[dict] = []
DATASET = Dataset(_SPEC, RECORDS)
