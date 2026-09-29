"""Reference dataset: pathways.

Natural key:            code          (uq_pathways_code_key)
Load order:             after academic_years; before pathway_levels,
                        program_versions
Update policy:          update — mutable: name, description, status.

Record shape:
    code: str                unique pathway code
    name: str                human-readable pathway name
    description: str, optional
    status: str, optional    RecordStatus: active (default) | inactive

Pathway codes are data rows, never hard-coded logic.

Phase 4B (2026-09-17), verified-minimum load:
    Codes O_LEVEL, A_LEVEL, TVET are established Rwandan system vocabulary
    (O-Level/Ordinary Level ends S3 with the O-Level national examination;
    A-Level/Advanced Level covers S4-S6; TVET is the technical/vocational
    stream at Levels 1-5). TTC (Teacher Training Colleges) is added as an
    internal catalogue code — "TTC" is real system vocabulary, but the code
    string itself is an internal identifier, not an official abbreviation.

    The MINEDUC reform announced 2026-08-10 reportedly restructures upper
    secondary general education, but its full text was not retrievable this
    session, so NO pathway was marked inactive and NO new pathway was
    invented from the reform headlines. The restructure is handled as
    uncertainty at the program/combination level, not by mutating pathway
    rows. O-Level (S1-S3) is unaffected by that reform and is current.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="pathways",
    table="pathways",
    model="app.models.pathway.Pathway",
    natural_key=("code",),
    record_fields=("name",),
    optional_fields=("description", "status"),
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   - MINEDUC, Basic Education,
#     https://www.mineduc.gov.rw/basic-education — "learners in 1,991 general
#     and professional secondary schools"; general vs professional (TVET)
#     secondary streams. Information current.
#   - MINEDUC, Technical and Vocational Training,
#     https://www.mineduc.gov.rw/technical-and-vocational-training —
#     "TVET trainees ... Level 1-5". Information current.
#   - MINEDUC newsroom, https://www.mineduc.gov.rw/updates — item dated
#     2026-08-10: "The Ministry of education introduces new reforms to
#     strengthen teaching, learning and student development". The full
#     article text was NOT retrievable; whether/when it changes the
#     upper-secondary pathway structure is UNRESOLVED.
#   - Wikipedia, "Education in Rwanda",
#     https://en.wikipedia.org/wiki/Education_in_Rwanda — SECONDARY source,
#     used only for cross-checking structure vocabulary (O-Level/A-Level/
#     TVET/TTC); no value in this dataset is sourced from it alone.
PROVENANCE = {
    "sources": [
        {
            "organization": "MINEDUC",
            "title": "Basic Education",
            "url": "https://www.mineduc.gov.rw/basic-education",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "general + professional secondary streams",
        },
        {
            "organization": "MINEDUC",
            "title": "Technical and Vocational Training",
            "url": "https://www.mineduc.gov.rw/technical-and-vocational-training",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "TVET Levels 1-5",
        },
        {
            "organization": "MINEDUC",
            "title": "News — new reforms announced 2026-08-10 (full text not retrievable)",
            "url": "https://www.mineduc.gov.rw/updates",
            "date": "2026-08-10",
            "accessed": "2026-09-17",
            "extracted": "existence of the reform only; structure impact UNRESOLVED",
        },
    ],
    "status": "pathway rows verified as system vocabulary; codes are internal identifiers",
}

RECORDS: list[dict] = [
    {
        "code": "O_LEVEL",
        "name": "O-Level (Ordinary Level)",
        "description": (
            "Lower secondary general education, S1-S3, ending with the "
            "O-Level national examination. Current structure."
        ),
        "status": "active",
    },
    {
        "code": "A_LEVEL",
        "name": "A-Level (Advanced Level)",
        "description": (
            "Upper secondary general education, S4-S6, ending with the "
            "A-Level national examination. Subject-combination model; a "
            "MINEDUC reform announced 2026-08-10 may restructure this level "
            "(transition UNRESOLVED — see dataset docstring)."
        ),
        "status": "active",
    },
    {
        "code": "TVET",
        "name": "Technical and Vocational Education and Training",
        "description": (
            "Technical secondary / vocational stream governed by the Rwanda "
            "TVET Board (RTB), qualification Levels 1-5 (L3-L5 at upper "
            "secondary age). Current structure."
        ),
        "status": "active",
    },
    {
        "code": "TTC",
        "name": "Teacher Training Colleges",
        "description": (
            "Pre-service lower-secondary teacher training colleges under "
            "REB. Code is an internal catalogue identifier, not an official "
            "abbreviation."
        ),
        "status": "active",
    },
]

DATASET = Dataset(_SPEC, RECORDS)
