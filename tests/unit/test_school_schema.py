"""Unit tests: school catalog schema contract.

Regression guard for the Phase 5E contract fix: ``SchoolRead`` previously
inherited the generic ``code`` field from ``CatalogBase`` even though the
``schools`` table has no ``code`` column — so validating any real school
row failed (invisible while the catalog was empty).
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.school import School
from app.schemas.catalog import SchoolRead


def _school_session() -> Session:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_school_read_validates_real_row() -> None:
    session = _school_session()
    try:
        school = School(name="APagi Secondary", school_code="TS001", district="Gasabo")
        session.add(school)
        session.commit()
        session.refresh(school)

        payload = SchoolRead.model_validate(school)
        assert payload.school_code == "TS001"
        assert payload.name == "APagi Secondary"
        assert payload.id == school.id
        assert payload.district == "Gasabo"
    finally:
        session.close()


def test_school_read_has_no_generic_code_field() -> None:
    assert "code" not in SchoolRead.model_fields
    assert "school_code" in SchoolRead.model_fields
