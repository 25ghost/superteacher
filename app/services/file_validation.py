"""Upload validation pipeline for material files (Phase 2, slice 2B).

MVP rule: a teacher's study content is a study **video** or a real **PDF**
document — nothing else. Photos of papers and every image format are
refused, and the check is made on the *bytes*, never on the filename
extension alone.

No malware-scanning infrastructure is bundled — this module is the
abstraction point for one. The checks that *are* enforced:

- non-empty payload;
- size within ``settings.MATERIAL_MAX_FILE_BYTES``;
- content type in the teaching-material allowlist (PDF + study videos);
- magic-bytes sniff for every allowed type; a mismatched signature is
  refused even when the client-declared type looks plausible.

Every refusal raises :class:`ValidationError` with a human-readable
message; the service records it on the ``file_assets`` row and answers
422.
"""
from __future__ import annotations

from app.core.config import get_settings

#: Study-video container formats a teacher may upload.
VIDEO_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)

#: MIME types a teacher may attach to a material (MVP): real PDFs and
#: study videos. Images (png/jpeg/webp/heic/...), plain text and office
#: documents are deliberately NOT teaching-material types.
ALLOWED_CONTENT_TYPES: frozenset[str] = VIDEO_CONTENT_TYPES | {"application/pdf"}

#: Leading-byte signatures for the types that have a reliable magic number.
#: mp4 and quicktime both start with a box whose type is ``ftyp`` at
#: offset 4; webm/matroska is an EBML document.
_MAGIC_PREFIXES: tuple[tuple[str, bytes], ...] = (
    ("application/pdf", b"%PDF-"),
    ("video/webm", b"\x1a\x45\xdf\xa3"),
)

#: Container formats identified by ``....ftyp`` at offset 4.
_FTYP_CONTAINERS: frozenset[str] = frozenset({"video/mp4", "video/quicktime"})


class ValidationError(Exception):
    """The upload failed a validation rule (service answers 422)."""


def normalize_content_type(content_type: str) -> str:
    """Lower-cased media type without any ``;`` parameter."""
    return (content_type or "").split(";")[0].strip().lower()


def category_for(content_type: str) -> str | None:
    """Map a stored/declared content type onto its material category.

    Returns ``"pdf_document"``, ``"video"``, or ``None`` when the type is
    not an MVP teaching-material type. The service uses this to keep the
    declared ``material_type`` honest about the bytes that were uploaded.
    """
    normalized = normalize_content_type(content_type)
    if normalized == "application/pdf":
        return "pdf_document"
    if normalized in VIDEO_CONTENT_TYPES:
        return "video"
    return None


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

    normalized = normalize_content_type(content_type)
    if normalized not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            f"content type {normalized or 'unknown'!r} is not allowed for "
            "teaching materials; only PDF documents and study videos are"
        )

    head = data[:8]
    for expected_type, prefix in _MAGIC_PREFIXES:
        if normalized == expected_type and not head.startswith(prefix):
            raise ValidationError(
                f"file content does not match the declared type {normalized!r}"
            )
    if normalized in _FTYP_CONTAINERS and head[4:8] != b"ftyp":
        raise ValidationError(
            f"file content does not match the declared type {normalized!r}"
        )
