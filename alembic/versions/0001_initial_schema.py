"""initial SuperTeacher schema (Student Registration reference tables)

Revision ID: 0001
Revises:
Create Date: 2026-09-12

Creates the 16 tables of the SuperTeacher backend foundation — the Student
Registration module's reference/catalog tables (pathways, education levels,
schools, programs, program versions, subjects, TVET sectors/programs) plus
accounts, students, and enrollment. Future SuperTeacher modules (Curriculum,
Learning, AI Tutor, Progress, school administration, ...) build on this same
schema. Fully reversible via downgrade().

Constraint names mirror the project naming convention:
- FK  <table>_<column>_fkey
- UQ  uq_<table>_<column(s)>_key
- CK  <table>_<purpose>_check
- PK  <table>_pkey

Reconciled in Phase 3B:
- ``users_role_check`` matches ``app.models.enums.UserRole`` exactly
  (student, teacher, parent, school_admin, rahura_admin).
- ``education_levels``, ``tvet_programs`` and ``tvet_sectors`` carry both
  ``created_at`` and ``updated_at`` like every other timestamped table.
- ``program_versions`` has the natural-key unique constraint
  ``uq_program_versions_offering_key`` so the same offering
  (program + academic year + pathway + education level) cannot be duplicated.
- Reference tables' ``status`` columns are CHECK-constrained to the shared
  ``RecordStatus`` vocabulary (active, inactive).
- Foreign-key columns that are trailing in a composite unique constraint have
  their own index (PostgreSQL does not index foreign keys automatically).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. users ---------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="users_pkey"),
        sa.UniqueConstraint("email", name="uq_users_email_key"),
        sa.UniqueConstraint("phone", name="uq_users_phone_key"),
        sa.CheckConstraint(
            "role IN ('student', 'teacher', 'parent', 'school_admin', 'rahura_admin')",
            name="users_role_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'disabled')",
            name="users_status_check",
        ),
    )

    # --- 2. students --------------------------------------------------------
    op.create_table(
        "students",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("gender", sa.String(length=32), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="students_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="students_user_id_fkey"),
        sa.UniqueConstraint("user_id", name="uq_students_user_id_key"),
        sa.CheckConstraint(
            "date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE",
            name="students_date_of_birth_check",
        ),
    )
    op.create_index("students_full_name_idx", "students", ["full_name"])

    # --- 3. academic_years --------------------------------------------------
    op.create_table(
        "academic_years",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="academic_years_pkey"),
        sa.UniqueConstraint("name", name="uq_academic_years_name_key"),
        sa.CheckConstraint("end_date >= start_date", name="academic_years_date_order_check"),
        sa.CheckConstraint(
            "status IN ('planned', 'active', 'closed', 'archived')",
            name="academic_years_status_check",
        ),
    )

    # --- 4. pathways ----------------------------------------------------------
    op.create_table(
        "pathways",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pathways_pkey"),
        sa.UniqueConstraint("code", name="uq_pathways_code_key"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="pathways_status_check",
        ),
    )

    # --- 5. education_levels ----------------------------------------------------
    op.create_table(
        "education_levels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("level_number", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="education_levels_pkey"),
        sa.UniqueConstraint("code", name="uq_education_levels_code_key"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="education_levels_status_check",
        ),
    )

    # --- 6. pathway_levels (M2M) ---------------------------------------------
    op.create_table(
        "pathway_levels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pathway_id", sa.Uuid(), nullable=False),
        sa.Column("education_level_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pathway_levels_pkey"),
        sa.ForeignKeyConstraint(["pathway_id"], ["pathways.id"], name="pathway_levels_pathway_id_fkey"),
        sa.ForeignKeyConstraint(
            ["education_level_id"], ["education_levels.id"],
            name="pathway_levels_education_level_id_fkey",
        ),
        sa.UniqueConstraint(
            "pathway_id", "education_level_id",
            name="uq_pathway_levels_pathway_id_education_level_id_key",
        ),
    )
    # education_level_id is trailing in the unique constraint above.
    op.create_index(
        "pathway_levels_education_level_id_idx", "pathway_levels", ["education_level_id"]
    )

    # --- 7. programs ---------------------------------------------------------
    op.create_table(
        "programs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("program_type", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="programs_pkey"),
        sa.UniqueConstraint("code", name="uq_programs_code_key"),
        sa.CheckConstraint(
            "program_type IN ('combination', 'tvet_program', 'stream', 'other')",
            name="programs_program_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="programs_status_check",
        ),
    )

    # --- 8. program_versions ----------------------------------------------
    op.create_table(
        "program_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("pathway_id", sa.Uuid(), nullable=False),
        sa.Column("education_level_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_until", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="program_versions_pkey"),
        sa.ForeignKeyConstraint(["program_id"], ["programs.id"], name="program_versions_program_id_fkey"),
        sa.ForeignKeyConstraint(
            ["academic_year_id"], ["academic_years.id"],
            name="program_versions_academic_year_id_fkey",
        ),
        sa.ForeignKeyConstraint(["pathway_id"], ["pathways.id"], name="program_versions_pathway_id_fkey"),
        sa.ForeignKeyConstraint(
            ["education_level_id"], ["education_levels.id"],
            name="program_versions_education_level_id_fkey",
        ),
        # Natural key of an offering: the same program, in the same academic
        # year, pathway and education level, is the same offering. Named
        # compactly on purpose — spelling out all four columns would exceed
        # PostgreSQL's 63-byte identifier limit.
        sa.UniqueConstraint(
            "program_id", "academic_year_id", "pathway_id", "education_level_id",
            name="uq_program_versions_offering_key",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="program_versions_status_check",
        ),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_from IS NULL "
            "OR effective_until >= effective_from",
            name="program_versions_effective_window_check",
        ),
    )
    # No standalone program_id index: the natural-key unique index above leads
    # with program_id, so a separate index would be redundant.
    op.create_index(
        "program_versions_academic_year_id_idx", "program_versions", ["academic_year_id"]
    )
    op.create_index("program_versions_pathway_id_idx", "program_versions", ["pathway_id"])
    op.create_index(
        "program_versions_education_level_id_idx", "program_versions", ["education_level_id"]
    )

    # --- 9. subjects -------------------------------------------------------
    op.create_table(
        "subjects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="subjects_pkey"),
        sa.UniqueConstraint("code", name="uq_subjects_code_key"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="subjects_status_check",
        ),
    )

    # --- 10. program_subjects ----------------------------------------------
    op.create_table(
        "program_subjects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("program_version_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("is_required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="program_subjects_pkey"),
        sa.ForeignKeyConstraint(
            ["program_version_id"], ["program_versions.id"],
            name="program_subjects_program_version_id_fkey",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], name="program_subjects_subject_id_fkey"),
        sa.UniqueConstraint(
            "program_version_id", "subject_id",
            name="uq_program_subjects_program_version_id_subject_id_key",
        ),
        sa.CheckConstraint(
            "subject_type IN ('core', 'elective', 'optional', 'module')",
            name="program_subjects_subject_type_check",
        ),
    )
    # subject_id is trailing in the unique constraint above.
    op.create_index("program_subjects_subject_id_idx", "program_subjects", ["subject_id"])

    # --- 11. tvet_sectors ----------------------------------------------------
    op.create_table(
        "tvet_sectors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="tvet_sectors_pkey"),
        sa.UniqueConstraint("code", name="uq_tvet_sectors_code_key"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="tvet_sectors_status_check",
        ),
    )

    # --- 12. tvet_programs ---------------------------------------------------
    op.create_table(
        "tvet_programs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("sector_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="tvet_programs_pkey"),
        sa.ForeignKeyConstraint(["program_id"], ["programs.id"], name="tvet_programs_program_id_fkey"),
        sa.ForeignKeyConstraint(["sector_id"], ["tvet_sectors.id"], name="tvet_programs_sector_id_fkey"),
        sa.UniqueConstraint("program_id", name="uq_tvet_programs_program_id_key"),
    )
    # program_id is covered by its unique constraint; sector_id needs an index.
    op.create_index("tvet_programs_sector_id_idx", "tvet_programs", ["sector_id"])

    # --- 13. schools ----------------------------------------------------------
    op.create_table(
        "schools",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("school_code", sa.String(length=32), nullable=True),
        sa.Column("school_type", sa.String(length=32), nullable=True),
        sa.Column("province", sa.String(length=80), nullable=True),
        sa.Column("district", sa.String(length=80), nullable=True),
        sa.Column("sector", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="schools_pkey"),
        sa.UniqueConstraint("school_code", name="uq_schools_school_code_key"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="schools_status_check",
        ),
    )
    op.create_index("schools_name_idx", "schools", ["name"])
    op.create_index("schools_district_idx", "schools", ["district"])

    # --- 14. school_programs -------------------------------------------------
    op.create_table(
        "school_programs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("program_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="school_programs_pkey"),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], name="school_programs_school_id_fkey"),
        sa.ForeignKeyConstraint(
            ["program_version_id"], ["program_versions.id"],
            name="school_programs_program_version_id_fkey",
        ),
        sa.UniqueConstraint(
            "school_id", "program_version_id",
            name="uq_school_programs_school_id_program_version_id_key",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="school_programs_status_check",
        ),
    )
    op.create_index("school_programs_program_version_id_idx", "school_programs", ["program_version_id"])

    # --- 15. student_enrollments ----------------------------------------------
    op.create_table(
        "student_enrollments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("pathway_id", sa.Uuid(), nullable=False),
        sa.Column("education_level_id", sa.Uuid(), nullable=False),
        sa.Column("program_version_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="student_enrollments_pkey"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], name="student_enrollments_student_id_fkey"),
        sa.ForeignKeyConstraint(
            ["academic_year_id"], ["academic_years.id"],
            name="student_enrollments_academic_year_id_fkey",
        ),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], name="student_enrollments_school_id_fkey"),
        sa.ForeignKeyConstraint(["pathway_id"], ["pathways.id"], name="student_enrollments_pathway_id_fkey"),
        sa.ForeignKeyConstraint(
            ["education_level_id"], ["education_levels.id"],
            name="student_enrollments_education_level_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["program_version_id"], ["program_versions.id"],
            name="student_enrollments_program_version_id_fkey",
        ),
        sa.UniqueConstraint(
            "student_id", "academic_year_id",
            name="uq_student_enrollments_student_id_academic_year_id_key",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'completed', 'transferred', 'withdrawn', 'cancelled')",
            name="student_enrollments_status_check",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="student_enrollments_date_order_check",
        ),
    )
    op.create_index("student_enrollments_school_id_idx", "student_enrollments", ["school_id"])
    op.create_index(
        "student_enrollments_academic_year_id_idx", "student_enrollments", ["academic_year_id"]
    )
    op.create_index("student_enrollments_pathway_id_idx", "student_enrollments", ["pathway_id"])
    op.create_index(
        "student_enrollments_education_level_id_idx", "student_enrollments", ["education_level_id"]
    )
    op.create_index(
        "student_enrollments_program_version_id_idx", "student_enrollments", ["program_version_id"]
    )

    # --- 16. student_subjects ----------------------------------------------
    op.create_table(
        "student_subjects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="student_subjects_pkey"),
        sa.ForeignKeyConstraint(
            ["enrollment_id"], ["student_enrollments.id"],
            name="student_subjects_enrollment_id_fkey",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], name="student_subjects_subject_id_fkey"),
        sa.UniqueConstraint(
            "enrollment_id", "subject_id",
            name="uq_student_subjects_enrollment_id_subject_id_key",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'completed')",
            name="student_subjects_status_check",
        ),
    )
    # subject_id is trailing in the unique constraint above.
    op.create_index("student_subjects_subject_id_idx", "student_subjects", ["subject_id"])


def downgrade() -> None:
    # Drop in exact reverse dependency order (children before parents).
    # Dropping a table also drops its indexes and constraints.
    op.drop_table("student_subjects")
    op.drop_table("student_enrollments")
    op.drop_table("school_programs")
    op.drop_table("schools")
    op.drop_table("tvet_programs")
    op.drop_table("tvet_sectors")
    op.drop_table("program_subjects")
    op.drop_table("subjects")
    op.drop_table("program_versions")
    op.drop_table("programs")
    op.drop_table("pathway_levels")
    op.drop_table("education_levels")
    op.drop_table("pathways")
    op.drop_table("academic_years")
    op.drop_table("students")
    op.drop_table("users")
