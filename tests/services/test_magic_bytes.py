# SPDX-License-Identifier: Apache-2.0
"""Unit tests for _validate_magic_bytes (Item A — P9b S-007).

Verifies that documents_service._validate_magic_bytes raises
MimeTypeMismatchError when the file signature does not match the
declared content type, and passes silently when it does.

Closes P8 DEFERRED §17 S-007.
"""
from __future__ import annotations

import pytest

from lmchat.services.documents_service import (
    MimeTypeMismatchError,
    _validate_magic_bytes,
)

# ---------------------------------------------------------------------------
# application/pdf
# ---------------------------------------------------------------------------


def test_pdf_valid_signature_passes() -> None:
    """Valid PDF magic bytes (%PDF-) pass without raising."""
    _validate_magic_bytes("application/pdf", b"%PDF-1.4 ...\n")


def test_pdf_invalid_signature_raises() -> None:
    """A non-PDF byte stream declared as application/pdf raises MimeTypeMismatchError."""
    with pytest.raises(MimeTypeMismatchError) as exc_info:
        _validate_magic_bytes("application/pdf", b"MZ\x90\x00\x03")  # EXE magic
    assert exc_info.value.content_type == "application/pdf"


def test_pdf_empty_bytes_raises() -> None:
    """Empty bytes declared as application/pdf raises MimeTypeMismatchError."""
    with pytest.raises(MimeTypeMismatchError):
        _validate_magic_bytes("application/pdf", b"")


def test_pdf_with_content_type_params_passes() -> None:
    """Content-Type with charset parameter strips correctly."""
    _validate_magic_bytes("application/pdf; charset=binary", b"%PDF-2.0\n")


def test_pdf_exe_renamed_raises() -> None:
    """An .exe renamed to .pdf is caught by magic-byte check."""
    # PE header starts with b'MZ'
    with pytest.raises(MimeTypeMismatchError):
        _validate_magic_bytes("application/pdf", b"MZ" + b"\x00" * 100)


# ---------------------------------------------------------------------------
# text/html
# ---------------------------------------------------------------------------


def test_html_valid_tag_passes() -> None:
    """Valid HTML starting with < passes."""
    _validate_magic_bytes("text/html", b"<!DOCTYPE html><html>")


def test_html_with_leading_whitespace_passes() -> None:
    """HTML with leading whitespace before < passes (stripped check)."""
    _validate_magic_bytes("text/html", b"   \n<!DOCTYPE html>")


def test_html_non_tag_bytes_raises() -> None:
    """Binary data declared as text/html raises MimeTypeMismatchError."""
    with pytest.raises(MimeTypeMismatchError) as exc_info:
        _validate_magic_bytes("text/html", b"\x89PNG\r\n")  # PNG magic
    assert exc_info.value.content_type == "text/html"


def test_html_empty_bytes_passes() -> None:
    """Empty bytes declared as text/html pass (no bytes to validate)."""
    # Empty body is allowed; content length checks are separate.
    _validate_magic_bytes("text/html", b"")


# ---------------------------------------------------------------------------
# text/plain and text/markdown — no magic byte check
# ---------------------------------------------------------------------------


def test_text_plain_arbitrary_bytes_passes() -> None:
    """text/plain has no magic byte; arbitrary bytes pass validation."""
    _validate_magic_bytes("text/plain", b"Hello, world!")


def test_text_plain_binary_bytes_passes() -> None:
    """text/plain skips magic byte check entirely; binary bytes pass."""
    _validate_magic_bytes("text/plain", b"\x00\x01\x02\x03")


def test_text_markdown_passes() -> None:
    """text/markdown skips magic byte check."""
    _validate_magic_bytes("text/markdown", b"# Heading\n\nContent.")


# ---------------------------------------------------------------------------
# upload_document integration — MimeTypeMismatchError bubbles to caller
# ---------------------------------------------------------------------------


def test_validate_magic_bytes_raises_mimetypemismatch_with_detail() -> None:
    """MimeTypeMismatchError carries both content_type and expected_sig."""
    exc = MimeTypeMismatchError("application/pdf", "PDF magic bytes (%PDF-)")
    assert exc.content_type == "application/pdf"
    assert "PDF" in exc.expected_sig
    assert "application/pdf" in str(exc)


# ---------------------------------------------------------------------------
# B-002 (a) HTML with UTF-8 BOM prefix
# ---------------------------------------------------------------------------


def test_html_with_utf8_bom_passes() -> None:
    """HTML with a UTF-8 BOM prefix (\\xef\\xbb\\xbf) passes validation.

    lstrip() strips ASCII whitespace but NOT the 3-byte UTF-8 BOM.
    The fix strips the BOM explicitly before the ``<`` check.
    Regression guard for B-001.
    """
    bom = b"\xef\xbb\xbf"
    _validate_magic_bytes("text/html", bom + b"<!DOCTYPE html><html>")


# ---------------------------------------------------------------------------
# B-002 (b) Truncated PDF body
# ---------------------------------------------------------------------------


def test_pdf_truncated_body_raises() -> None:
    """A truncated PDF body (fewer than 5 bytes) raises MimeTypeMismatchError.

    ``%PDF-`` is the 5-byte PDF magic signature. ``%PD`` (3 bytes) is not a
    complete match and must be rejected.
    """
    with pytest.raises(MimeTypeMismatchError) as exc_info:
        _validate_magic_bytes("application/pdf", b"%PD")
    assert exc_info.value.content_type == "application/pdf"



# ---------------------------------------------------------------------------
# application/epub+zip and DOCX — both ZIP containers, same magic bytes
# ---------------------------------------------------------------------------

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_epub_valid_zip_signature_passes() -> None:
    """Valid ZIP local-file-header magic bytes (PK\\x03\\x04) pass for EPUB."""
    _validate_magic_bytes("application/epub+zip", b"PK\x03\x04" + b"\x00" * 20)


def test_epub_invalid_signature_raises() -> None:
    """Non-ZIP bytes declared as EPUB raise MimeTypeMismatchError."""
    with pytest.raises(MimeTypeMismatchError) as exc_info:
        _validate_magic_bytes("application/epub+zip", b"Not a zip file at all")
    assert exc_info.value.content_type == "application/epub+zip"


def test_epub_empty_bytes_raises() -> None:
    """Empty bytes declared as EPUB raise MimeTypeMismatchError."""
    with pytest.raises(MimeTypeMismatchError):
        _validate_magic_bytes("application/epub+zip", b"")


def test_docx_valid_zip_signature_passes() -> None:
    """Valid ZIP local-file-header magic bytes (PK\\x03\\x04) pass for DOCX."""
    _validate_magic_bytes(_DOCX_MIME, b"PK\x03\x04" + b"\x00" * 20)


def test_docx_invalid_signature_raises() -> None:
    """Non-ZIP bytes (a PDF) declared as DOCX raise MimeTypeMismatchError."""
    with pytest.raises(MimeTypeMismatchError) as exc_info:
        _validate_magic_bytes(_DOCX_MIME, b"%PDF-1.4\n")
    assert exc_info.value.content_type == _DOCX_MIME


def test_docx_empty_bytes_raises() -> None:
    """Empty bytes declared as DOCX raise MimeTypeMismatchError."""
    with pytest.raises(MimeTypeMismatchError):
        _validate_magic_bytes(_DOCX_MIME, b"")
