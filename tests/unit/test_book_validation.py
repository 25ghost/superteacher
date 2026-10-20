"""Unit tests: administrator book PDF intake validation (book P1).

Pure functions over bytes and a temp file — no database, no HTTP. The
contract under test is the refusal order documented in
``app.services.book_validation``: envelope (filename, ``application/pdf``)
→ ``%PDF-`` magic → ``pypdf`` structure → no encryption → every page
readable and at least one page of extractable text. Fixtures are real,
structurally valid PDFs built by ``tests.conftest.build_minimal_pdf`` —
the old ``b"%PDF-1.4 fake pdf content"`` prefix trick cannot survive a
genuine parse and is exactly what intake must reject.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.services import book_validation
from app.services.book_validation import BookUploadError
from app.services.learning_context_service import LearningValidationError
from tests.conftest import build_minimal_pdf


# --- envelope (filename + declared media type) -------------------------------------


def test_a_filename_is_mandatory() -> None:
    with pytest.raises(BookUploadError) as excinfo:
        book_validation.validate_upload_envelope(
            original_filename="   ", content_type="application/pdf"
        )
    assert excinfo.value.reason == "filename_missing"
    assert excinfo.value.status_code == 422


@pytest.mark.parametrize(
    "content_type",
    ["", "image/png", "application/octet-stream", "text/plain", "application/pdfx"],
)
def test_only_the_pdf_media_type_is_allowed(content_type: str) -> None:
    with pytest.raises(BookUploadError) as excinfo:
        book_validation.validate_upload_envelope(
            original_filename="book.pdf", content_type=content_type
        )
    assert excinfo.value.reason == "content_type_not_allowed"


def test_the_media_type_is_normalized_case_and_parameters_away() -> None:
    normalized = book_validation.validate_upload_envelope(
        original_filename="book.pdf",
        content_type="Application/PDF; charset=binary",
    )
    assert normalized == "application/pdf"


# --- magic bytes --------------------------------------------------------------------


@pytest.mark.parametrize(
    "head",
    [b"%PDF-1.4", b"%PDF-1.7", b"%PDF-2.0"],
)
def test_the_pdf_signature_is_accepted(head: bytes) -> None:
    book_validation.validate_magic_head(head)  # no raise


@pytest.mark.parametrize(
    "head",
    [
        b"PK\x03\x04",  # zip/office
        b"\x89PNG",  # image
        b"hello world",  # text
        b"%PD",  # truncated signature
        b"",
    ],
)
def test_anything_else_is_refused(head: bytes) -> None:
    with pytest.raises(BookUploadError) as excinfo:
        book_validation.validate_magic_head(head)
    assert excinfo.value.reason == "magic_mismatch"


# --- structural probe ----------------------------------------------------------------


def _write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "probe.pdf"
    path.write_bytes(data)
    return path


def test_a_text_pdf_passes_and_reports_its_page_count(tmp_path: Path) -> None:
    path = _write(tmp_path, build_minimal_pdf(pages=3))
    assert book_validation.probe_pdf(path) == 3


def test_a_mixed_text_and_image_only_document_still_passes_intake(
    tmp_path: Path,
) -> None:
    """Partial scans are extraction's problem (P2), not intake's.

    No page-count, minimum-text or timing threshold is invented here: the
    document is structurally valid and at least some text is extractable,
    so intake accepts it.
    """
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(build_minimal_pdf("Chapter one"))))
    writer.append(PdfReader(io.BytesIO(build_minimal_pdf(None))))
    buffer = io.BytesIO()
    writer.write(buffer)

    path = _write(tmp_path, buffer.getvalue())
    assert book_validation.probe_pdf(path) == 2


def test_an_image_only_document_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, build_minimal_pdf(None))
    with pytest.raises(BookUploadError) as excinfo:
        book_validation.probe_pdf(path)
    assert excinfo.value.reason == "no_extractable_text"
    assert "scanned" in str(excinfo.value)


def test_an_encrypted_document_is_refused(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(build_minimal_pdf())))
    writer.encrypt("hunter2")
    buffer = io.BytesIO()
    writer.write(buffer)

    path = _write(tmp_path, buffer.getvalue())
    with pytest.raises(BookUploadError) as excinfo:
        book_validation.probe_pdf(path)
    assert excinfo.value.reason == "encrypted_pdf"


@pytest.mark.parametrize(
    "payload",
    [
        b"%PDF-1.4 truncated garbage that never closes",
        b"%PDF-1.7\nno objects at all",
        build_minimal_pdf()[:120],  # a real file cut short of its xref
    ],
)
def test_a_corrupt_or_truncated_document_is_refused(
    tmp_path: Path, payload: bytes
) -> None:
    path = _write(tmp_path, payload)
    with pytest.raises(BookUploadError) as excinfo:
        book_validation.probe_pdf(path)
    assert excinfo.value.reason == "unreadable_pdf"


# --- error contract -------------------------------------------------------------------


def test_every_refusal_is_a_422_domain_error_with_a_stable_reason() -> None:
    """BookUploadError is a LearningValidationError: HTTP mapping unchanged."""
    error = BookUploadError("nope", reason="some_code")
    assert isinstance(error, LearningValidationError)
    assert error.status_code == 422
    assert error.reason == "some_code"
    assert str(error) == "nope"
