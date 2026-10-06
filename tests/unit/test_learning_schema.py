"""Unit tests: the Phase 1 learning tables, migration 0010, drift parity.

Slice 1 of the learning marketplace adds three tables (``learning_contexts``
``teaching_offerings`` ``learning_enrollments``) and the teacher
``verification_status`` column. Model, migration and the offline
verification script must agree — the drift tests below run
``scripts/verify_database.py``'s own schema descriptors, which compare the
ORM metadata against the parsed migration DDL column by column,
constraint by constraint.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.enums import (
    LearningEnrollmentStatus,
    TeacherVerificationStatus,
    TeachingOfferingStatus,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_spec = importlib.util.spec_from_file_location(
    "verify_database", BACKEND_DIR / "scripts" / "verify_database.py"
)
verify_database = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_database", verify_database)
_spec.loader.exec_module(verify_database)

_NEW_TABLES = ("learning_contexts", "teaching_offerings", "learning_enrollments")


def _table(name: str):
    return Base.metadata.tables[name]


def test_learning_tables_are_registered_with_expected_columns() -> None:
    assert set(_NEW_TABLES) <= set(Base.metadata.tables)
    assert set(_table("learning_contexts").columns.keys()) == {
        "id",
        "academic_year_id",
        "pathway_id",
        "education_level_id",
        "program_version_id",
        "subject_id",
        "created_at",
        "updated_at",
    }
    context = _table("learning_contexts")
    assert context.columns["program_version_id"].nullable is True
    for column in (
        "academic_year_id",
        "pathway_id",
        "education_level_id",
        "subject_id",
    ):
        assert context.columns[column].nullable is False

    assert set(_table("teaching_offerings").columns.keys()) == {
        "id",
        "teacher_id",
        "learning_context_id",
        "description",
        "status",
        "created_at",
        "updated_at",
    }
    assert _table("teaching_offerings").columns["description"].nullable is True

    enrollments = _table("learning_enrollments")
    assert set(enrollments.columns.keys()) == {
        "id",
        "student_id",
        "teaching_offering_id",
        "learning_context_id",
        "status",
        "started_at",
        "ended_at",
        "created_at",
        "updated_at",
    }
    assert enrollments.columns["ended_at"].nullable is True
    assert enrollments.columns["started_at"].nullable is False


def test_teacher_verification_column_and_check() -> None:
    teachers = _table("teachers")
    assert "verification_status" in teachers.columns
    column = teachers.columns["verification_status"]
    assert column.nullable is False
    check = next(
        c
        for c in teachers.constraints
        if isinstance(c, CheckConstraint)
        and c.name == "teachers_verification_status_check"
    )
    stored = sorted(re.findall(r"'([^']+)'", str(check.sqltext)))
    assert stored == sorted(member.value for member in TeacherVerificationStatus)
    assert TeacherVerificationStatus.PENDING.value == "pending"


def test_new_table_foreign_keys_follow_the_naming_convention() -> None:
    expectations = {
        "learning_contexts": {
            "learning_contexts_academic_year_id_fkey": "academic_years.id",
            "learning_contexts_pathway_id_fkey": "pathways.id",
            "learning_contexts_education_level_id_fkey": "education_levels.id",
            "learning_contexts_program_version_id_fkey": "program_versions.id",
            "learning_contexts_subject_id_fkey": "subjects.id",
        },
        "teaching_offerings": {
            "teaching_offerings_teacher_id_fkey": "teachers.id",
            "teaching_offerings_learning_context_id_fkey": "learning_contexts.id",
        },
        "learning_enrollments": {
            "learning_enrollments_student_id_fkey": "students.id",
            "learning_enrollments_teaching_offering_id_fkey": "teaching_offerings.id",
            "learning_enrollments_learning_context_id_fkey": "learning_contexts.id",
        },
    }
    for table_name, expected in expectations.items():
        fks = {
            c.name: c
            for c in _table(table_name).constraints
            if isinstance(c, ForeignKeyConstraint)
        }
        assert set(fks) == set(expected), (table_name, sorted(fks))
        for name, target in expected.items():
            assert fks[name].elements[0].target_fullname == target


def test_partial_unique_indexes_enforce_the_phase_one_rules() -> None:
    """One active offer per (teacher, context); one active enrollment per
    (student, context); one context per natural key (split around the
    nullable program version)."""

    def _index(table: str, name: str):
        return next(i for i in _table(table).indexes if i.name == name)

    offering_idx = _index(
        "teaching_offerings", "uq_teaching_offerings_teacher_context_active_key"
    )
    assert offering_idx.unique is True
    assert [c.name for c in offering_idx.columns] == [
        "teacher_id",
        "learning_context_id",
    ]
    assert str(offering_idx.dialect_options["postgresql"]["where"]) == "status = 'active'"
    assert str(offering_idx.dialect_options["sqlite"]["where"]) == "status = 'active'"

    enrollment_idx = _index(
        "learning_enrollments", "uq_learning_enrollments_student_context_active_key"
    )
    assert enrollment_idx.unique is True
    assert [c.name for c in enrollment_idx.columns] == [
        "student_id",
        "learning_context_id",
    ]
    assert str(enrollment_idx.dialect_options["postgresql"]["where"]) == "status = 'active'"

    with_program = _index(
        "learning_contexts", "uq_learning_contexts_with_program_key"
    )
    assert with_program.unique is True
    assert [c.name for c in with_program.columns] == [
        "academic_year_id",
        "pathway_id",
        "education_level_id",
        "program_version_id",
        "subject_id",
    ]
    assert (
        str(with_program.dialect_options["postgresql"]["where"])
        == "program_version_id IS NOT NULL"
    )

    without_program = _index(
        "learning_contexts", "uq_learning_contexts_without_program_key"
    )
    assert without_program.unique is True
    assert [c.name for c in without_program.columns] == [
        "academic_year_id",
        "pathway_id",
        "education_level_id",
        "subject_id",
    ]
    assert (
        str(without_program.dialect_options["postgresql"]["where"])
        == "program_version_id IS NULL"
    )


def test_status_vocabularies_match_the_new_enum_checks() -> None:
    offerings = _table("teaching_offerings")
    check = next(
        c
        for c in offerings.constraints
        if isinstance(c, CheckConstraint) and c.name == "teaching_offerings_status_check"
    )
    assert sorted(re.findall(r"'([^']+)'", str(check.sqltext))) == sorted(
        member.value for member in TeachingOfferingStatus
    )

    enrollments = _table("learning_enrollments")
    check = next(
        c
        for c in enrollments.constraints
        if isinstance(c, CheckConstraint)
        and c.name == "learning_enrollments_status_check"
    )
    assert sorted(re.findall(r"'([^']+)'", str(check.sqltext))) == sorted(
        member.value for member in LearningEnrollmentStatus
    )
    date_check = next(
        c
        for c in enrollments.constraints
        if isinstance(c, CheckConstraint)
        and c.name == "learning_enrollments_date_order_check"
    )
    assert "ended_at" in str(date_check.sqltext)


def _migration_head() -> str:
    """The last revision declared across the migration files (in file order)."""
    import ast

    head = ""
    for path in verify_database._migration_files():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "revision":
                head = ast.literal_eval(node.value)
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "revision"
            ):
                head = ast.literal_eval(node.value)
    return head


def test_verification_script_covers_the_new_tables_and_head() -> None:
    assert verify_database.EXPECTED_REVISION == _migration_head()
    for table in _NEW_TABLES:
        assert table in verify_database.EXPECTED_TABLES
        assert table in verify_database.LATER_MIGRATION_TABLES


def test_truncation_list_covers_the_new_tables() -> None:
    """Integration cleanup must truncate the new tables too."""
    import conftest as tests_conftest

    for table in _NEW_TABLES:
        assert table in tests_conftest.APPLICATION_TABLES


def test_new_tables_and_verification_check_have_no_model_migration_drift() -> None:
    """The objects this slice introduces must match migration 0010 exactly."""
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )

    problems = verify_database.diff_schemas(
        {t: metadata_schema[t] for t in (*_NEW_TABLES, "teachers")},
        {t: migration_schema[t] for t in (*_NEW_TABLES, "teachers")},
        "models",
        "migrations",
    )
    assert problems == [], problems

    assert verify_database._checks_match(
        metadata_schema["teachers"]["checks"]["teachers_verification_status_check"],
        migration_schema["teachers"]["checks"]["teachers_verification_status_check"],
    )


def test_slice_adds_no_new_drift_problems() -> None:
    """The whole-schema comparison may not gain a problem naming the new tables."""
    metadata_schema = verify_database.schema_from_metadata()
    migration_schema = verify_database._combined_migration_schema(
        verify_database._migration_files()
    )
    problems = verify_database.diff_schemas(
        metadata_schema, migration_schema, "models", "migrations"
    )
    introduced = [
        p
        for p in problems
        if any(name in p for name in (*_NEW_TABLES, "teachers_verification_status_check"))
    ]
    assert introduced == [], introduced
