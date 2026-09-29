"""0005 role vocabulary — collapse the account roles to exactly three

Revision ID: 0005
Revises: 0004

The backend now allows exactly three roles on ``users.role``:

    student | teacher | admin

``0001`` created ``users_role_check`` with the five-value legacy
vocabulary (``student, teacher, parent, school_admin, rahura_admin``).
That migration is already applied everywhere, so the change lands here
as an explicit data normalization followed by a constraint swap:

1. Legacy administrators are merged into the single ``admin`` role
   (``school_admin`` and ``rahura_admin`` both become ``admin``).
2. Any legacy ``parent`` row — there are none in the provisioned
   databases — becomes ``student`` (least privilege) and is suspended so
   it cannot act until an administrator reviews the account.
3. ``users_role_check`` is dropped and recreated with the three allowed
   values.

Normalization runs BEFORE the constraint swap so the migration applies
cleanly to databases that still contain legacy rows. The ORM model
derives the same CHECK from ``app.models.enums.UserRole``, so model and
database can no longer drift.

Downgrade restores the five-value vocabulary (``admin`` →
``rahura_admin``). The ``parent`` → ``student`` mapping is lossy and
deliberately not reversed; status stays as written.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None

# The exact CHECK texts (literal SQL, like migration 0001) so offline
# verification can compare them with the ORM-derived constraint.
_UPGRADE_CHECK = "role IN ('student', 'teacher', 'admin')"
_DOWNGRADE_CHECK = (
    "role IN ('student', 'teacher', 'parent', 'school_admin', 'rahura_admin')"
)


def upgrade() -> None:
    # --- 1. data normalization (before the constraint swap) -------------------
    # Both legacy administrator flavours collapse into one ``admin`` role.
    op.execute(
        "UPDATE users SET role = 'admin' "
        "WHERE role IN ('school_admin', 'rahura_admin')"
    )
    # Legacy parents are not part of the three-role system: demote to the
    # least-privileged role and suspend the account pending review. The
    # ``users_status_check`` vocabulary already allows 'suspended'.
    op.execute(
        "UPDATE users SET role = 'student', status = 'suspended' "
        "WHERE role = 'parent'"
    )

    # --- 2. constraint swap ---------------------------------------------------
    op.drop_constraint("users_role_check", "users", type_="check")
    op.create_check_constraint(
        "users_role_check",
        "users",
        _UPGRADE_CHECK,
    )


def downgrade() -> None:
    # admin → rahura_admin (the platform-operator flavour of the legacy pair).
    op.execute("UPDATE users SET role = 'rahura_admin' WHERE role = 'admin'")
    op.drop_constraint("users_role_check", "users", type_="check")
    op.create_check_constraint(
        "users_role_check",
        "users",
        _DOWNGRADE_CHECK,
    )
    # The parent → student demotion above is intentionally not reversed.
