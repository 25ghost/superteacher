"""PDF intake validation for administrator book uploads (book P1).

An administrator book is a real, structurally valid, text-extractable PDF
— nothing else. The teacher-material pipeline's prefix check is
deliberately NOT enough here (ADR-003): a filename, a declared MIME type
or a forged ``%PDF-`` header never makes a malformed document acceptable.
The checks, in order:

- a non-empty filename and an ``application/pdf`` content type;
- the byte ceiling ``settings.BOOK_MAX_FILE_BYTES`` (enforced by the
  streaming stage in :mod:`app.services.book_service`, never by loading
  the whole file into memory);
- the ``%PDF-`` leading signature;
- :mod:`pypdf` structural parse of the document (a corrupt or truncated
  file fails here);
- rejection of encrypted documents (``reader.is_encrypted``), whatever
  password policy the source used — decryption is out of scope;
- a page-by-page text probe: every page must be readable, and at least
  one page must yield extractable text. Image-only (scanned) documents
  are rejected in the initial release; OCR is excluded (ADR-003).

Partially scanned or mixed PDFs (some pages with text, some without)
PASS intake — they are structurally valid and extractable — but no
page-count, minimum-text, or processing-time threshold is invented here:
defining how mixed documents become learning content is extraction's
(P2) job, not intake policy.

There is zero AI processing and zero external transmission in this
module: validation is a local parse of bytes the server already holds.
Every refusal raises :class:`BookUploadError` carrying a stable
``reason`` code that the API layer records in the audit trail — codes
and fixed messages only, never bytes, passwords or filesystem paths.
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from app.services.learning_context_service import LearningValidationError

#: The only media type an administrator book may carry.
BOOK_CONTENT_TYPE = "application/pdf"

#: Leading-byte signature of a PDF document.
_PDF_MAGIC_PREFIX = b"%PDF-"


class BookUploadError(LearningValidationError):
    """A book upload failed intake validation (service answers 422).

    Subclasses ``LearningValidationError`` so every existing
    ``LearningError`` mapping applies unchanged; ``reason`` is a stable,
    audit-safe classification code (``filename_missing``,
    ``encrypted_pdf``, ...).
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def validate_upload_envelope(*, original_filename: str, content_type: str) -> str:
    """Filename + PDF-only content type. Returns the normalized type."""
    if not original_filename or not original_filename.strip():
        raise BookUploadError(
            "the uploaded file must have a filename", reason="filename_missing"
        )
    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized != BOOK_CONTENT_TYPE:
        raise BookUploadError(
            f"content type {normalized or 'unknown'!r} is not allowed for "
            "books; only PDF documents are",
            reason="content_type_not_allowed",
        )
    return normalized


def validate_magic_head(head: bytes) -> None:
    """Refuse a payload whose leading bytes are not a PDF signature."""
    if not head.startswith(_PDF_MAGIC_PREFIX):
        raise BookUploadError(
            "file content does not match the declared type 'application/pdf'",
            reason="magic_mismatch",
        )


def probe_pdf(path: Path) -> int:
    """Structurally validate the staged PDF; return its page count.

    Raises :class:`BookUploadError` for corrupt, encrypted, unreadable or
    text-less documents. Every page's text extraction is attempted so a
    document that falls apart mid-file is refused, not accepted on the
    strength of its first page.
    """
    try:
        with path.open("rb") as handle:
            reader = PdfReader(handle, strict=False)
            if reader.is_encrypted:
                raise BookUploadError(
                    "encrypted PDF documents are not accepted",
                    reason="encrypted_pdf",
                )
            page_count = len(reader.pages)
            text_found = False
            for page in reader.pages:
                extracted = page.extract_text() or ""
                if extracted.strip():
                    text_found = True
    except BookUploadError:
        raise
    except Exception as exc:  # pypdf parse/structure failure of any kind
        raise BookUploadError(
            "the uploaded file is not a readable PDF document",
            reason="unreadable_pdf",
        ) from exc
    if not text_found:
        raise BookUploadError(
            "the PDF has no extractable text (scanned or image-only "
            "documents are not accepted)",
            reason="no_extractable_text",
        )
    return page_count
