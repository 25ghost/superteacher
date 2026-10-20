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
from app.schemas.book import BookRead
from app.services import book_service
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
