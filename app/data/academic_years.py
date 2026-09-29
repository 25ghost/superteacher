"""Reference dataset: academic_years.

Natural key:            name          (uq_academic_years_name_key)
Load order:             first (no parents); before program_versions
Update policy:          verify — the loader NEVER rewrites dates or status of an
                        existing row; drift is reported for a human to decide.

Record shape (required; extra keys are rejected):
    name: str        unique academic-year label
    start_date: str  ISO date, inclusive first day
    end_date: str    ISO date, must be >= start_date (DB CHECK also enforces)
    status: str      AcademicYearStatus: planned | active | closed | archived

Phase 4B (2026-09-17), verified-minimum load:
    Exactly ONE record is loaded: "2025/2026", status "closed". Its existence
    and closed state are officially attested (NESA announced the launch of the
    national examinations closing the 2025/2026 school year on 2026-07-07).
    The exact start/end dates were NOT published in any retrievable official
    source this session; the values below follow the national September-July
    school calendar and are flagged PROVISIONAL. Because the update policy is
    "verify", a later correction of these dates is a reviewed human decision,
    never a silent seed rewrite.
    No 2026/2027 record is loaded: no official planning source establishing
    its exact boundaries was retrievable, and speculative future years are
    forbidden by the phase rules. Adding it later is a safe INSERT.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="academic_years",
    table="academic_years",
    model="app.models.academic_year.AcademicYear",
    natural_key=("name",),
    record_fields=("start_date", "end_date", "status"),
    update_policy="verify",
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   - National Examination and School Inspection Authority (NESA), home page,
#     https://www.nesa.gov.rw/ — news item dated 2026-07-07: "MINEDUC yatangije
#     Ibizamini bya Leta Bisoza Amashuri Abanza bya 2025/2026" (MINEDUC, through
#     NESA, launched the national examinations closing the 2025/2026 school
#     year). Attests: the year name "2025/2026" and that it ended around
#     July 2026. Information current.
#   - Ministry of Education (MINEDUC), Basic Education page,
#     https://www.mineduc.gov.rw/basic-education — sub-sector structure
#     (pre-primary, primary, secondary). No academic-year calendar published
#     on the page.
#   - MINEDUC newsroom, https://www.mineduc.gov.rw/updates — education
#     activities dated through September 2026, consistent with a
#     September-July school calendar.
# Notes: start/end dates are PROVISIONAL (2025-09-01 / 2026-07-31), chosen to
# bracket the officially attested July 2026 examinations; exact official
# boundary dates remain SOURCE DATA UNCERTAIN and must be confirmed against a
# MINEDUC/REB official calendar before being relied upon.
PROVENANCE = {
    "sources": [
        {
            "organization": "NESA (National Examination and School Inspection Authority)",
            "title": "Home page — national examinations news for the 2025/2026 school year",
            "url": "https://www.nesa.gov.rw/",
            "date": "2026-07-07 (item date)",
            "accessed": "2026-09-17",
            "extracted": "academic year name 2025/2026; year closed ~July 2026",
        },
        {
            "organization": "MINEDUC (Ministry of Education, Rwanda)",
            "title": "Basic Education",
            "url": "https://www.mineduc.gov.rw/basic-education",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "sub-sector structure; September-July calendar convention",
        },
    ],
    "status": "verified existence; dates PROVISIONAL (SOURCE DATA UNCERTAIN)",
}

RECORDS: list[dict] = [
    {
        "name": "2025/2026",
        "start_date": "2025-09-01",  # PROVISIONAL — not officially confirmed
        "end_date": "2026-07-31",    # PROVISIONAL — examinations attested 2026-07-07
        "status": "closed",
    },
]

DATASET = Dataset(_SPEC, RECORDS)
