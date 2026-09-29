"""Reference dataset: pathway_levels.

Natural key:            (pathway, level)
Load order:             after pathways and education_levels; before
                        program_versions
Update policy:          update — association rows have no mutable fields.

Record shape:
    pathway: str    resolves against pathways.code
    level: str      resolves against education_levels.code

Each record means "this pathway spans this education level" — a pure M2M
association, idempotent via uq_pathway_levels_pathway_id_education_level_id_key.

Phase 4B (2026-09-17), verified-minimum load:
    O_LEVEL  -> S1, S2, S3        (lower secondary = Ordinary Level)
    A_LEVEL  -> S4, S5, S6        (upper secondary = Advanced Level)
    TVET     -> L3, L4, L5        (RTB qualification Levels 3-5 at secondary age)
    TTC      -> S4, S5, S6        (TTCs are upper-secondary-age institutions;
                                   per MINEDUC the 17 public TTCs train primary
                                   teachers at the upper-secondary level)
    Every row is the standard structural reading of the Rwandan 6-3-3 system
    plus the RTB level span. No pathway/level pair is asserted beyond these
    verified rules (e.g. TVET is NOT mapped to S1-S6, and O-Level is NOT
    mapped to L1/L2 — those levels are not even in the catalogue).
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="pathway_levels",
    table="pathway_levels",
    model="app.models.pathway_level.PathwayLevel",
    natural_key=("pathway", "level"),
    references={"pathway": "pathways", "level": "education_levels"},
    fk_columns={"pathway": "pathway_id", "level": "education_level_id"},
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   - MINEDUC, Basic Education,
#     https://www.mineduc.gov.rw/basic-education — pre-primary/primary/
#     secondary sub-sector composition; general vs professional secondary.
#   - MINEDUC, Technical and Vocational Training,
#     https://www.mineduc.gov.rw/technical-and-vocational-training — TVET
#     Levels 1-5.
#   - NESA, https://www.nesa.gov.rw/ — O-Level examinations terminate lower
#     secondary (2025/2026 school year news, item 2026-07-07).
# Notes: the pathway<->level matrix below is the direct, uncontroversial
# reading of those sources (3+3 secondary years; TVET levels 3-5 at
# secondary age; TTC at upper-secondary age). Confidence: HIGH.
PROVENANCE = {
    "sources": [
        {
            "organization": "MINEDUC",
            "title": "Basic Education",
            "url": "https://www.mineduc.gov.rw/basic-education",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "secondary = lower + upper (3+3); general/professional split",
        },
        {
            "organization": "MINEDUC",
            "title": "Technical and Vocational Training",
            "url": "https://www.mineduc.gov.rw/technical-and-vocational-training",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "TVET qualification Levels 1-5",
        },
        {
            "organization": "NESA",
            "title": "Home page — 2025/2026 national examinations news",
            "url": "https://www.nesa.gov.rw/",
            "date": "2026-07-07 (item date)",
            "accessed": "2026-09-17",
            "extracted": "O-Level terminates lower secondary",
        },
    ],
    "status": "verified mapping; no speculative pathway/level pairs included",
}

RECORDS: list[dict] = [
    # O-Level spans lower secondary.
    {"pathway": "O_LEVEL", "level": "S1"},
    {"pathway": "O_LEVEL", "level": "S2"},
    {"pathway": "O_LEVEL", "level": "S3"},
    # A-Level spans upper secondary.
    {"pathway": "A_LEVEL", "level": "S4"},
    {"pathway": "A_LEVEL", "level": "S5"},
    {"pathway": "A_LEVEL", "level": "S6"},
    # TVET spans RTB qualification Levels 3-5 (secondary age).
    {"pathway": "TVET", "level": "L3"},
    {"pathway": "TVET", "level": "L4"},
    {"pathway": "TVET", "level": "L5"},
    # Teacher Training Colleges operate at the upper-secondary age band.
    {"pathway": "TTC", "level": "S4"},
    {"pathway": "TTC", "level": "S5"},
    {"pathway": "TTC", "level": "S6"},
]

DATASET = Dataset(_SPEC, RECORDS)
