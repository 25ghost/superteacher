"""Upload validation for teacher material files (MVP: video or PDF only).

The MVP content rule is enforced *here*, on the bytes: every image
format, plain text and office documents are refused even when they
claim a plausible media type, and a PDF/video row may only ever carry a
PDF/video payload (the second half of that pairing lives in
``material_service.create_material``).

Run against the pure function — no session, no HTTP.
"""
from __future__ import annotations

import pytest

from app.services import file_validation
from app.services.file_validation import ValidationError

PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
QUICKTIME = b"\x00\x00\x00\x18ftypqt  " + b"\x00" * 32
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


# --- the vocabulary --------------------------------------------------------------------


def test_the_allowlist_is_exactly_pdf_and_study_videos() -> None:
    assert file_validation.ALLOWED_CONTENT_TYPES == {
        "application/pdf",
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
    assert file_validation.VIDEO_CONTENT_TYPES == {
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
    for refused in (
        "image/png",
        "image/jpeg",
        "image/webp",
        "text/plain",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
        "",
    ):
        assert refused not in file_validation.ALLOWED_CONTENT_TYPES


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/pdf", "pdf_document"),
        ("video/mp4", "video"),
        ("video/webm", "video"),
        ("video/quicktime", "video"),
        ("image/png", None),
        ("text/plain", None),
        ("", None),
    ],
)
def test_category_for_maps_types_onto_the_material_vocabulary(
    content_type: str, expected: str | None
) -> None:
    assert file_validation.category_for(content_type) == expected


def test_normalize_strips_parameters_and_case() -> None:
    assert file_validation.normalize_content_type("Application/PDF; charset=binary") == "application/pdf"
    assert file_validation.normalize_content_type("  VIDEO/MP4 ") == "video/mp4"
    assert file_validation.normalize_content_type("") == ""


# --- acceptance -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "content_type", "filename"),
    [
        (PDF, "application/pdf", "notes.pdf"),
        (MP4, "video/mp4", "lesson.mp4"),
        (QUICKTIME, "video/quicktime", "lesson.mov"),
        (WEBM, "video/webm", "lesson.webm"),
        (PDF, "application/pdf; charset=binary", "notes.pdf"),  # parameter tolerated
    ],
)
def test_pdf_and_study_videos_pass(data: bytes, content_type: str, filename: str) -> None:
    file_validation.validate_upload(
        data=data, content_type=content_type, original_filename=filename
    )


# --- refusal ------------------------------------------------------------------------


def test_images_and_office_files_are_refused_whatever_the_claim() -> None:
    for data, content_type, filename in (
        (PNG, "image/png", "photo.png"),
        (JPEG, "image/jpeg", "scan.jpg"),
        (PNG, "application/pdf", "lying.png"),  # extension/media type lie
        (b"some text", "text/plain", "notes.txt"),
        (b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "w.docx"),
        (b"\x89PNG\r\n\x1a\n", "video/mp4", "lying.mp4"),
    ):
        with pytest.raises(ValidationError) as excinfo:
            file_validation.validate_upload(
                data=data, content_type=content_type, original_filename=filename
            )
        assert "not allowed" in str(excinfo.value) or "does not match" in str(
            excinfo.value
        )


def test_magic_bytes_must_match_the_declared_type() -> None:
    for data, content_type in (
        (b"not a pdf at all", "application/pdf"),
        (PNG, "application/pdf"),
        (PDF, "video/webm"),
        (MP4, "video/webm"),
        (b"\x00\x00\x00\x08mdat", "video/mp4"),  # no ftyp box
    ):
        with pytest.raises(ValidationError) as excinfo:
            file_validation.validate_upload(
                data=data, content_type=content_type, original_filename="file.bin"
            )
        assert "does not match the declared type" in str(excinfo.value)


def test_empty_payload_and_missing_filename_are_refused() -> None:
    with pytest.raises(ValidationError) as filename_error:
        file_validation.validate_upload(data=PDF, content_type="application/pdf", original_filename="  ")
    assert "filename" in str(filename_error.value)

    with pytest.raises(ValidationError) as empty_error:
        file_validation.validate_upload(data=b"", content_type="application/pdf", original_filename="x.pdf")
    assert "empty" in str(empty_error.value)


def test_payload_over_the_size_limit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TinySettings:
        MATERIAL_MAX_FILE_BYTES = 16

    monkeypatch.setattr(file_validation, "get_settings", lambda: _TinySettings())

    with pytest.raises(ValidationError) as excinfo:
        file_validation.validate_upload(
            data=b"%PDF-" + b"a" * 64,
            content_type="application/pdf",
            original_filename="big.pdf",
        )
    assert "material upload limit" in str(excinfo.value)
