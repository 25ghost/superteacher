"""Unit tests: catalog schemas (serialization contract, no database).

Proves the Pydantic read models serialize the exact field shapes the API
promises — including UUID handling and the program-version identity fields.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.schemas.catalog import (
    AcademicYearRead,
    EducationLevelRead,
    ProgramRead,
    ProgramVersionRead,
    PathwayRead,
    SchoolProgramRead,
    SchoolRead,
    SubjectRead,
    TVETProgramRead,
    TVETSectorRead,
)


def _uuid(value: str) -> UUID:
    return UUID(int=int(value, 16) & ((1 << 128) - 1)) if all(c in "0123456789abcdef" for c in value) else uuid4()


def test_academic_year_roundtrip() -> None:
    row = AcademicYearRead(
        id=uuid4(),
        name="2025/2026",
        start_date=date(2025, 9, 1),
        end_date=date(2026, 7, 31),
        status="closed",
    )
    assert row.model_dump()["name"] == "2025/2026"
    assert row.start_date == date(2025, 9, 1)


def test_academic_year_rejects_missing_status() -> None:
    with pytest.raises(ValidationError):
        AcademicYearRead(
            id=uuid4(),
            name="x",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 2),
        )


def test_pathway_and_subject_share_catalog_base_fields() -> None:
    pathway = PathwayRead(
        id=uuid4(), code="O_LEVEL", name="O-Level", description=None, status="active"
    )
    subject = SubjectRead(id=uuid4(), code="SUB_MATH", name="Mathematics", status="active")
    assert pathway.code == "O_LEVEL"
    assert subject.code == "SUB_MATH"
    # description is optional everywhere.
    assert pathway.description is None


def test_education_level_serializes_level_number() -> None:
    level = EducationLevelRead(
        id=uuid4(), code="S1", name="Senior 1", level_number=7, status="active"
    )
    assert level.level_number == 7


def test_program_type_is_required() -> None:
    program = ProgramRead(
        id=uuid4(), code="PCM", name="Physics-Chemistry-Math",
        description=None, status="active", program_type="combination",
    )
    assert program.program_type == "combination"
    with pytest.raises(ValidationError):
        ProgramRead(id=uuid4(), code="X", name="X", status="active")


def test_program_version_identity_fields() -> None:
    row = ProgramVersionRead(
        id=uuid4(),
        program_id=uuid4(),
        program_code="PCM",
        academic_year_name="2025/2026",
        pathway_code="A_LEVEL",
        level_code="S4",
        code="PCM-2026",
        name="PCM 2026",
        description=None,
        effective_from=None,
        effective_until=None,
        status="active",
    )
    # Identity = the offering tuple, NOT program_versions.code.
    assert (row.program_code, row.academic_year_name, row.pathway_code, row.level_code) == (
        "PCM", "2025/2026", "A_LEVEL", "S4",
    )
    assert row.code == "PCM-2026"  # label only


def test_school_and_school_program_fields() -> None:
    school = SchoolRead(
        id=uuid4(), code="SCH", name="GS Test", description=None, status="active",
        school_code="010101", school_type=None, province=None, district=None, sector=None,
    )
    assert school.school_code == "010101"
    school_program = SchoolProgramRead(
        id=uuid4(), school_id=uuid4(), school_code="010101",
        program_version_id=uuid4(),
        program_code="PRG01", program_name="Test Program",
        code="PV-01", name="Version 1",
        pathway_code="O_LEVEL", level_code="S1",
        status="active",
    )
    assert school_program.status == "active"
    assert school_program.program_code == "PRG01"
    assert school_program.pathway_code == "O_LEVEL"
    assert school_program.level_code == "S1"


def test_tvet_payload_shapes() -> None:
    sector = TVETSectorRead(id=uuid4(), code="ICT", name="ICT", description=None, status="active")
    program = TVETProgramRead(
        id=uuid4(), program_id=uuid4(), program_code="SWD",
        program_name="Software Development", sector_code="ICT", sector_name="ICT",
    )
    assert sector.code == "ICT"
    assert program.sector_code == "ICT"


def test_uuid_fields_serialize_to_strings_in_json() -> None:
    row = SubjectRead(id=uuid4(), code="X", name="X", status="active")
    assert row.model_dump(mode="json")["id"] == str(row.id)
