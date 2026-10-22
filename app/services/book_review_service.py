"""Administrative book review: inspect, correct, approve, reject, publish (P3).

The durable half of ADR-005's review phase. One file owns every
revision-scoped review decision so the transaction and audit rules stay
uniform:

- **Inspection** (`get_revision_detail`) exposes one revision with its
  complete unit tree — kind, title, ordered content, source-page range,
  extraction warnings, assessment designation and durable review state —
  and never a storage key or filesystem path.
- **Correction** (`correct_unit`) edits the domain fields of ONE unit
  while — and only while — the revision is ``pending_review``. Every
  invalid input (blank title, impossible page range, cross-revision
  parent, cycle, colliding sibling order, null on a non-parent field,
  empty body) is refused BEFORE anything is written, so a failed
  correction leaves content and audit state untouched (the endpoint
  rolls the whole request back). A correction always resets the unit to
  ``pending``; a STRUCTURAL correction (kind / parent / display_order)
  additionally resets the whole moved subtree, and re-asserting kind or
  parent is the explicit resolution of that unit's
  ``unresolved_structure`` warning — approval alone never erases a
  warning.
- **Per-unit approval** (`approve_unit`) records reviewer identity and
  timestamp on the unit; a chapter/topic with an unresolved assessment
  designation cannot be approved, so a published tree never hides an
  undecided designation behind an approval.
- **Whole-revision approval** (`approve_revision`) is the publication:
  it validates first (every unit approved, every chapter/topic
  designation resolved, no unresolved structural warning, a valid tree),
  then — inside the endpoint's transaction — clears the book's previous
  current-publication flag, sets this revision ``published`` +
  ``is_current_publication`` and audits it. The book row is
  ``SELECT ... FOR UPDATE``-locked first, so concurrent publications of
  the same book serialize and the partial unique index never sees two
  current revisions.
- **Rejection** (`reject_revision`) records the mandatory reason on the
  revision plus its audit event; rejected and published revisions are
  then immutable to every other operation (409).
- **Correction revisions** (`create_correction_revision`) copy a
  rejected or published revision into the NEXT edition number of the
  same book: new unit IDs, parents remapped within the copy, review
  state reset to ``pending``, contexts and source asset shared — and the
  source revision is never touched.

House rules, everywhere: administrator-only (service-level guard, not
just the route guard), the endpoint owns the commit (this module only
flushes), unknown and foreign ids answer the SAME 404 (no existence
leak), invalid lifecycle transitions are 409, invalid content 422, and
audit payloads carry identifiers, counts and fixed codes only — never
extracted book text or filesystem paths. Event names fit the 32-char
``auth_events.event_type`` column.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.book import Book
from app.models.book_revision import BookRevision
from app.models.book_revision_context import BookRevisionContext
from app.models.book_unit import BookUnit
from app.models.enums import (
    AssessmentRequirement,
    BookRevisionStatus,
    BookUnitKind,
    BookUnitReviewStatus,
    UserRole,
)
from app.models.file_asset import FileAsset
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import book_repository as book_repo
from app.schemas.book import (
    BookRevisionDetailRead,
    BookRevisionRead,
    BookRevisionRejectRequest,
    BookUnitCorrectRequest,
    BookUnitRead,
)
from app.schemas.material import FileAssetRead
from app.services.book_extraction import WARNING_UNRESOLVED_STRUCTURE
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)

logger = logging.getLogger(__name__)

#: Audit ``event_type`` values (fixed, 32-char column).
EVENT_UNIT_CORRECTED = "book_unit_corrected"
EVENT_UNIT_APPROVED = "book_unit_approved"
EVENT_REVISION_APPROVED = "book_revision_approved"
EVENT_REVISION_REJECTED = "book_revision_rejected"
EVENT_REVISION_CORRECTED = "book_revision_corrected"

#: The kinds whose assessment designation must be reviewed explicitly.
_DESIGNATION_KINDS = (BookUnitKind.CHAPTER.value, BookUnitKind.TOPIC.value)

#: Structural corrections: they move/define the tree, so they invalidate
#: the approval of the unit AND its whole subtree.
_STRUCTURAL_FIELDS = frozenset({"kind", "parent_unit_id", "display_order"})


# --- reads ----------------------------------------------------------------------------


def get_revision_detail(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> BookRevisionDetailRead:
    """One revision with its complete ordered unit tree (admin only)."""
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id)
    return _read_detail(revision, _ordered_units(session, revision))


# --- correction -----------------------------------------------------------------------


def correct_unit(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: BookUnitCorrectRequest,
) -> BookUnitRead:
    """Correct ONE unit of a ``pending_review`` revision (admin only).

    Everything is validated against the loaded tree BEFORE any column is
    written, so an invalid correction changes neither content nor audit
    state (the endpoint rolls the request back as one unit). The unit is
    reset to ``pending`` unconditionally — approval of old content never
    carries over to corrected content.
    """
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="corrected")

    units = _load_units(session, revision)
    by_id = {unit.id: unit for unit in units}
    unit = by_id.get(unit_id)
    if unit is None:
        # Unknown and foreign answer the same 404 (L6).
        raise LearningNotFoundError(f"no book unit with id {unit_id}")
    children = _children_by_parent(units)

    provided = set(payload.model_fields_set)
    if not provided:
        raise LearningValidationError("no correction fields supplied")
    for field in sorted(provided - {"parent_unit_id"}):
        if getattr(payload, field) is None:
            raise LearningValidationError(f"{field} must not be null")

    # --- resolve the EFFECTIVE parent (used by both validations below) ----------
    parent_id = (
        payload.parent_unit_id
        if "parent_unit_id" in provided
        else unit.parent_unit_id
    )
    if "parent_unit_id" in provided and payload.parent_unit_id is not None:
        _validate_parent(unit, payload.parent_unit_id, by_id)

    # --- page range (current values + the requested ones, against the PDF) ------
    page_count = _page_count(revision)
    page_start = (
        payload.source_page_start
        if "source_page_start" in provided
        else unit.source_page_start
    )
    page_end = (
        payload.source_page_end
        if "source_page_end" in provided
        else unit.source_page_end
    )
    if page_end < page_start:
        raise LearningValidationError(
            "source page range is invalid: the end page precedes the start page"
        )
    if page_count is not None and page_end > page_count:
        raise LearningValidationError(
            f"source pages must fall within the document's {page_count} pages"
        )

    # --- title ------------------------------------------------------------------
    if "title" in provided:
        title = payload.title.strip()
        if not title:
            raise LearningValidationError("the unit title must not be blank")
    else:
        title = unit.title

    # --- ordering (effective parent + effective order, vs siblings) -------------
    # Validated for EVERY correction, not only when display_order is sent:
    # a parent-only move into a spot whose sibling already holds this order
    # would otherwise reach the partial unique index as a 500 instead of a
    # 422. The unit itself is excluded (keeping its own slot is a no-op).
    order = (
        payload.display_order
        if "display_order" in provided
        else unit.display_order
    )
    for sibling in children.get(parent_id, ()):
        if sibling.id != unit.id and sibling.display_order == order:
            raise LearningValidationError(
                f"another unit already has display_order {order} "
                "under the same parent"
            )

    # --- assessment designation is a chapter/topic field ------------------------
    effective_kind = (
        payload.kind.value if "kind" in provided else unit.kind
    )
    if (
        "assessment_requirement" in provided
        and effective_kind not in _DESIGNATION_KINDS
    ):
        raise LearningValidationError(
            "the assessment designation applies to chapter/topic units only"
        )

    # --- apply ------------------------------------------------------------------
    if "title" in provided:
        unit.title = title
    if "content" in provided:
        unit.content = payload.content
    if "kind" in provided:
        unit.kind = payload.kind.value
    if "display_order" in provided:
        unit.display_order = payload.display_order
    if "parent_unit_id" in provided:
        unit.parent_unit_id = payload.parent_unit_id
    if "source_page_start" in provided:
        unit.source_page_start = payload.source_page_start
    if "source_page_end" in provided:
        unit.source_page_end = payload.source_page_end
    if "assessment_requirement" in provided:
        unit.assessment_requirement = payload.assessment_requirement.value

    # --- review-state invalidation ----------------------------------------------
    structural = bool(provided & _STRUCTURAL_FIELDS)
    _reset_review(unit)
    if structural:
        for descendant in _descendants(unit, children):
            _reset_review(descendant)
    if provided & {"kind", "parent_unit_id"}:
        _resolve_structural_warning(unit)

    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_UNIT_CORRECTED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "unit_id": str(unit_id),
            "fields": sorted(provided),
            "structural": structural,
        },
    )
    session.flush()
    logger.info(
        "book unit corrected",
        extra={
            "admin_id": str(user.id),
            "book_revision_id": str(revision_id),
            "unit_id": str(unit_id),
        },
    )
    return _read_unit(unit)


# --- per-unit approval ----------------------------------------------------------------


def approve_unit(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
) -> BookUnitRead:
    """Approve ONE unit of a ``pending_review`` revision (admin only)."""
    _require_admin(user)
    _load_book(session, book_id)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="approved")

    unit = session.scalar(
        select(BookUnit)
        .where(BookUnit.id == unit_id, BookUnit.book_revision_id == revision.id)
        .execution_options(populate_existing=True)
    )
    if unit is None:
        raise LearningNotFoundError(f"no book unit with id {unit_id}")
    if unit.review_status == BookUnitReviewStatus.APPROVED.value:
        raise LearningConflictError("the unit is already approved")
    if (
        unit.kind in _DESIGNATION_KINDS
        and unit.assessment_requirement == AssessmentRequirement.UNRESOLVED.value
    ):
        raise LearningValidationError(
            "resolve the assessment designation before approving a "
            "chapter/topic unit"
        )

    unit.review_status = BookUnitReviewStatus.APPROVED.value
    unit.reviewed_by_user_id = user.id
    unit.reviewed_at = _now()
    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_UNIT_APPROVED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "unit_id": str(unit_id),
        },
    )
    session.flush()
    logger.info(
        "book unit approved",
        extra={
            "admin_id": str(user.id),
            "book_revision_id": str(revision_id),
            "unit_id": str(unit_id),
        },
    )
    return _read_unit(unit)


# --- whole-revision approval (= publication) ------------------------------------------


def approve_revision(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> BookRevisionDetailRead:
    """Approve a fully reviewed revision and make it the CURRENT publication.

    Validation first (422 with a clear message while anything is
    incomplete), then the pointer move — previous current flag cleared
    and flushed BEFORE this revision's flag is set, so the partial
    unique index never observes two current rows even inside one
    transaction. The book row lock serializes concurrent publications of
    the same book.
    """
    _require_admin(user)
    _load_book(session, book_id, for_update=True)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="approved")

    units = _load_units(session, revision)
    if not units:
        raise LearningValidationError(
            "the revision has no extracted units to approve"
        )
    ordered = _ordered_tree(units)
    problems = _publication_problems(ordered)
    if problems:
        raise LearningValidationError("; ".join(problems))

    previous = session.scalar(
        select(BookRevision)
        .where(
            BookRevision.book_id == book_id,
            BookRevision.is_current_publication.is_(True),
            BookRevision.id != revision.id,
        )
        .execution_options(populate_existing=True)
    )
    if previous is not None:
        previous.is_current_publication = False
        session.flush()

    revision.is_current_publication = True
    revision.status = BookRevisionStatus.PUBLISHED.value
    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_REVISION_APPROVED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "revision_number": revision.revision_number,
            "unit_count": len(ordered),
            "previous_published_revision_id": (
                str(previous.id) if previous is not None else None
            ),
        },
    )
    session.flush()
    logger.info(
        "book revision approved and published",
        extra={
            "admin_id": str(user.id),
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
        },
    )
    return _read_detail(revision, ordered)


# --- rejection -------------------------------------------------------------------------


def reject_revision(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    payload: BookRevisionRejectRequest,
) -> BookRevisionDetailRead:
    """Reject a ``pending_review`` revision with a recorded reason (admin only).

    The book row lock is taken for the same reason as approval: a
    concurrent approve/reject of the same revision must serialize rather
    than interleave into an impossible state.
    """
    _require_admin(user)
    _load_book(session, book_id, for_update=True)
    revision = _load_revision(session, book_id, revision_id, for_update=True)
    _require_pending(revision, action="rejected")

    reason = payload.reason.strip()
    if not reason:
        raise LearningValidationError("a rejection reason is required")

    revision.status = BookRevisionStatus.REJECTED.value
    revision.rejection_reason = reason
    _log(
        session,
        revision=revision,
        user=user,
        event_type=EVENT_REVISION_REJECTED,
        payload={
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
            "revision_number": revision.revision_number,
            "reason": reason,
        },
    )
    session.flush()
    logger.info(
        "book revision rejected",
        extra={
            "admin_id": str(user.id),
            "book_id": str(book_id),
            "book_revision_id": str(revision_id),
        },
    )
    return _read_detail(revision, _ordered_units(session, revision))


# --- correction revisions ---------------------------------------------------------------


def create_correction_revision(
    session: Session,
    user,
    *,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> BookRevisionDetailRead:
    """Clone a rejected/published revision into the NEXT edition (admin only).

    The copy shares the source's source asset, learning contexts and
    extraction summary (same document), gets brand-new unit IDs with
    parents remapped inside the copy, and every unit starts ``pending``.
    The source revision and its units are never written — published and
    rejected history stays byte-identical.
    """
    _require_admin(user)
    book = _load_book(session, book_id, for_update=True)
    source = _load_revision(session, book_id, revision_id)
    if source.status not in (
        BookRevisionStatus.REJECTED.value,
        BookRevisionStatus.PUBLISHED.value,
    ):
        raise LearningConflictError(
            "only a rejected or published revision can be corrected "
            f"(current status: {source.status})"
        )

    source_units = _load_units(session, source)
    _ordered_tree(source_units)  # refuse a corrupt source instead of copying it

    created = BookRevision(
        book_id=book.id,
        revision_number=book_repo.next_revision_number(session, book.id),
        file_asset_id=source.file_asset_id,
        uploaded_by_user_id=user.id,
        status=BookRevisionStatus.PENDING_REVIEW.value,
        extraction_summary=source.extraction_summary,
        extraction_error=None,
        is_current_publication=False,
        rejection_reason=None,
    )
    session.add(created)
    session.flush()
    for link in source.contexts:
        session.add(
            BookRevisionContext(
                book_revision_id=created.id,
                learning_context_id=link.learning_context_id,
            )
        )

    children = _children_by_parent(source_units)
    copied = 0

    def _copy(unit: BookUnit, parent_new_id: uuid.UUID | None) -> None:
        nonlocal copied
        row = BookUnit(
            id=uuid.uuid4(),
            book_revision_id=created.id,
            parent_unit_id=parent_new_id,
            kind=unit.kind,
            title=unit.title,
            content=unit.content,
            display_order=unit.display_order,
            source_page_start=unit.source_page_start,
            source_page_end=unit.source_page_end,
            extraction_warnings=unit.extraction_warnings,
            assessment_requirement=unit.assessment_requirement,
            review_status=BookUnitReviewStatus.PENDING.value,
            reviewed_by_user_id=None,
            reviewed_at=None,
        )
        session.add(row)
        copied += 1
        for child in children.get(unit.id, ()):
            _copy(child, row.id)

    for root in children.get(None, ()):
        _copy(root, None)
    session.flush()

    _log(
        session,
        revision=created,
        user=user,
        event_type=EVENT_REVISION_CORRECTED,
        payload={
            "book_id": str(book_id),
            "source_revision_id": str(revision_id),
            "book_revision_id": str(created.id),
            "revision_number": created.revision_number,
            "unit_count": copied,
        },
    )
    session.flush()
    logger.info(
        "book correction revision created",
        extra={
            "admin_id": str(user.id),
            "book_id": str(book_id),
            "source_revision_id": str(revision_id),
            "book_revision_id": str(created.id),
        },
    )
    return _read_detail(created, _ordered_units(session, created))


# --- helpers ------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_admin(user) -> None:
    """Service-level guard (the route guard is not the only guard)."""
    if user.role != UserRole.ADMIN.value:
        raise LearningForbiddenError(
            "administrator role required for this operation"
        )


def _require_pending(revision: BookRevision, *, action: str) -> None:
    if revision.status != BookRevisionStatus.PENDING_REVIEW.value:
        raise LearningConflictError(
            f"only pending_review revisions can be {action} "
            f"(current status: {revision.status})"
        )


def _load_book(session: Session, book_id: uuid.UUID, *, for_update: bool = False) -> Book:
    stmt = select(Book).where(Book.id == book_id)
    if for_update:
        # Serializes every revision-level decision of this book (approve,
        # reject, correction-revision numbering) inside the transaction.
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    book = session.scalar(stmt)
    if book is None:
        raise LearningNotFoundError(f"no book with id {book_id}")
    return book


def _load_revision(
    session: Session,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> BookRevision:
    stmt = select(BookRevision).where(
        BookRevision.id == revision_id, BookRevision.book_id == book_id
    )
    if for_update:
        # A locking read must not join other tables (PostgreSQL refuses
        # FOR UPDATE on the nullable side of the joinedload's outer join);
        # ``file_asset`` is then lazy-loaded inside the same transaction.
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    else:
        stmt = stmt.options(joinedload(BookRevision.file_asset))
    revision = session.scalar(stmt)
    if revision is None:
        # A revision id from ANOTHER book answers exactly the same 404 as
        # an unknown id: the caller learns nothing about foreign revisions.
        raise LearningNotFoundError(f"no book revision with id {revision_id}")
    return revision


def _load_units(session: Session, revision: BookRevision) -> list[BookUnit]:
    return list(
        session.scalars(
            select(BookUnit)
            .where(BookUnit.book_revision_id == revision.id)
            .execution_options(populate_existing=True)
        )
    )


def _children_by_parent(
    units: list[BookUnit],
) -> dict[uuid.UUID | None, list[BookUnit]]:
    """Sibling units grouped by parent, each group in display order."""
    grouped: dict[uuid.UUID | None, list[BookUnit]] = {}
    for unit in units:
        grouped.setdefault(unit.parent_unit_id, []).append(unit)
    for siblings in grouped.values():
        siblings.sort(key=lambda unit: (unit.display_order, unit.id))
    return grouped


def _ordered_units(session: Session, revision: BookRevision) -> list[BookUnit]:
    return _ordered_tree(_load_units(session, revision))


def _ordered_tree(units: list[BookUnit]) -> list[BookUnit]:
    """Depth-first, parents before children; refuse anything not a tree.

    A cycle or an unreachable row means the hierarchy cannot be
    interpreted reliably — publication and inspection both refuse it
    rather than returning a half-ordering.
    """
    grouped = _children_by_parent(units)
    ordered: list[BookUnit] = []
    visited: set[uuid.UUID] = set()

    def _visit(unit: BookUnit) -> None:
        if unit.id in visited:
            raise LearningValidationError(
                "the unit hierarchy is not a valid tree"
            )
        visited.add(unit.id)
        ordered.append(unit)
        for child in grouped.get(unit.id, ()):
            _visit(child)

    for root in grouped.get(None, ()):
        _visit(root)
    if len(visited) != len(units):
        raise LearningValidationError(
            "the unit hierarchy is not a valid tree"
        )
    return ordered


def _descendants(
    unit: BookUnit, children: dict[uuid.UUID | None, list[BookUnit]]
) -> list[BookUnit]:
    """Every unit strictly below ``unit`` (BFS, no revisits)."""
    found: list[BookUnit] = []
    queue = list(children.get(unit.id, ()))
    seen = {unit.id}
    while queue:
        current = queue.pop(0)
        if current.id in seen:
            continue
        seen.add(current.id)
        found.append(current)
        queue.extend(children.get(current.id, ()))
    return found


def _validate_parent(
    unit: BookUnit, parent_id: uuid.UUID, by_id: dict[uuid.UUID, BookUnit]
) -> None:
    """Cross-revision parents and cycles are refused BEFORE any write.

    The composite self-FK would reject a cross-revision parent at commit
    time (a 500); validating here turns it into the 422 it should be.
    """
    parent = by_id.get(parent_id)
    if parent is None:
        raise LearningValidationError(
            f"parent unit {parent_id} does not belong to this revision"
        )
    if parent.id == unit.id:
        raise LearningValidationError("a unit cannot be its own parent")
    cursor = parent
    while cursor.parent_unit_id is not None:
        if cursor.parent_unit_id == unit.id:
            raise LearningValidationError(
                "reparenting would create a cycle"
            )
        next_cursor = by_id.get(cursor.parent_unit_id)
        if next_cursor is None:  # pragma: no cover - FK guarantees existence
            raise LearningValidationError(
                "the unit hierarchy is not a valid tree"
            )
        cursor = next_cursor


def _publication_problems(units: list[BookUnit]) -> list[str]:
    """Everything that must be fixed before a revision may be published."""
    problems: list[str] = []
    unapproved = [
        unit
        for unit in units
        if unit.review_status != BookUnitReviewStatus.APPROVED.value
    ]
    if unapproved:
        problems.append(
            f"{len(unapproved)} of {len(units)} units are still unapproved; "
            "approve every unit first"
        )
    undecided = [
        unit
        for unit in units
        if unit.kind in _DESIGNATION_KINDS
        and unit.assessment_requirement == AssessmentRequirement.UNRESOLVED.value
    ]
    if undecided:
        problems.append(
            f"{len(undecided)} chapter/topic units still have an unresolved "
            "assessment designation"
        )
    warning_units = [
        unit
        for unit in units
        if WARNING_UNRESOLVED_STRUCTURE in _warning_list(unit.extraction_warnings)
    ]
    if warning_units:
        problems.append(
            f"{len(warning_units)} units carry an unresolved structural "
            "warning; correct their structure first"
        )
    return problems


def _warning_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [code for code in raw.split("\n") if code]


def _resolve_structural_warning(unit: BookUnit) -> None:
    """A structural assertion is the explicit resolution of the warning.

    ``unresolved_structure`` means the parser could not place this
    content; the administrator re-asserting kind/parent IS that
    resolution. Approval alone never clears it (the codes stay visible
    on approved units).
    """
    codes = [
        code
        for code in _warning_list(unit.extraction_warnings)
        if code != WARNING_UNRESOLVED_STRUCTURE
    ]
    unit.extraction_warnings = "\n".join(codes) or None


def _reset_review(unit: BookUnit) -> None:
    unit.review_status = BookUnitReviewStatus.PENDING.value
    unit.reviewed_by_user_id = None
    unit.reviewed_at = None


def _page_count(revision: BookRevision) -> int | None:
    """The source document's page count (unknown only for legacy rows)."""
    if not revision.extraction_summary:
        return None
    try:
        parsed = json.loads(revision.extraction_summary)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("page_count")
    return value if isinstance(value, int) and value > 0 else None


def _log(
    session: Session,
    *,
    revision: BookRevision,
    user,
    event_type: str,
    payload: dict,
) -> None:
    """One audit row about identifiers and fixed codes — never content."""
    auth_event_repo.log_event(
        session,
        user_id=revision.uploaded_by_user_id,
        event_type=event_type,
        metadata_json=json.dumps(payload),
        actor_user_id=user.id,
    )


def _asset_read(asset: FileAsset) -> FileAssetRead:
    return FileAssetRead(
        file_asset_id=asset.id,
        original_filename=asset.original_filename,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        checksum_sha256=asset.checksum_sha256,
        validation_status=asset.validation_status,
        validation_error=asset.validation_error,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _read_unit(unit: BookUnit) -> BookUnitRead:
    return BookUnitRead(
        unit_id=unit.id,
        parent_unit_id=unit.parent_unit_id,
        kind=BookUnitKind(unit.kind),
        title=unit.title,
        content=unit.content,
        display_order=unit.display_order,
        source_page_start=unit.source_page_start,
        source_page_end=unit.source_page_end,
        extraction_warnings=_warning_list(unit.extraction_warnings),
        assessment_requirement=AssessmentRequirement(unit.assessment_requirement),
        review_status=BookUnitReviewStatus(unit.review_status),
        reviewed_by_user_id=unit.reviewed_by_user_id,
        reviewed_at=unit.reviewed_at,
        created_at=unit.created_at,
        updated_at=unit.updated_at,
    )


def _read_detail(
    revision: BookRevision, ordered_units: list[BookUnit]
) -> BookRevisionDetailRead:
    return BookRevisionDetailRead(
        revision_id=revision.id,
        book_id=revision.book_id,
        revision_number=revision.revision_number,
        status=BookRevisionStatus(revision.status),
        uploaded_by_user_id=revision.uploaded_by_user_id,
        file_asset=_asset_read(revision.file_asset),
        extraction_summary=_summary_dict(revision.extraction_summary),
        extraction_error=revision.extraction_error,
        is_current_publication=revision.is_current_publication,
        rejection_reason=revision.rejection_reason,
        units=[_read_unit(unit) for unit in ordered_units],
        created_at=revision.created_at,
        updated_at=revision.updated_at,
    )


def _summary_dict(raw: str | None) -> dict | None:
    """The stored extraction summary as a dict; NULL/legacy-bad → None."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
