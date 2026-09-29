"""Reference dataset: schools.

Natural key:            school_code   (uq_schools_school_code_key)
Load order:             after program_subjects; before school_programs
Update policy:          update — mutable: name, school_type, province,
                        district, sector, status.

Record shape:
    school_code: str             unique official school code
    name: str                    official school name
    school_type: str, optional   free text for now — a controlled vocabulary is
                                 a later, deliberate decision (Phase 3C §7)
    province: str, optional      free text; no administrative reference tables
    district: str, optional      free text
    sector: str, optional        free text (administrative sector, not education)
    status: str, optional        RecordStatus: active (default) | inactive

``school_code`` is UNIQUE but NULLABLE in the schema; this dataset therefore
requires every source record to carry a code so the loader can match
idempotently (Phase 3C finding #1).

Phase 4B (2026-09-17), verified-minimum load:
    INTENTIONALLY EMPTY — BLOCKED ON SOURCE DATA.
    NESA's accredited-schools register (names + official school codes) was
    not retrievable this session; web search was unavailable and every
    candidate registry URL on nesa.gov.rw returned 404. The phase's school
    rule is explicit: never invent school codes, never substitute arbitrary
    identifiers, and report the limitation instead. Zero rows are authored.
    Unblocking requirement: the official NESA (or MINEDUC/REB SDMS) school
    list including per-school codes, with its academic-year reference.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="schools",
    table="schools",
    model="app.models.school.School",
    natural_key=("school_code",),
    record_fields=("name",),
    optional_fields=("school_type", "province", "district", "sector", "status"),
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   - NESA (National Examination and School Inspection Authority), home page,
#     https://www.nesa.gov.rw/ — mandate covers examinations, selection and
#     school inspection ("Basic Education and TSS Quality Standards Division"
#     supervises norms and standards). The accredited-schools register with
#     codes was NOT retrievable.
#   - MINEDUC, Basic Education, https://www.mineduc.gov.rw/basic-education —
#     counts only: 1,991 general and professional secondary schools.
#   - RTB, https://www.rtb.gov.rw/index.php/training-management-department —
#     366 TVET schools (count only).
#   - REB operates the Schools Data Management System (SDMS), referenced
#     from https://www.reb.gov.rw/ — the likely authoritative source of
#     school codes, but the register itself was not retrievable.
PROVENANCE = {
    "sources": [
        {
            "organization": "NESA",
            "title": "Home page (mandate pages only)",
            "url": "https://www.nesa.gov.rw/",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "inspection/standards mandate; school register NOT retrievable",
        },
        {
            "organization": "MINEDUC",
            "title": "Basic Education",
            "url": "https://www.mineduc.gov.rw/basic-education",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "school counts only (1,991 general/professional secondary schools)",
        },
    ],
    "status": "EMPTY — BLOCKER: official school list with codes not retrievable",
}

RECORDS: list[dict] = []
DATASET = Dataset(_SPEC, RECORDS)
