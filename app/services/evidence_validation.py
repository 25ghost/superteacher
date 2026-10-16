"""Upload validation pipeline for teacher-verification evidence files.

Verification evidence is a scanned or photographed *document* attached to
a teacher-verification application (identity document, academic
qualification, vocational qualification, professional registration,
employment confirmation, professional-experience reference,
misrepresentation declaration). Deliberately separate from the teaching-
material pipeline (app.services.file_validation): evidence allows images
(a photo of a card or certificate is a normal submission) but still
refuses everything that is not PDF/JPEG/PNG.

The check is made on the *bytes*, never on the filename extension alone:

- non-empty payload;
- size within ``settings.EVIDENCE_MAX_FILE_BYTES``;
- content type in the evidence allowlist (PDF + JPEG + PNG);
- magic-bytes sniff for every allowed type; a mismatched signature is
  refused even when the client-declared type looks plausible.

Every refusal raises :class:`file_validation.ValidationError` (same error
type the material pipeline raises, so services and API error mapping stay
uniform); the service answers 422 with the message.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.services.file_validation import ValidationError, normalize_content_type

#: MIME types an applicant may attach as verification evidence (MVP):
#: real PDFs plus JPEG/PNG photographs of documents.
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
    }
)

#: Leading-byte signatures for the evidence allowlist.
_MAGIC_PREFIXES: tuple[tuple[str, bytes], ...] = (
    ("application/pdf", b"%PDF-"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
)


def validate_evidence_upload(
    *,
    data: bytes,
    content_type: str,
    original_filename: str,
) -> str:
    """Run the full evidence pipeline; raise ValidationError on any refusal.

    Returns the normalized (lower-cased, parameter-stripped) content type
    that was actually validated, so the caller stores an honest value.
    """
    if not original_filename or not original_filename.strip():
        raise ValidationError("the uploaded file must have a filename")
    if not data:
        raise ValidationError("the uploaded file is empty")

    settings = get_settings()
    if len(data) > settings.EVIDENCE_MAX_FILE_BYTES:
        max_mb = settings.EVIDENCE_MAX_FILE_BYTES / (1024 * 1024)
        raise ValidationError(
            f"file exceeds the {max_mb:g} MB evidence upload limit"
        )

    normalized = normalize_content_type(content_type)
    if normalized not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            f"content type {normalized or 'unknown'!r} is not allowed for "
            "verification evidence; only PDF, JPEG and PNG documents are"
        )

    head = data[:8]
    for expected_type, prefix in _MAGIC_PREFIXES:
        if normalized == expected_type and not head.startswith(prefix):
            raise ValidationError(
                f"file content does not match the declared type {normalized!r}"
            )
    return normalized
