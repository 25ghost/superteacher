"""Reference dataset: education_levels.

Natural key:            code          (uq_education_levels_code_key)
Load order:             after pathways; before pathway_levels, program_versions
Update policy:          update — mutable: name, level_number, description, status.

Record shape:
    code: str                unique level code
    name: str                human-readable level name
    level_number: int        ordinal used for ordering/reporting
    description: str, optional
    status: str, optional    RecordStatus: active (default) | inactive

Phase 4B (2026-09-17), verified-minimum load:
    S1-S3: Ordinary Level of lower secondary (O-Level pathway). Officially
    attested vocabulary: O-Level national examinations are administered by
    NESA (attested for the 2025/2026 school year).
    S4-S6: Advanced Level of upper secondary (A-Level pathway).
    L3-L5: RTB TVET qualification levels; the "Level 1-5" span is officially
    attested on MINEDUC's TVET page; only L3-L5 fall in the secondary-school
    age range this catalogue covers (L1-L2 are out of MVP scope and are NOT
    loaded — they can be added later as plain INSERTs).

    level_number is an internal, monotonic ordinal for ordering/reporting
    (7..12 mirroring S1..S6, 13..15 for L3..L5). It is a catalogue value,
    not an official government numbering.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="education_levels",
    table="education_levels",
    model="app.models.education_level.EducationLevel",
    natural_key=("code",),
    record_fields=("name", "level_number"),
    optional_fields=("description", "status"),
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   - MINEDUC, Technical and Vocational Training,
#     https://www.mineduc.gov.rw/technical-and-vocational-training —
#     "Number of TVET trainees in 581 TVET schools, Level 1-5". Attests the
#     TVET level span; the L3/L4/L5 split at secondary age follows the RTB
#     TVET qualifications framework (framework document NOT retrievable this
#     session — the level names themselves are standard RTB vocabulary).
#   - NESA, https://www.nesa.gov.rw/ — national examinations incl. O-Level
#     for the 2025/2026 school year (item dated 2026-07-07). Attests S1-S3
#     ending in O-Level examinations.
#   - Wikipedia (SECONDARY, cross-check only) — 6-3-3 structure: 6 primary +
#     3 lower secondary + 3 upper secondary.
PROVENANCE = {
    "sources": [
        {
            "organization": "MINEDUC",
            "title": "Technical and Vocational Training",
            "url": "https://www.mineduc.gov.rw/technical-and-vocational-training",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "TVET Levels 1-5 span",
        },
        {
            "organization": "NESA",
            "title": "Home page — 2025/2026 national examinations news",
            "url": "https://www.nesa.gov.rw/",
            "date": "2026-07-07 (item date)",
            "accessed": "2026-09-17",
            "extracted": "O-Level examination stage (end of lower secondary)",
        },
    ],
    "status": "level codes verified as system vocabulary; level_number is an internal ordinal",
}

RECORDS: list[dict] = [
    {
        "code": "S1",
        "name": "Senior 1",
        "level_number": 7,
        "description": "First year of lower secondary (O-Level pathway).",
        "status": "active",
    },
    {
        "code": "S2",
        "name": "Senior 2",
        "level_number": 8,
        "description": "Second year of lower secondary (O-Level pathway).",
        "status": "active",
    },
    {
        "code": "S3",
        "name": "Senior 3",
        "level_number": 9,
        "description": (
            "Final year of lower secondary; concludes with the O-Level "
            "national examination."
        ),
        "status": "active",
    },
    {
        "code": "S4",
        "name": "Senior 4",
        "level_number": 10,
        "description": "First year of upper secondary (A-Level pathway).",
        "status": "active",
    },
    {
        "code": "S5",
        "name": "Senior 5",
        "level_number": 11,
        "description": "Second year of upper secondary (A-Level pathway).",
        "status": "active",
    },
    {
        "code": "S6",
        "name": "Senior 6",
        "level_number": 12,
        "description": (
            "Final year of upper secondary; concludes with the A-Level "
            "national examination."
        ),
        "status": "active",
    },
    {
        "code": "L3",
        "name": "TVET Level 3",
        "level_number": 13,
        "description": (
            "RTB TVET qualification Level 3 (entry trades; post-O-Level)."
        ),
        "status": "active",
    },
    {
        "code": "L4",
        "name": "TVET Level 4",
        "level_number": 14,
        "description": "RTB TVET qualification Level 4.",
        "status": "active",
    },
    {
        "code": "L5",
        "name": "TVET Level 5",
        "level_number": 15,
        "description": (
            "RTB TVET qualification Level 5 (advanced diploma pathway; "
            "bridges to polytechnic higher education)."
        ),
        "status": "active",
    },
]

DATASET = Dataset(_SPEC, RECORDS)
