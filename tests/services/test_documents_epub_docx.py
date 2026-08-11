# SPDX-License-Identifier: Apache-2.0
"""Unit tests for EPUB and DOCX text extraction (documents_service._extract_text).

Covers:
- Valid EPUB: chapters extracted in OPF spine order, tags stripped via the
  same BeautifulSoup path used for text/html.
- Valid EPUB fallback: container.xml/OPF missing or unparsable falls back
  to scanning *.xhtml/*.html/*.htm entries sorted by name.
- Valid DOCX: paragraph text extracted from word/document.xml, including
  <w:tab/> -> "\\t" and <w:br/>/<w:cr/> -> "\\n" run mapping.
- Malformed ZIP containers (declared EPUB/DOCX) raise DocumentParseError.
- Structurally valid EPUB/DOCX containers with no extractable text raise
  DocumentParseError.

Magic-byte validation for these two MIME types lives in test_magic_bytes.py;
route-level 422 mapping for DocumentParseError lives in
tests/routes/test_documents.py.
"""
from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from lmchat.services.documents_service import DocumentParseError, _extract_text

_EPUB_MIME = "application/epub+zip"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """Build an in-memory ZIP archive from a name -> bytes mapping."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# EPUB — valid extraction, spine order
# ---------------------------------------------------------------------------


def _minimal_epub_entries(*, chapter_bodies: list[str]) -> dict[str, bytes]:
    """Build a minimal-but-valid EPUB: mimetype + container.xml + OPF + chapters."""
    container_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    manifest_items = "\n".join(
        f'    <item id="chap{i}" href="chap{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(chapter_bodies))
    )
    spine_items = "\n".join(
        f'    <itemref idref="chap{i}"/>' for i in range(len(chapter_bodies))
    )
    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
        'unique-identifier="BookId">\n'
        "  <metadata></metadata>\n"
        "  <manifest>\n"
        f"{manifest_items}\n"
        "  </manifest>\n"
        "  <spine>\n"
        f"{spine_items}\n"
        "  </spine>\n"
        "</package>\n"
    ).encode()

    entries: dict[str, bytes] = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": container_xml,
        "OEBPS/content.opf": opf,
    }
    for i, chapter_body in enumerate(chapter_bodies):
        entries[f"OEBPS/chap{i}.xhtml"] = (
            f"<html><body>{chapter_body}</body></html>"
        ).encode()
    return entries


def test_epub_extracts_chapters_in_spine_order() -> None:
    """Chapter text is extracted in spine order, with tags stripped."""
    entries = _minimal_epub_entries(
        chapter_bodies=[
            "<h1>Chapter One</h1><p>First chapter body text.</p>",
            "<h1>Chapter Two</h1><p>Second chapter body text.</p>",
        ]
    )
    body_bytes = _make_zip(entries)

    text = _extract_text(body_bytes, _EPUB_MIME)

    assert "First chapter body text." in text
    assert "Second chapter body text." in text
    assert text.index("First chapter body text.") < text.index(
        "Second chapter body text."
    )
    # Tags are stripped, same as the text/html path.
    assert "<h1>" not in text
    assert "<p>" not in text


def test_epub_fallback_extracts_unordered_html_entries() -> None:
    """When container.xml is absent, the fallback scans *.xhtml/.html/.htm entries."""
    entries = {
        "chapA.xhtml": b"<html><body><p>Fallback chapter text.</p></body></html>",
    }
    body_bytes = _make_zip(entries)

    text = _extract_text(body_bytes, _EPUB_MIME)
    assert "Fallback chapter text." in text


def test_epub_no_html_chapters_raises_parse_error() -> None:
    """A valid EPUB container whose spine has no chapters yields no text."""
    entries = _minimal_epub_entries(chapter_bodies=[])
    body_bytes = _make_zip(entries)

    with pytest.raises(DocumentParseError):
        _extract_text(body_bytes, _EPUB_MIME)


def test_epub_malformed_zip_raises_parse_error() -> None:
    """A body that isn't a real ZIP (despite the leading ZIP signature) raises."""
    with pytest.raises(DocumentParseError):
        _extract_text(b"PK\x03\x04garbage", _EPUB_MIME)


# ---------------------------------------------------------------------------
# DOCX — valid extraction
# ---------------------------------------------------------------------------


def _docx_entries(document_xml: bytes) -> dict[str, bytes]:
    return {"word/document.xml": document_xml}


def test_docx_extracts_paragraph_text_in_order() -> None:
    """Paragraph text is extracted from word/document.xml, in document order."""
    document_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<w:document xmlns:w="{_DOCX_NS}">\n'
        "  <w:body>\n"
        "    <w:p><w:r><w:t>First paragraph.</w:t></w:r></w:p>\n"
        "    <w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p>\n"
        "  </w:body>\n"
        "</w:document>\n"
    ).encode()
    body_bytes = _make_zip(_docx_entries(document_xml))

    text = _extract_text(body_bytes, _DOCX_MIME)

    assert "First paragraph." in text
    assert "Second paragraph." in text
    assert text.index("First paragraph.") < text.index("Second paragraph.")


def test_docx_tab_and_break_run_mapping() -> None:
    """<w:tab/> becomes a literal tab; <w:br/> becomes a newline within a paragraph."""
    document_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<w:document xmlns:w="{_DOCX_NS}">\n'
        "  <w:body>\n"
        "    <w:p><w:r><w:t>Col1</w:t></w:r><w:r><w:tab/></w:r>"
        "<w:r><w:t>Col2</w:t></w:r></w:p>\n"
        "    <w:p><w:r><w:t>Line1</w:t></w:r><w:r><w:br/></w:r>"
        "<w:r><w:t>Line2</w:t></w:r></w:p>\n"
        "  </w:body>\n"
        "</w:document>\n"
    ).encode()
    body_bytes = _make_zip(_docx_entries(document_xml))

    text = _extract_text(body_bytes, _DOCX_MIME)

    assert "Col1\tCol2" in text
    assert "Line1\nLine2" in text


def test_docx_empty_body_raises_parse_error() -> None:
    """A valid DOCX with no paragraphs yields no extractable text."""
    document_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<w:document xmlns:w="{_DOCX_NS}"><w:body></w:body></w:document>\n'
    ).encode()
    body_bytes = _make_zip(_docx_entries(document_xml))

    with pytest.raises(DocumentParseError):
        _extract_text(body_bytes, _DOCX_MIME)


def test_docx_missing_document_xml_raises_parse_error() -> None:
    """A ZIP without word/document.xml raises DocumentParseError."""
    body_bytes = _make_zip({"README.txt": b"not a docx"})

    with pytest.raises(DocumentParseError):
        _extract_text(body_bytes, _DOCX_MIME)


def test_docx_malformed_zip_raises_parse_error() -> None:
    """A body that isn't a real ZIP (despite the leading ZIP signature) raises."""
    with pytest.raises(DocumentParseError):
        _extract_text(b"PK\x03\x04garbage", _DOCX_MIME)
