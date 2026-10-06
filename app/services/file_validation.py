"""Upload validation pipeline for material files (Phase 2, slice 2B).

No malware-scanning infrastructure is bundled — this module is the
abstraction point for one. The checks that *are* enforced:

- non-empty payload;
- size within ``settings.MATERIAL_MAX_FILE_BYTES``;
- content type in the teaching-material allowlist;
- magic-bytes sniff for the types that have a reliable signature
  (PDF, PNG, JPEG, DOCX); a mismatched signature is refused even when
  the client-declared type looks plausible.

Every refusal raises :class:`ValidationError` with a human-readable
message; the service records it on the ``file_assets`` row and answers
422.
"""
from __future__ import annotations

from app.core.config import get_settings

#: MIME types a teacher may attach to a material (slice 2B).
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "image/png",
        "image/jpeg",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)

#: Leading-byte signatures for types that have a reliable magic number.
_MAGIC_PREFIXES: tuple[tuple[str, bytes], ...] = (
    ("application/pdf", b"%PDF-"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"PK\x03\x04",
    ),
)


class ValidationError(Exception):
    """The upload failed a validation rule (service answers 422)."""


def validate_upload(
    *,
    data: bytes,
    content_type: str,
    original_filename: str,
) -> None:
    """Run the full pipeline; raise :class:`ValidationError` on any refusal."""
    if not original_filename or not original_filename.strip():
        raise ValidationError("the uploaded file must have a filename")
    if not data:
        raise ValidationError("the uploaded file is empty")

    settings = get_settings()
    if len(data) > settings.MATERIAL_MAX_FILE_BYTES:
        max_mb = settings.MATERIAL_MAX_FILE_BYTES / (1024 * 1024)
        raise ValidationError(
            f"file exceeds the {max_mb:g} MB material upload limit"
        )

    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            f"content type {normalized or 'unknown'!r} is not allowed for "
            "teaching materials"
        )

    head = data[:8]
    for expected_type, prefix in _MAGIC_PREFIXES:
        if normalized == expected_type and not head.startswith(prefix):
            raise ValidationError(
                f"file content does not match the declared type {normalized!r}"
            )
