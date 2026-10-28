"""Administrative book intake API — ``/admin/books`` (role-split).

Every route requires an administrator (``require_admin``); no handler
here serves a student or teacher token — cross-role calls are refused
with 403 before any logic runs, and anonymous callers get 401.

- ``POST /admin/books`` — upload one PDF (multipart: ``title``, optional
  ``description``, one-or-more ``learning_context_id`` fields, one
  ``file``) and create the book with its ``uploaded`` first edition. The
  upload is streamed to a bounded temp file, structurally validated
  through ``pypdf`` (real PDF, unencrypted, extractable text), written
  through the storage backend and recorded with a ``book_uploaded`` audit
  event — or, on refusal, with ``book_validation_failed`` and no rows
  left behind.
- ``GET  /admin/books`` — the book library, newest first (bounded
  ``limit``/``offset``).
- ``GET  /admin/books/{book_id}`` — one book with its latest revision
  and canonical contexts; unknown id → 404.

Book review & publication (P3) — every route here is administrator-only
and operates on ONE revision of ONE book:

- ``GET    .../revisions/{revision_id}`` — full inspection: the ordered
  unit tree with review state, extraction warnings and designation.
- ``PATCH  .../revisions/{revision_id}/units/{unit_id}`` — correct one
  unit while the revision is ``pending_review``.
- ``POST   .../revisions/{revision_id}/units/{unit_id}/approve`` —
  approve a single unit.
- ``POST   .../revisions/{revision_id}/approve`` — approve the fully
  reviewed revision: it becomes the current publication.
- ``POST   .../revisions/{revision_id}/reject`` — reject with a recorded
  reason.
- ``POST   .../revisions/{revision_id}/correct`` — clone a rejected or
  published revision into the next edition for correction.

Objective assessment authoring (P4) — administrator-only, one revision:

- ``GET    .../revisions/{revision_id}/units/{unit_id}/questions`` —
  list the questions of one exercise-set unit.
- ``POST   .../revisions/{revision_id}/units/{unit_id}/questions`` —
  author one question (options inline, unverified).
- ``PATCH  .../revisions/{revision_id}/questions/{question_id}`` —
  correct one question (supplying ``options`` replaces the set).
- ``DELETE .../revisions/{revision_id}/questions/{question_id}`` —
  delete one question.
- ``POST   .../revisions/{revision_id}/questions/{question_id}/verify``
  — name THE correct option (answer key).
- ``PATCH  .../revisions/{revision_id}/units/{unit_id}/threshold`` —
  set one exercise set's passing threshold percent.

The response carries metadata only: never a ``storage_key``, filesystem
path or file bytes, and no student or teacher surface exists for books in
this phase. Error contract: invalid upload/inputs → 422 (shared
``LearningError`` family, mapped unchanged), unknown context or book id →
404, wrong role → 403.
"""
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_ADMIN
from app.core.auth_dependencies import require_admin
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.book import (
    BookRead,
    BookRevisionDetailRead,
    BookRevisionRejectRequest,
    BookUnitCorrectRequest,
    BookUnitRead,
)
from app.schemas.book_assessment import (
    BookQuestionCorrectRequest,
    BookQuestionCreate,
    BookQuestionRead,
    BookQuestionVerifyRequest,
    BookUnitThresholdRequest,
)
from app.services import (
    book_extraction_service,
    book_question_service,
    book_review_service,
    book_service,
)
from app.services.learning_context_service import LearningError

_settings = get_settings()

router = APIRouter(prefix="/admin", tags=[TAG_ADMIN])


def _http_error(exc: LearningError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post(
    "/books",
    response_model=BookRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a PDF and create a book (administrator only)",
    description=(
        "multipart/form-data: ``title`` (1-200 chars), optional "
        "``description`` (≤2000 chars), one or more repeated "
        "``learning_context_id`` fields (the canonical contexts the book "
        "is valid for — at least one, every id must exist), and one "
        "``file``. The file must be a real, unencrypted, text-extractable "
        "PDF within the book size limit; scanned/image-only documents and "
        "any other payload are refused (422) and leave no book, asset or "
        "stored bytes behind. The book is created with its first edition "
        "in ``uploaded`` state — no student can see it in this phase. "
        "Unknown context id → 404. Administrators only."
    ),
    responses={
        201: {"description": "Book created with its first revision"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown learning context id"},
        422: {"description": "Invalid upload or metadata"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
async def admin_create_book(
    request: Request,
    title: str = Form(..., min_length=1, max_length=200),
    description: str | None = Form(default=None, max_length=2000),
    learning_context_id: list[uuid.UUID] = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookRead:
    staged: book_service.StagedBookUpload | None = None
    try:
        # 1. Stream + prove the file BEFORE any row exists: a refusal here
        #    audits the attempt and rolls back nothing (there is nothing).
        try:
            staged = await book_service.stage_book_upload(file)
        except LearningError as exc:
            session.rollback()
            book_service.log_book_validation_failed(
                session,
                admin,
                reason=getattr(exc, "reason", "invalid_upload"),
                original_filename=file.filename,
                declared_content_type=file.content_type,
            )
            session.commit()
            raise _http_error(exc) from exc

        # 2. Persist: asset → storage → book → revision → contexts.
        try:
            created = book_service.create_book_upload(
                session,
                admin,
                staged=staged,
                title=title,
                description=description,
                learning_context_ids=learning_context_id,
            )
            asset_id = created.revision.file_asset.file_asset_id
        except LearningError as exc:
            session.rollback()
            raise _http_error(exc) from exc
        except Exception:
            session.rollback()
            raise

        # 3. Commit owns the transaction; a failed commit must not strand
        #    the already-stored bytes the rollback cannot reach.
        try:
            session.commit()
        except Exception:
            session.rollback()
            book_service.delete_stored_asset(asset_id)
            raise
        return created
    finally:
        if staged is not None:
            staged.cleanup()


@router.get(
    "/books",
    response_model=list[BookRead],
    summary="List the administrator book library",
    description=(
        "Newest first, bounded by ``limit`` (1-100, default 50) and "
        "``offset``. Metadata only — no file bytes or storage paths. "
        "Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def admin_list_books(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[BookRead]:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return book_service.list_books(session, admin, limit=limit, offset=offset)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/books/{book_id}",
    response_model=BookRead,
    summary="Retrieve one book with its latest revision",
    description=(
        "The book's metadata, its latest revision (status, uploaded file "
        "asset, uploader) and the canonical learning contexts it is valid "
        "for. Unknown id → 404. Administrators only."
    ),
    responses={
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def admin_read_book(
    request: Request,
    book_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookRead:
    try:
        return book_service.get_book(session, admin, book_id)
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/books/{book_id}/retry-extraction",
    response_model=BookRead,
    summary="Retry a failed book extraction (administrator only)",
    description=(
        "Requeues the book's latest revision when — and only when — its "
        "content extraction previously FAILED: the revision returns to "
        "``uploaded`` and the background extraction sweep re-parses the "
        "already-stored PDF, so the response shows the requeued state "
        "while the sweeper moves it to ``pending_review`` (or back to "
        "``extraction_failed`` with the fixed error code) asynchronously. "
        "The retry never re-uploads bytes. Any other revision status → "
        "409 (nothing to retry), unknown id → 404. Administrators only."
    ),
    responses={
        200: {"description": "Extraction requeued; latest revision shown"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book id"},
        409: {"description": "Latest extraction is not in a failed state"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_retry_book_extraction(
    request: Request,
    book_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookRead:
    # Write path: service flushes, the endpoint owns the commit.
    try:
        result = book_extraction_service.retry_book_extraction(
            session, admin, book_id=book_id
        )
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return result


# --- book review & publication (P3) -------------------------------------------------


def _review_write(service_call, session: Session):
    """Run one review decision, then commit it as one transaction.

    A domain refusal (404/409/422) rolls the WHOLE request back — a
    failed correction or approval must leave content, review state and
    audit events untouched — and only a clean service result reaches the
    commit, which is the single owner of the transaction boundary.
    """
    try:
        result = service_call()
    except LearningError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return result


@router.get(
    "/books/{book_id}/revisions/{revision_id}",
    response_model=BookRevisionDetailRead,
    summary="Inspect one book revision (administrator only)",
    description=(
        "Full review surface for ONE revision: extraction summary and "
        "outcome, publication/rejection state, and the complete unit tree "
        "in reading order — kind, title, ordered content, source-page "
        "range, extraction warnings, assessment designation and durable "
        "review state (reviewer + timestamp). Never a storage key or "
        "filesystem path. Unknown id, or a revision of another book, → "
        "404. Administrators only."
    ),
    responses={
        200: {"description": "Revision with its ordered unit tree"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book or revision id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def admin_inspect_book_revision(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookRevisionDetailRead:
    # Read path: translate domain errors only; no commit/rollback.
    try:
        return book_review_service.get_revision_detail(
            session, admin, book_id=book_id, revision_id=revision_id
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.patch(
    "/books/{book_id}/revisions/{revision_id}/units/{unit_id}",
    response_model=BookUnitRead,
    summary="Correct one unit of a pending review (administrator only)",
    description=(
        "Edits the domain fields of ONE unit while — and only while — the "
        "revision is ``pending_review``. Only supplied fields are applied "
        "(``model_fields_set``); ``parent_unit_id`` may be explicit null "
        "to move a unit to the root, any other explicit null is refused "
        "(422). Blank titles, impossible page ranges, cross-revision "
        "parents, cycles, colliding sibling orders and empty bodies are "
        "all refused (422) BEFORE anything is written, so a refused "
        "correction changes nothing. Every correction resets the unit to "
        "``pending``; a structural correction (kind/parent/order) also "
        "resets the moved subtree. Wrong revision status → 409, unknown "
        "ids → 404. Administrators only."
    ),
    responses={
        200: {"description": "Corrected unit, reset to pending"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book, revision or unit id"},
        409: {"description": "Revision is no longer pending review"},
        422: {"description": "Invalid correction"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_correct_book_unit(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: BookUnitCorrectRequest,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookUnitRead:
    return _review_write(
        lambda: book_review_service.correct_unit(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            unit_id=unit_id,
            payload=payload,
        ),
        session,
    )


@router.post(
    "/books/{book_id}/revisions/{revision_id}/units/{unit_id}/approve",
    response_model=BookUnitRead,
    summary="Approve one unit of a pending review (administrator only)",
    description=(
        "Records reviewer identity and timestamp on the unit. A "
        "chapter/topic unit whose assessment designation is still "
        "``unresolved`` cannot be approved (422), so a published tree "
        "never hides an undecided designation behind an approval. Already "
        "approved → 409, wrong revision status → 409, unknown ids → 404. "
        "Administrators only."
    ),
    responses={
        200: {"description": "Approved unit"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book, revision or unit id"},
        409: {"description": "Unit already approved or revision not pending"},
        422: {"description": "Unresolved assessment designation"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_approve_book_unit(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookUnitRead:
    return _review_write(
        lambda: book_review_service.approve_unit(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            unit_id=unit_id,
        ),
        session,
    )


@router.post(
    "/books/{book_id}/revisions/{revision_id}/approve",
    response_model=BookRevisionDetailRead,
    summary="Approve a reviewed revision as the current publication",
    description=(
        "Validates first: every unit approved, no chapter/topic with an "
        "unresolved assessment designation, no unresolved structural "
        "warning, a valid unit tree (anything incomplete → 422 with the "
        "reason). Then, atomically inside one transaction, the book's "
        "previous current-publication flag is cleared and THIS revision "
        "becomes ``published`` + current — concurrent publications of the "
        "same book serialize on the book row lock. Wrong revision status "
        "(not ``pending_review``) → 409, unknown ids → 404. "
        "Administrators only."
    ),
    responses={
        200: {"description": "Revision approved and published"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book or revision id"},
        409: {"description": "Revision is no longer pending review"},
        422: {"description": "Review is incomplete"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_approve_book_revision(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookRevisionDetailRead:
    return _review_write(
        lambda: book_review_service.approve_revision(
            session, admin, book_id=book_id, revision_id=revision_id
        ),
        session,
    )


@router.post(
    "/books/{book_id}/revisions/{revision_id}/reject",
    response_model=BookRevisionDetailRead,
    summary="Reject a pending revision with a recorded reason",
    description=(
        "Marks the ``pending_review`` revision ``rejected`` and stores the "
        "mandatory non-blank reason (422 if blank) for the next reviewer. "
        "Rejected and published revisions then refuse every other review "
        "operation (409). Wrong status → 409, unknown ids → 404. "
        "Administrators only."
    ),
    responses={
        200: {"description": "Rejected revision"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book or revision id"},
        409: {"description": "Revision is no longer pending review"},
        422: {"description": "Blank rejection reason"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_reject_book_revision(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    payload: BookRevisionRejectRequest,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookRevisionDetailRead:
    return _review_write(
        lambda: book_review_service.reject_revision(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            payload=payload,
        ),
        session,
    )


@router.post(
    "/books/{book_id}/revisions/{revision_id}/correct",
    response_model=BookRevisionDetailRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a correction revision from a rejected/published one",
    description=(
        "Clones the source revision into the NEXT edition of the same "
        "book: new unit IDs with parents remapped inside the copy, every "
        "unit reset to ``pending``, shared source asset, learning contexts "
        "and extraction summary — the source revision is never modified, "
        "so published/rejected history stays intact. Only rejected or "
        "published sources may be corrected (otherwise 409); unknown ids "
        "→ 404. The new edition starts ``pending_review``. Administrators "
        "only."
    ),
    responses={
        201: {"description": "Correction revision created"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book or revision id"},
        409: {"description": "Source revision is not rejected/published"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_correct_book_revision(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookRevisionDetailRead:
    return _review_write(
        lambda: book_review_service.create_correction_revision(
            session, admin, book_id=book_id, revision_id=revision_id
        ),
        session,
    )


# --- objective assessment authoring (P4) -----------------------------------------------


@router.get(
    "/books/{book_id}/revisions/{revision_id}/units/{unit_id}/questions",
    response_model=list[BookQuestionRead],
    summary="List the questions of one exercise-set unit",
    description=(
        "Every question of ONE unit in sibling order, with its options "
        "and the answer-key verification state (verifier + timestamp). "
        "Administrators only. Unknown ids → 404."
    ),
    responses={
        200: {"description": "Ordered questions with their options"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book, revision or unit id"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_READ)
def admin_list_book_unit_questions(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[BookQuestionRead]:
    try:
        return book_question_service.list_questions(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            unit_id=unit_id,
        )
    except LearningError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/books/{book_id}/revisions/{revision_id}/units/{unit_id}/questions",
    response_model=BookQuestionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Author one question on one exercise-set unit",
    description=(
        "Creates ONE question on ONE ``exercise_set`` unit of a "
        "``pending_review`` revision. ``question_type`` is "
        "``multiple_choice`` (2-10 options) or ``true_false`` (exactly 2); "
        "options are supplied inline with distinct texts and sibling "
        "orders; ``prompt`` and ``explanation`` must be non-blank. "
        "Correctness is NOT part of creation — the question stays "
        "unverified until an explicit ``verify`` action names the correct "
        "option. Creating resets the unit to ``pending``. Duplicate "
        "``display_order`` in the same set → 422; wrong revision status "
        "→ 409; unknown ids → 404; a non-exercise-set unit → 422. "
        "Administrators only."
    ),
    responses={
        201: {"description": "Question created (unverified)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book, revision or unit id"},
        409: {"description": "Revision is no longer pending review"},
        422: {"description": "Invalid question or options"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_create_book_unit_question(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: BookQuestionCreate,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookQuestionRead:
    return _review_write(
        lambda: book_question_service.create_question(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            unit_id=unit_id,
            payload=payload,
        ),
        session,
    )


@router.patch(
    "/books/{book_id}/revisions/{revision_id}/questions/{question_id}",
    response_model=BookQuestionRead,
    summary="Correct one question of a pending review",
    description=(
        "Only supplied fields are applied (``model_fields_set``); an "
        "empty body is a 422. Supplying ``options`` REPLACES the whole "
        "option set — fresh rows. ANY correction clears the question's "
        "answer key (verification state and option flags) and resets "
        "the owning unit to ``pending``; everything is validated "
        "before any row is written, so a refused correction changes "
        "nothing. Wrong revision status → 409, unknown ids → 404. "
        "Administrators only."
    ),
    responses={
        200: {"description": "Corrected question (verification cleared)"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book, revision or question id"},
        409: {"description": "Revision is no longer pending review"},
        422: {"description": "Invalid correction"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_correct_book_question(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: BookQuestionCorrectRequest,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookQuestionRead:
    return _review_write(
        lambda: book_question_service.correct_question(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            question_id=question_id,
            payload=payload,
        ),
        session,
    )


@router.delete(
    "/books/{book_id}/revisions/{revision_id}/questions/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one question of a pending review",
    description=(
        "Removes ONE question and its options. Published and rejected "
        "revisions refuse every mutation (409), so a graded question "
        "and the attempts that reference it are never orphaned by a "
        "late delete. Wrong revision status → 409, unknown ids → 404. "
        "Administrators only."
    ),
    responses={
        204: {"description": "Question deleted"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book, revision or question id"},
        409: {"description": "Revision is no longer pending review"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_delete_book_question(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    question_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    _review_write(
        lambda: book_question_service.delete_question(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            question_id=question_id,
        ),
        session,
    )


@router.post(
    "/books/{book_id}/revisions/{revision_id}/questions/{question_id}/verify",
    response_model=BookQuestionRead,
    summary="Name THE correct option of one question (answer key)",
    description=(
        "Explicitly confirm THE correct option of ONE question. The "
        "named option must belong to this question (else the SAME 404 "
        "as an unknown id); every other option of the question is "
        "cleared, so exactly one correct option exists. Verification is "
        "recorded with the administrator identity and timestamp — "
        "correctness is never inferred. Wrong revision status → 409, "
        "unknown ids → 404. Administrators only."
    ),
    responses={
        200: {"description": "Verified question"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book, revision or question id"},
        409: {"description": "Revision is no longer pending review"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_verify_book_question(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: BookQuestionVerifyRequest,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookQuestionRead:
    return _review_write(
        lambda: book_question_service.verify_question(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            question_id=question_id,
            payload=payload,
        ),
        session,
    )


@router.patch(
    "/books/{book_id}/revisions/{revision_id}/units/{unit_id}/threshold",
    response_model=BookUnitRead,
    summary="Set one exercise set's passing threshold percent",
    description=(
        "Configure the per-exercise-set passing threshold (1-100, "
        "integer percent). ``null`` restores the default 70. Any "
        "effective change resets the unit's review status to "
        "``pending``; setting the stored value again is an idempotent "
        "no-op. Only ``pending_review`` revisions and ``exercise_set`` "
        "units can be configured (409 / 422). Administrators only."
    ),
    responses={
        200: {"description": "Updated exercise-set unit"},
        401: {"description": "Missing/invalid credentials"},
        403: {"description": "Authenticated but not an administrator"},
        404: {"description": "Unknown book, revision or unit id"},
        409: {"description": "Revision is no longer pending review"},
        422: {"description": "Invalid threshold or unit kind"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(_settings.RATE_LIMIT_STUDENT_WRITE)
def admin_set_book_unit_threshold(
    request: Request,
    book_id: uuid.UUID,
    revision_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: BookUnitThresholdRequest,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> BookUnitRead:
    return _review_write(
        lambda: book_question_service.set_unit_threshold(
            session,
            admin,
            book_id=book_id,
            revision_id=revision_id,
            unit_id=unit_id,
            payload=payload,
        ),
        session,
    )
