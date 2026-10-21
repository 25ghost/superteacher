"""Administrator book intake: stage, validate, store, record (book P1).

Business rules for the ``/admin/books`` surface (ADR-001 … ADR-003):

- **Administrator-only.** The endpoint guard refuses everyone else; if
  this service is reached directly it refuses a non-admin caller with the
  shared ``LearningForbiddenError`` (403).
- **Upload staging is streamed and bounded.** The multipart file is
  copied to a local temporary file in fixed-size chunks, hashing as it
  goes, and is refused the moment the byte count passes
  ``settings.BOOK_MAX_FILE_BYTES`` — the whole book is never held in
  memory, and the client is never trusted about size, name or media type
  (see :mod:`app.services.book_validation` for the validation order).
- **Structure is proven before any row exists**: ``%PDF-`` magic, a
  ``pypdf`` parse of every page, no encryption, and at least one page of
  extractable text. Image-only/scanned documents are rejected (no OCR in
  the initial release); mixed documents pass intake because defining how
  they become learning content is extraction's (P2) problem.
- **Storage then records, in this order**: a ``file_assets`` row → the
  bytes copied through the ``StorageBackend`` under an opaque
  ``file_assets/{uuid}`` key (never a client-supplied path) → the
  ``books`` row → the ``book_revisions`` row (edition 1, ``uploaded``) →
  the canonical ``learning_contexts`` rows the book is valid for. Every
  context id must exist (unknown → 404, same as a foreign one); at least
  one is required (422 otherwise). Eligibility is exactly these
  associations — never a title or free-text resemblance.
- **No student surface, no AI, no external calls.** The response exposes
  metadata only: never a ``storage_key``, filesystem path or the file
  bytes themselves.
- **Failure leaves nothing behind.** A refusal after the storage write
  deletes the stored bytes before re-raising (compensation), so a failed
  request cannot strand an orphaned PDF.

Audit (``auth_events``): success logs ``book_uploaded``; intake refusal
logs ``book_validation_failed`` with a stable ``reason`` code — both
committed **after** any partial business state is rolled back, following
the ``login_failed`` precedent, so an audit row survives exactly when the
request failed. Nothing commits here — the API layer owns the
transaction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Sequence

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.enums import BookRevisionStatus, FileAssetStatus, UserRole
from app.models.file_asset import FileAsset
from app.repositories import auth_event_repository as auth_event_repo
from app.repositories import book_repository as book_repo
from app.repositories import file_asset_repository as file_asset_repo
from app.repositories import learning_context_repository as context_repo
from app.schemas.book import BookRead, BookRevisionRead
from app.schemas.material import FileAssetRead
from app.services import book_validation
from app.services.book_validation import BookUploadError
from app.services.learning_context_service import (
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)
from app.services.storage import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

#: How much of the upload is read from the network at a time.
_CHUNK_BYTES = 1024 * 1024

#: How many leading bytes are needed to check the PDF signature.
_MAGIC_LEN = len(b"%PDF-")


class StagedBookUpload:
    """A validated upload staged on local disk, ready to be stored.

    ``cleanup()`` is idempotent and MUST run on every path (success or
    failure) so the temporary copy never outlives the request.
    """

    def __init__(
        self,
        *,
        path: Path,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        checksum_sha256: str,
        page_count: int,
    ) -> None:
        self.path = path
        self.original_filename = original_filename
        self.content_type = content_type
        self.size_bytes = size_bytes
        self.checksum_sha256 = checksum_sha256
        self.page_count = page_count

    def cleanup(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort temp removal
            logger.warning("could not remove staged upload %s", self.path.name)


async def stage_book_upload(file: UploadFile) -> StagedBookUpload:
    """Stream one multipart upload to a temp file and prove it is a PDF.

    Envelope first (filename, ``application/pdf``), then the ``%PDF-``
    signature on the leading bytes, then the byte ceiling as the copy
    runs, then the full structural probe. Every refusal raises
    :class:`BookUploadError` with a stable ``reason`` code; the temp file
    is removed before any of them propagate.
    """
    settings = get_settings()
    content_type = book_validation.validate_upload_envelope(
        original_filename=file.filename or "",
        content_type=file.content_type or "",
    )

    handle = tempfile.NamedTemporaryFile(
        mode="wb", suffix=".pdf", prefix="st-book-", delete=False
    )
    path = Path(handle.name)
    try:
        hasher = hashlib.sha256()
        head = bytearray()
        total = 0
        with handle:
            while True:
                chunk = await file.read(_CHUNK_BYTES)
                if not chunk:
                    break
                if len(head) < _MAGIC_LEN:
                    head.extend(chunk[:_MAGIC_LEN])
                    if len(head) >= _MAGIC_LEN:
                        book_validation.validate_magic_head(bytes(head))
                total += len(chunk)
                if total > settings.BOOK_MAX_FILE_BYTES:
                    raise BookUploadError(
                        f"the uploaded PDF exceeds the "
                        f"{settings.BOOK_MAX_FILE_BYTES} byte limit",
                        reason="file_too_large",
                    )
                hasher.update(chunk)
                handle.write(chunk)
        if total == 0:
            raise BookUploadError(
                "the uploaded file is empty", reason="empty_file"
            )
        page_count = book_validation.probe_pdf(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise

    return StagedBookUpload(
        path=path,
        original_filename=(file.filename or "").strip(),
        content_type=content_type,
        size_bytes=total,
        checksum_sha256=hasher.hexdigest(),
        page_count=page_count,
    )


def create_book_upload(
    session: Session,
    user,
    *,
    staged: StagedBookUpload,
    title: str,
    description: str | None,
    learning_context_ids: Sequence[uuid.UUID],
    storage: StorageBackend | None = None,
) -> BookRead:
    """Store the staged PDF and record book + edition 1 + its contexts.

    Nothing is written until every learning context resolves, so an
    unknown id (404) can never leave a half-created book behind.
    """
    _require_admin(user)

    context_ids = _resolve_contexts(session, learning_context_ids)

    backend = storage or get_storage_backend()
    asset = file_asset_repo.create(
        session,
        uploaded_by_user_id=user.id,
        original_filename=staged.original_filename,
        content_type=staged.content_type,
        size_bytes=staged.size_bytes,
        checksum_sha256=staged.checksum_sha256,
        storage_key="pending",
        validation_status=FileAssetStatus.VALIDATING.value,
    )
    storage_key = f"file_assets/{asset.id}"
    asset.storage_key = storage_key
    session.flush()

    try:
        backend.save_file(
            storage_key, staged.path, content_type=staged.content_type
        )
    except Exception as exc:  # storage backend failure
        asset.validation_status = FileAssetStatus.INVALID.value
        asset.validation_error = "storage backend rejected the upload"
        session.flush()
        raise BookUploadError(
            "the file could not be stored; try again",
            reason="storage_failed",
        ) from exc

    asset.validation_status = FileAssetStatus.STORED.value
    session.flush()

    try:
        book = book_repo.create_book(
            session,
            title=title,
            description=description,
            created_by_user_id=user.id,
        )
        revision = book_repo.create_revision(
            session,
            book_id=book.id,
            revision_number=book_repo.next_revision_number(session, book.id),
            file_asset_id=asset.id,
            uploaded_by_user_id=user.id,
            status=BookRevisionStatus.UPLOADED.value,
        )
        for context_id in context_ids:
            book_repo.create_revision_context(
                session,
                book_revision_id=revision.id,
                learning_context_id=context_id,
            )
        _log(
            session,
            user=user,
            event_type="book_uploaded",
            payload={
                "book_id": str(book.id),
                "book_revision_id": str(revision.id),
                "file_asset_id": str(asset.id),
                "revision_number": revision.revision_number,
                "size_bytes": asset.size_bytes,
                "checksum_sha256": asset.checksum_sha256,
                "page_count": staged.page_count,
                "learning_context_ids": [str(cid) for cid in context_ids],
            },
        )
        session.flush()
    except Exception:
        # The bytes are already on disk: a refused/failed record write must
        # not strand an orphaned PDF the rollback cannot reach.
        _delete_quietly(backend, storage_key)
        raise

    logger.info(
        "book uploaded",
        extra={
            "user_id": str(user.id),
            "book_id": str(book.id),
            "book_revision_id": str(revision.id),
        },
    )
    return _read_book(book)


def list_books(
    session: Session, user, *, limit: int = 50, offset: int = 0
) -> list[BookRead]:
    """The book library, newest first (administrator only)."""
    _require_admin(user)
    return [
        _read_book(book)
        for book in book_repo.list_books(session, limit=limit, offset=offset)
    ]


def get_book(session: Session, user, book_id: uuid.UUID) -> BookRead:
    """One book — an unknown id is the same 404 as any other (L6)."""
    _require_admin(user)
    book = book_repo.get_book_by_id(session, book_id)
    if book is None:
        raise LearningNotFoundError(f"no book with id {book_id}")
    return _read_book(book)


def log_book_validation_failed(
    session: Session,
    user,
    *,
    reason: str,
    original_filename: str | None = None,
    declared_content_type: str | None = None,
) -> None:
    """Audit an intake refusal (flushed, not committed).

    The caller rolls back any partial business state and commits this row
    on its own, so a failed upload is still traceable while no book,
    asset or stored bytes survive it. Only fixed codes and the client's
    own filename/media type are recorded — never bytes or paths.
    """
    payload = {"reason": reason}
    if original_filename:
        payload["original_filename"] = original_filename[:200]
    if declared_content_type:
        payload["declared_content_type"] = declared_content_type[:100]
    _log(
        session,
        user=user,
        event_type="book_validation_failed",
        payload=payload,
    )
    session.flush()


def delete_stored_asset(
    file_asset_id: uuid.UUID, storage: StorageBackend | None = None
) -> None:
    """Best-effort removal of already-stored bytes after a failed commit."""
    backend = storage or get_storage_backend()
    _delete_quietly(backend, f"file_assets/{file_asset_id}")


# --- helpers ---------------------------------------------------------------------------------


def _delete_quietly(backend: StorageBackend, storage_key: str) -> None:
    try:
        backend.delete(storage_key)
    except Exception:  # pragma: no cover - cleanup is best-effort
        logger.warning("could not delete stored asset %s", storage_key)


def _require_admin(user) -> None:
    if user.role != UserRole.ADMIN.value:
        raise LearningForbiddenError("administrator role required for this operation")


def _resolve_contexts(
    session: Session, learning_context_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """Deduplicate the requested contexts and prove each one exists.

    An unknown id answers the same 404 as a foreign one (L6); zero valid
    contexts is a 422 — a book that is valid everywhere is valid nowhere.
    """
    ordered: list[uuid.UUID] = []
    for context_id in learning_context_ids:
        if context_id not in ordered:
            ordered.append(context_id)
    if not ordered:
        raise LearningValidationError(
            "at least one learning context is required"
        )
    for context_id in ordered:
        if context_repo.get_by_id(session, context_id) is None:
            raise LearningNotFoundError(
                f"no learning context with id {context_id}"
            )
    return ordered


def _log(
    session: Session,
    *,
    user,
    event_type: str,
    payload: dict,
) -> None:
    auth_event_repo.log_event(
        session,
        user_id=user.id,
        event_type=event_type,
        metadata_json=json.dumps(payload),
        actor_user_id=user.id,
    )


def _file_asset_read(asset: FileAsset) -> FileAssetRead:
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


def _read_book(book) -> BookRead:
    """Serialize a book with its newest revision (metadata only)."""
    revision = book.revisions[0]  # relationship is newest-first
    return BookRead(
        book_id=book.id,
        title=book.title,
        description=book.description,
        created_by_user_id=book.created_by_user_id,
        learning_context_ids=[
            link.learning_context_id for link in revision.contexts
        ],
        revision=BookRevisionRead(
            revision_id=revision.id,
            book_id=revision.book_id,
            revision_number=revision.revision_number,
            status=BookRevisionStatus(revision.status),
            uploaded_by_user_id=revision.uploaded_by_user_id,
            file_asset=_file_asset_read(revision.file_asset),
            extraction_summary=_extraction_summary_dict(
                revision.extraction_summary
            ),
            extraction_error=revision.extraction_error,
            created_at=revision.created_at,
            updated_at=revision.updated_at,
        ),
        created_at=book.created_at,
        updated_at=book.updated_at,
    )


def _extraction_summary_dict(raw: str | None) -> dict | None:
    """The stored extraction summary as a dict; NULL/legacy-bad → None.

    P2 writes it as sorted JSON via ``ExtractionResult.summary_json()``;
    a value that cannot parse is simply not summarized rather than
    breaking the book read (the ``status`` still tells the truth).
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
