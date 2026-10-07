"""0014 MVP material types: video + pdf_document (Phase 2 reconciliation)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-07

The MVP narrows teacher-uploaded study content to exactly two kinds:

    video          — a study video (mp4 / webm / quicktime)
    pdf_document   — a real PDF document (books, notes and worksheets are
                     PDFs with their own title and lesson placement)

so the former file-classification vocabulary ``book`` / ``note`` /
``exercise`` is retired. ``exercise`` was only ever an upload type — no
assignment domain exists — and assignments are a later product phase.

Order matters and is not cosmetic: the legacy CHECK must be dropped
*before* the rows are rewritten, because a CHECK constraint is re-evaluated
on every UPDATE — rewriting ``book`` to ``pdf_document`` while the old
``book/note/exercise`` CHECK is still attached fails with a CheckViolation
(observed against PostgreSQL: ``new row for relation "materials" violates
check constraint "materials_material_type_check"``). So the sequence is
drop → rewrite → create.

Data migration (the legacy values are legal until step 1 removes them):

    1. DROP the ``book/note/exercise`` CHECK;
    2. UPDATE materials SET material_type = 'pdf_document'
         WHERE material_type IN ('book', 'note', 'exercise');
    3. CREATE the ``video/pdf_document`` CHECK.

Existing rows are preserved: every historical material record survives as
a ``pdf_document``. No row is deleted, no table or column is dropped —
``materials`` keeps its single ``material_type`` discriminator column.
Stored files are untouched; the tightened upload allowlist (PDF + study
videos, enforced on the bytes rather than the filename) only governs new
uploads.

Downgrade restores the old vocabulary. It is necessarily lossy for the
type field (``video`` → ``note``, ``pdf_document`` → ``book``) because
the old CHECK cannot express the MVP values; rows themselves are kept.

Schema-wise this is a CHECK swap only — columns and table shape are
unchanged, and ``scripts/verify_database.py`` compares the accumulated
migration chain (with this swap applied) against the ORM metadata.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UPGRADE_CHECK = "material_type IN ('video', 'pdf_document')"
_DOWNGRADE_CHECK = "material_type IN ('book', 'note', 'exercise')"


def upgrade() -> None:
    # --- 1. retire the legacy vocabulary before any row is rewritten -----
    op.drop_constraint("materials_material_type_check", "materials", type_="check")

    # --- 2. data migration: preserve every existing material row ---------
    op.execute(
        "UPDATE materials SET material_type = 'pdf_document' "
        "WHERE material_type IN ('book', 'note', 'exercise')"
    )

    # --- 3. MVP study content is video or PDF ----------------------------
    op.create_check_constraint(
        "materials_material_type_check",
        "materials",
        _UPGRADE_CHECK,
    )


def downgrade() -> None:
    # Lossy by construction (see module docstring): the pre-MVP CHECK has
    # no equivalent for 'video' / 'pdf_document'. Same order rule as
    # upgrade(): drop first, rewrite, then re-create the legacy CHECK.
    op.drop_constraint("materials_material_type_check", "materials", type_="check")
    op.execute(
        "UPDATE materials SET material_type = "
        "CASE WHEN material_type = 'video' THEN 'note' ELSE 'book' END "
        "WHERE material_type IN ('video', 'pdf_document')"
    )
    op.create_check_constraint(
        "materials_material_type_check",
        "materials",
        _DOWNGRADE_CHECK,
    )
