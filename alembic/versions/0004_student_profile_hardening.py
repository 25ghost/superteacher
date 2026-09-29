"""0004 student profile hardening — validation backstops + audit vocabulary

Revision ID: 0004
Revises: 0003

Hardening (backend-only, mirrors the schema/service layer):

1. Phone canonicalisation — every stored phone is normalized to
   E.164-with-``+`` so format variants (``2507…`` vs ``+2507…``) can no
   longer mint duplicate identities.
2. Gender normalization + CHECK — lowercase existing values, clear any
   value outside the vocabulary (schema-level vocab had no DB backstop),
   then add ``students_gender_check``.
3. Date-of-birth range — replaces the future-only check with a range
   (``<= CURRENT_DATE`` AND ``>= 1900-01-01``); implausibly old junk rows
   are nulled first (the column is nullable) so the constraint validates.
4. Audit vocabulary — ``student_profile_history`` gains CHECKs on
   ``change_type`` (create/update) and ``field_name`` (profile/full_name/
   gender/country), mirroring the repository constants.
5. Case-insensitive email uniqueness — functional unique index on
   ``lower(email)`` so differently-cased duplicates are refused even when
   written outside the API.

Every data normalization runs BEFORE its constraint is created so the
migration applies cleanly to databases containing legacy rows.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_GENDER_VOCAB = ("female", "male", "other", "undisclosed")


def upgrade() -> None:
    # --- 1. canonical phones (E.164 with leading +) --------------------------
    op.execute(
        "UPDATE users SET phone = '+' || phone "
        "WHERE phone IS NOT NULL AND phone NOT LIKE '+%'"
    )

    # --- 2. gender normalization + vocabulary CHECK --------------------------
    op.execute("UPDATE students SET gender = lower(gender) WHERE gender IS NOT NULL")
    _gender_list = ", ".join(f"'{g}'" for g in _GENDER_VOCAB)
    op.execute(
        "UPDATE students SET gender = NULL "
        f"WHERE gender IS NOT NULL AND gender NOT IN ({_gender_list})"
    )
    op.create_check_constraint(
        "students_gender_check",
        "students",
        f"gender IS NULL OR gender IN ({_gender_list})",
    )

    # --- 3. date-of-birth range check (replaces future-only check) -----------
    # ISO string literals: PostgreSQL casts them to date implicitly, and the
    # same expression compiles on the SQLite unit-test databases.
    op.execute(
        "UPDATE students SET date_of_birth = NULL "
        "WHERE date_of_birth IS NOT NULL AND date_of_birth < '1900-01-01'"
    )
    op.drop_constraint(
        "students_date_of_birth_check", "students", type_="check"
    )
    op.create_check_constraint(
        "students_date_of_birth_range_check",
        "students",
        "date_of_birth IS NULL OR "
        "(date_of_birth <= CURRENT_DATE AND date_of_birth >= '1900-01-01')",
    )

    # --- 4. audit vocabulary CHECKs ------------------------------------------
    op.create_check_constraint(
        "student_profile_history_change_type_check",
        "student_profile_history",
        "change_type IN ('create', 'update')",
    )
    op.create_check_constraint(
        "student_profile_history_field_name_check",
        "student_profile_history",
        "field_name IN ('profile', 'full_name', 'gender', 'country')",
    )

    # --- 5. case-insensitive email uniqueness --------------------------------
    # Postgres/SQLite treat NULLs as distinct in unique indexes, so the
    # many phone-or-bare accounts (email IS NULL) remain legal.
    op.create_index(
        "uq_users_email_ci_key",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_users_email_ci_key", table_name="users")
    op.drop_constraint(
        "student_profile_history_field_name_check",
        "student_profile_history",
        type_="check",
    )
    op.drop_constraint(
        "student_profile_history_change_type_check",
        "student_profile_history",
        type_="check",
    )
    op.drop_constraint(
        "students_date_of_birth_range_check", "students", type_="check"
    )
    op.create_check_constraint(
        "students_date_of_birth_check",
        "students",
        "date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE",
    )
    op.drop_constraint("students_gender_check", "students", type_="check")
    # Phone/gender normalizations are intentionally not reversed (lossy one-
    # way data cleanup); the application layer re-canonicalises on write.
