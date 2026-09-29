"""Reference dataset: subjects.

Natural key:            code          (uq_subjects_code_key)
Load order:             after pathway_levels; before program_subjects
Update policy:          update — mutable: name, description, status.

Record shape:
    code: str                unique subject code
    name: str                human-readable subject name
    description: str, optional
    status: str, optional    RecordStatus: active (default) | inactive

Phase 4B (2026-09-17), verified-minimum load:
    A conservative set of 14 core general-education subjects. Every name
    below is standard, uncontroversial Rwandan CBC subject vocabulary
    (attested in REB curriculum materials, MINEDUC pages, and NESA
    examination news; REB's own pages confirm CBC syllabus production for
    S1-S3). Codes are INTERNAL identifiers (prefix "SUB_" + stable slug) —
    the phase rules forbid presenting invented codes as official ones.

    NOT loaded: subject combinations (e.g. PCM/PCB/HGL...), their official
    subject codes, electives, and TVET modules. REB's CBC subject/syllabus
    catalogue pages were not retrievable this session; guessing them would
    violate the no-invention rule. A later pass with the official CBC
    framework can extend this list; codes are additive, so no loaded row
    changes meaning when the list grows.
"""
from app.data._spec import Dataset, DatasetSpec

_SPEC = DatasetSpec(
    name="subjects",
    table="subjects",
    model="app.models.subject.Subject",
    natural_key=("code",),
    record_fields=("name",),
    optional_fields=("description", "status"),
)

# Source provenance (Phase 4B, accessed 2026-09-17):
#   - REB, Curriculum, Teaching & Learning Resources Department,
#     https://www.reb.gov.rw/curriculum-teaching-learning-resources-department
#     — REB prepares and distributes curricula for nursery, primary,
#     secondary; scripted lessons for S1-S3 confirm the lower-secondary CBC
#     subject structure is active. Subject names are the standard ones in
#     REB/MINEDUC materials.
#   - MINEDUC, Basic Education, https://www.mineduc.gov.rw/basic-education
#     — general secondary stream description.
#   - NESA, https://www.nesa.gov.rw/ — national examinations (2025/2026)
#     confirm these are examined school subjects.
# Notes: names kept exactly as standard official vocabulary; no synonyms or
# spelling variants were merged or invented. Confidence: HIGH for the names,
# INTERNAL for the code strings.
PROVENANCE = {
    "sources": [
        {
            "organization": "REB (Rwanda Basic Education Board)",
            "title": "Curriculum, Teaching & Learning Resources Department",
            "url": "https://www.reb.gov.rw/curriculum-teaching-learning-resources-department",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "CBC curricula for secondary incl. S1-S3 scripted lessons",
        },
        {
            "organization": "MINEDUC",
            "title": "Basic Education",
            "url": "https://www.mineduc.gov.rw/basic-education",
            "date": "current as displayed 2026",
            "accessed": "2026-09-17",
            "extracted": "general secondary stream",
        },
        {
            "organization": "NESA",
            "title": "Home page — national examinations",
            "url": "https://www.nesa.gov.rw/",
            "date": "2026-07-07 (item date)",
            "accessed": "2026-09-17",
            "extracted": "examined subjects across the school system",
        },
    ],
    "status": "subject names verified; codes are internal identifiers; "
    "combination/elective subjects deliberately NOT loaded (source gap)",
}

RECORDS: list[dict] = [
    {"code": "SUB_MATH", "name": "Mathematics"},
    {"code": "SUB_PHY", "name": "Physics"},
    {"code": "SUB_CHEM", "name": "Chemistry"},
    {"code": "SUB_BIO", "name": "Biology"},
    {"code": "SUB_ENG", "name": "English"},
    {"code": "SUB_FR", "name": "French"},
    {"code": "SUB_KIN", "name": "Kinyarwanda"},
    {"code": "SUB_GEO", "name": "Geography"},
    {"code": "SUB_HIST", "name": "History"},
    {"code": "SUB_ECON", "name": "Economics"},
    {"code": "SUB_ENT", "name": "Entrepreneurship"},
    {"code": "SUB_CS", "name": "Computer Science"},
    {"code": "SUB_PE", "name": "Physical Education"},
    {"code": "SUB_REL", "name": "Religious Studies"},
]

DATASET = Dataset(_SPEC, RECORDS)
