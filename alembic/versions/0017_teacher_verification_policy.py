"""0017 teacher verification policy (application workflow + approved scope)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-13

Implements the durable data model of the teacher verification policy:

1. ``teachers.verification_category`` — the APPROVED category
   (formal_teacher | independent_tutor), nullable and defaulting every
   existing row to NULL: legacy teachers without a completed application
   fail closed. Existing ``verification_status`` values are preserved.
2. ``teacher_applications`` — one row per verification review cycle with
   an explicit lifecycle (draft / submitted / needs_information /
   approved / rejected / withdrawn); one non-terminal application per
   teacher via a partial unique index. No application is auto-created and
   nothing is auto-approved: existing teachers are NOT backfilled.
3. ``teacher_application_contexts`` — the canonical learning contexts the
   applicant explicitly requests, each receiving an explicit approved /
   rejected decision (rejected requires a reason — DB CHECK).
4. ``teacher_evidence`` + ``teacher_evidence_contexts`` — submitted
   identity/credential evidence (private ``file_assets`` reference,
   structured issuer data, duplicate-review indicator, soft delete) and
   its explicit relevance links to requested contexts.
5. ``teacher_verification_checks`` — recorded reviewer verification
   decisions (method, outcome, source institution, monotonic ``seq`` per
   evidence so the latest outcome is deterministic).
6. ``teacher_assessments`` — recorded subject-specific assessment
   outcomes for the independent-tutor experience route (scope, date,
   assessor, outcome; no scoring is ever computed here).
7. ``teacher_approved_contexts`` — the authoritative approved teaching
   scope, written only by the approval workflow.

No data is fabricated: every new column is nullable or defaults to the
least-privilege state, and no existing row gains teaching authority.
Downgrade drops the seven tables and the category column; historical
offerings, materials, classes and audit rows are untouched.

Migration impact on legacy data (fail closed, by design): an old
``approved`` verification_status no longer suffices for teaching
authorization — the category and approved scope are missing until the
teacher completes an application.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. approved category on the teacher profile -------------------------
    op.add_column(
        "teachers",
        sa.Column("verification_category", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "teachers_verification_category_check",
        "teachers",
        "verification_category IS NULL OR verification_category IN "
        "('formal_teacher', 'independent_tutor')",
    )

    # --- 2. applications -----------------------------------------------------
    op.create_table(
        "teacher_applications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("claimed_school_id", sa.Uuid(), nullable=True),
        sa.Column("misrepresentation_declaration", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("statement", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("previous_application_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teacher_applications_pkey"),
        sa.ForeignKeyConstraint(["teacher_id"], ["teachers.id"], name="teacher_applications_teacher_id_fkey"),
        sa.ForeignKeyConstraint(["claimed_school_id"], ["schools.id"], name="teacher_applications_claimed_school_id_fkey"),
        sa.ForeignKeyConstraint(["previous_application_id"], ["teacher_applications.id"], name="teacher_applications_previous_application_id_fkey"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="teacher_applications_created_by_fkey"),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'needs_information', 'approved', 'rejected', 'withdrawn')",
            name="teacher_applications_status_check",
        ),
        sa.CheckConstraint(
            "category IS NULL OR category IN ('formal_teacher', 'independent_tutor')",
            name="teacher_applications_category_check",
        ),
    )
    op.create_index(
        "uq_teacher_applications_active_key",
        "teacher_applications",
        ["teacher_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('draft', 'submitted', 'needs_information')"),
        sqlite_where=sa.text("status IN ('draft', 'submitted', 'needs_information')"),
    )
    op.create_index(
        "teacher_applications_teacher_id_idx", "teacher_applications", ["teacher_id"]
    )
    op.create_index(
        "teacher_applications_status_idx", "teacher_applications", ["status"]
    )

    # --- 3. requested contexts ----------------------------------------------
    op.create_table(
        "teacher_application_contexts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("learning_context_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teacher_application_contexts_pkey"),
        sa.ForeignKeyConstraint(["application_id"], ["teacher_applications.id"], name="teacher_application_contexts_application_id_fkey"),
        sa.ForeignKeyConstraint(["learning_context_id"], ["learning_contexts.id"], name="teacher_application_contexts_learning_context_id_fkey"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], name="teacher_application_contexts_decided_by_fkey"),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'rejected')",
            name="teacher_application_contexts_status_check",
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR decision_reason IS NOT NULL",
            name="teacher_application_contexts_reason_check",
        ),
    )
    op.create_index(
        "uq_teacher_application_contexts_key",
        "teacher_application_contexts",
        ["application_id", "learning_context_id"],
        unique=True,
    )
    op.create_index(
        "teacher_application_contexts_context_id_idx",
        "teacher_application_contexts",
        ["learning_context_id"],
    )

    # --- 4. evidence ---------------------------------------------------------
    op.create_table(
        "teacher_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("file_asset_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("issuing_organization", sa.String(length=200), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("credential_reference", sa.String(length=200), nullable=True),
        sa.Column("duplicate_indicator", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teacher_evidence_pkey"),
        sa.ForeignKeyConstraint(["application_id"], ["teacher_applications.id"], name="teacher_evidence_application_id_fkey"),
        sa.ForeignKeyConstraint(["file_asset_id"], ["file_assets.id"], name="teacher_evidence_file_asset_id_fkey"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="teacher_evidence_created_by_fkey"),
        sa.CheckConstraint(
            "evidence_type IN ('identity_document', 'academic_qualification', 'teacher_training', 'professional_registration', 'employment_confirmation', 'subject_qualification', 'vocational_qualification', 'professional_experience')",
            name="teacher_evidence_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'removed')",
            name="teacher_evidence_status_check",
        ),
    )
    op.create_index("teacher_evidence_application_id_idx", "teacher_evidence", ["application_id"])
    op.create_index("teacher_evidence_file_asset_id_idx", "teacher_evidence", ["file_asset_id"])

    op.create_table(
        "teacher_evidence_contexts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("learning_context_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teacher_evidence_contexts_pkey"),
        sa.ForeignKeyConstraint(["evidence_id"], ["teacher_evidence.id"], name="teacher_evidence_contexts_evidence_id_fkey"),
        sa.ForeignKeyConstraint(["learning_context_id"], ["learning_contexts.id"], name="teacher_evidence_contexts_learning_context_id_fkey"),
    )
    op.create_index(
        "uq_teacher_evidence_contexts_key",
        "teacher_evidence_contexts",
        ["evidence_id", "learning_context_id"],
        unique=True,
    )
    op.create_index(
        "teacher_evidence_contexts_context_id_idx",
        "teacher_evidence_contexts",
        ["learning_context_id"],
    )

    # --- 5. verification checks ----------------------------------------------
    op.create_table(
        "teacher_verification_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("source_institution", sa.String(length=200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teacher_verification_checks_pkey"),
        sa.ForeignKeyConstraint(["application_id"], ["teacher_applications.id"], name="teacher_verification_checks_application_id_fkey"),
        sa.ForeignKeyConstraint(["evidence_id"], ["teacher_evidence.id"], name="teacher_verification_checks_evidence_id_fkey"),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["users.id"], name="teacher_verification_checks_reviewer_user_id_fkey"),
        sa.CheckConstraint(
            "method IN ('registry_lookup', 'issuer_confirmation', 'manual_contact', 'other')",
            name="teacher_verification_checks_method_check",
        ),
        sa.CheckConstraint(
            "outcome IN ('verified', 'failed', 'inconclusive')",
            name="teacher_verification_checks_outcome_check",
        ),
    )
    op.create_index(
        "uq_teacher_verification_checks_evidence_seq_key",
        "teacher_verification_checks",
        ["evidence_id", "seq"],
        unique=True,
    )
    op.create_index(
        "teacher_verification_checks_application_id_idx",
        "teacher_verification_checks",
        ["application_id"],
    )

    # --- 6. subject assessments ----------------------------------------------
    op.create_table(
        "teacher_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_date", sa.Date(), nullable=False),
        sa.Column("assessor", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teacher_assessments_pkey"),
        sa.ForeignKeyConstraint(["application_id"], ["teacher_applications.id"], name="teacher_assessments_application_id_fkey"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], name="teacher_assessments_subject_id_fkey"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], name="teacher_assessments_recorded_by_fkey"),
        sa.CheckConstraint(
            "outcome IN ('passed', 'failed')",
            name="teacher_assessments_outcome_check",
        ),
    )
    op.create_index("teacher_assessments_application_id_idx", "teacher_assessments", ["application_id"])
    op.create_index("teacher_assessments_subject_id_idx", "teacher_assessments", ["subject_id"])

    # --- 7. approved scope ----------------------------------------------------
    op.create_table(
        "teacher_approved_contexts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("learning_context_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="teacher_approved_contexts_pkey"),
        sa.ForeignKeyConstraint(["teacher_id"], ["teachers.id"], name="teacher_approved_contexts_teacher_id_fkey"),
        sa.ForeignKeyConstraint(["learning_context_id"], ["learning_contexts.id"], name="teacher_approved_contexts_learning_context_id_fkey"),
        sa.ForeignKeyConstraint(["application_id"], ["teacher_applications.id"], name="teacher_approved_contexts_application_id_fkey"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], name="teacher_approved_contexts_approved_by_fkey"),
        sa.UniqueConstraint(
            "teacher_id",
            "learning_context_id",
            name="uq_teacher_approved_contexts_teacher_context_key",
        ),
    )
    op.create_index(
        "teacher_approved_contexts_context_id_idx",
        "teacher_approved_contexts",
        ["learning_context_id"],
    )
    op.create_index(
        "teacher_approved_contexts_application_id_idx",
        "teacher_approved_contexts",
        ["application_id"],
    )


def downgrade() -> None:
    op.drop_table("teacher_approved_contexts")
    op.drop_table("teacher_assessments")
    op.drop_table("teacher_verification_checks")
    op.drop_table("teacher_evidence_contexts")
    op.drop_table("teacher_evidence")
    op.drop_table("teacher_application_contexts")
    op.drop_table("teacher_applications")
    op.drop_constraint(
        "teachers_verification_category_check", "teachers", type_="check"
    )
    op.drop_column("teachers", "verification_category")
