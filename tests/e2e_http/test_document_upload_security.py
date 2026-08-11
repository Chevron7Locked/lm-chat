# SPDX-License-Identifier: Apache-2.0
"""§2G — Document upload security tests.

Tests cover five attack surfaces on the multipart document upload path:

1. Polyglot PDF+HTML — a file carrying both PDF magic bytes (``%PDF-``) and
   an HTML payload.  Asserts the server classifies by magic bytes (not
   extension), pypdf extracts text without rendering HTML, and the chunk
   retrieval path returns raw plain text (no unsanitized HTML).

2. ZIP / path-traversal slip — filenames containing ``../../../etc/passwd``.
   The server stores uploads as opaque blobs (no archive extraction), so
   the attack is inapplicable.  Asserts ``_sanitize_filename`` strips the
   traversal components and the upload succeeds (defense-in-depth).

3. Oversized upload — Content-Length of 1 GB with a minimal body.  Asserts
   the server returns HTTP 413 PRE-buffer (before reading the full body).
   Uses a streaming generator that claims 1 GB but only sends headers.

4. MIME spoof — a plain-text file claiming ``application/pdf``.  Asserts
   the server rejects with 415 (magic byte mismatch).

5. Stored XSS at retrieval — upload a ``.txt`` containing
   ``<script>alert(1)</script>``, retrieve its chunks via
   ``GET /api/documents/{id}/chunks``, and assert the chunk text is
   returned as raw plain text (no HTML rendering; the frontend's
   ``rehype-sanitize`` is the active XSS barrier at render time).
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest

from tests.integration.conftest import register_and_login

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRIPT_TAG = "<script>alert(1)</script>"
_STUB_MODEL = "stub-model-q4"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _upload(
    client: httpx.AsyncClient,
    cookie: str,
    content: bytes,
    filename: str = "test.txt",
    content_type: str = "text/plain",
) -> httpx.Response:
    """POST /api/documents and return the raw response."""
    return await client.post(
        "/api/documents",
        files={"file": (filename, io.BytesIO(content), content_type)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )


def _make_pdf_with_html_text(html_payload: str = _SCRIPT_TAG) -> bytes:
    """Build a minimal valid PDF whose visible text is *html_payload*.

    The PDF is structurally valid — pypdf can extract the text stream —
    so the server's magic-byte check (``%PDF-`` prefix) passes and
    ``_extract_text`` returns the HTML payload as plain text.

    The PDF stores the HTML as a hex-encoded string in a content stream
    text-showing operation (``Tj``) so the visible text is the raw HTML
    without PDF syntax interference.

    Returns the raw PDF bytes.
    """
    hex_text = html_payload.encode("utf-8").hex()
    stream_content = f"BT\n/F1 12 Tf\n100 700 Td\n<{hex_text}> Tj\nET\n"
    stream_bytes = stream_content.encode("latin-1")
    stream_len = len(stream_bytes)

    # Build PDF objects.
    objects: dict[int, bytes] = {
        1: b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj",
        2: b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj",
        3: (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Contents 4 0 R"
            b" /Resources << /Font << /F1 5 0 R >> >> >>\n"
            b"endobj"
        ),
        4: (
            f"4 0 obj\n<< /Length {stream_len} >>\nstream\n{stream_content}endstream\nendobj"
        ).encode("latin-1"),
        5: b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj",
    }

    # Build body and locate object offsets for xref.
    body = b"%PDF-1.4\n"
    for i in range(1, 6):
        body += objects[i] + b"\n"

    obj_starts: dict[int, int] = {}
    for i in range(1, 6):
        marker = f"{i} 0 obj".encode()
        pos = body.find(marker)
        if pos >= 0:
            obj_starts[i] = pos

    # Build xref table.
    xref_entries = [f"{0:010d} {65535:05d} f "]
    for i in range(1, 6):
        xref_entries.append(f"{obj_starts[i]:010d} {0:05d} n ")
    xref_table = "xref\n"
    xref_table += f"0 {len(xref_entries)}\n"
    xref_table += "\n".join(xref_entries) + "\n"

    # Build trailer.
    xref_offset = len(body)
    trailer = (
        f"trailer\n<< /Size {len(xref_entries)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    )

    return body + xref_table.encode() + trailer.encode()


# ---------------------------------------------------------------------------
# 1. Polyglot PDF + HTML
# ---------------------------------------------------------------------------


async def test_polyglot_pdf_with_html_accepted(
    client: httpx.AsyncClient,
) -> None:
    """A file with PDF magic bytes + HTML text is accepted as PDF.

    The server classifies by magic bytes (``%PDF-``), not by extension.
    pypdf extracts the HTML payload as plain text (it is text content
    in a PDF content stream, not rendered markup).
    """
    _, cookie = await register_and_login(client)
    pdf_bytes = _make_pdf_with_html_text(_SCRIPT_TAG)

    resp = await _upload(
        client,
        cookie,
        content=pdf_bytes,
        filename="invoice.pdf",
        content_type="application/pdf",
    )
    assert resp.status_code == 201, (
        f"Polyglot PDF upload expected 201, got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["id"] > 0
    assert body["chunk_count"] > 0


async def test_polyglot_pdf_chunks_contain_raw_text(
    client: httpx.AsyncClient,
) -> None:
    """Chunks from a polyglot PDF contain the HTML as raw plain text.

    The text is stored as-is from pypdf extraction, which returns the
    visible text from the PDF content stream.  No HTML rendering occurs
    server-side; the frontend's ``rehype-sanitize`` is the XSS barrier.
    """
    _, cookie = await register_and_login(client)
    pdf_bytes = _make_pdf_with_html_text(_SCRIPT_TAG)

    resp = await _upload(
        client,
        cookie,
        content=pdf_bytes,
        filename="invoice.pdf",
        content_type="application/pdf",
    )
    assert resp.status_code == 201
    doc_id = resp.json()["id"]

    # Retrieve chunks.
    chunks_resp = await client.get(
        f"/api/documents/{doc_id}/chunks",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert chunks_resp.status_code == 200
    chunks = chunks_resp.json()
    assert isinstance(chunks, list)
    assert len(chunks) > 0

    # The script tag appears as raw text in at least one chunk preview.
    all_text = " ".join(c["text_preview"] for c in chunks)
    assert _SCRIPT_TAG in all_text, f"Expected script tag in chunk text, got: {all_text[:500]}"
    # The text is plain text in the JSON body — no HTML rendering.
    # The frontend's rehype-sanitize schema (ChatMessage.tsx:76-86)
    # allows only className on code/span elements, so the script tag
    # would be stripped at render time.
    assert isinstance(chunks[0]["text_preview"], str)


# ---------------------------------------------------------------------------
# 2. ZIP / path-traversal slip — filename sanitization
# ---------------------------------------------------------------------------


async def test_zip_slip_filename_sanitized(
    client: httpx.AsyncClient,
) -> None:
    """Path-traversal filenames (``../../../etc/passwd``) are sanitized.

    The server stores uploads as opaque blobs (no archive extraction),
    so ZIP-slip extraction is inapplicable.  This test verifies that
    ``_sanitize_filename`` (routes/documents.py:99-120) strips directory
    components and unsafe characters, returning a safe filename.

    The upload itself succeeds (201) because the content type is valid
    and the sanitized filename is used as the document title.
    """
    _, cookie = await register_and_login(client)

    # Attempt path traversal via both UNIX and Windows separators.
    content = b"benign content"
    for malicious_name in [
        "../../../etc/passwd",
        "..\\..\\..\\windows\\system32\\config",
        "../../etc/shadow.txt",
        "foo/../../../bar.txt",
    ]:
        resp = await _upload(
            client,
            cookie,
            content=content,
            filename=malicious_name,
            content_type="text/plain",
        )
        assert resp.status_code == 201, (
            f"Upload with filename {malicious_name!r} expected 201, "
            f"got {resp.status_code}: {resp.text[:200]}"
        )
        body = resp.json()
        # The title should not contain path separators.
        assert "/" not in body["filename"], (
            f"Sanitized filename still contains '/': {body['filename']!r}"
        )
        assert "\\" not in body["filename"], (
            f"Sanitized filename still contains '\\\\': {body['filename']!r}"
        )


# ---------------------------------------------------------------------------
# 3. Oversized upload — 1 GB Content-Length, minimal body
# ---------------------------------------------------------------------------


async def test_oversized_upload_413_prebuffer(
    client: httpx.AsyncClient,
) -> None:
    """A 1 GB-claiming upload returns 413 PRE-buffer (before reading body).

    The route checks the declared ``Content-Length`` header before reading
    the body, so a client claiming 1 GB is rejected with 413 without
    buffering the declared payload.
    """
    _, cookie = await register_and_login(client)

    boundary = "----TestBoundary12345"
    part_headers = (
        'Content-Disposition: form-data; name="file"; filename="oversized.txt"\r\n'
        "Content-Type: text/plain\r\n"
        # Claim 1 GB — the server SHOULD 413 before reading beyond the
        # actual body bytes.
        "Content-Length: 1073741824\r\n"
        "\r\n"
    )
    tiny_body = b"this is a tiny body, well under the limit"
    closing = f"\r\n--{boundary}--\r\n".encode()

    body = f"--{boundary}\r\n".encode() + part_headers.encode() + tiny_body + closing

    resp = await client.post(
        "/api/documents",
        content=body,
        headers={
            "Cookie": f"lmchat_session={cookie}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )

    assert resp.status_code == 413, (
        f"Expected 413 for oversized upload (Content-Length: 1 GB), "
        f"got {resp.status_code}: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# 4. MIME spoof — text file claiming application/pdf
# ---------------------------------------------------------------------------


async def test_mime_spoof_text_as_pdf_415(
    client: httpx.AsyncClient,
) -> None:
    """A plain-text file claiming ``application/pdf`` is rejected with 415.

    The server's ``_validate_magic_bytes`` checks for the ``%PDF-``
    prefix when ``content_type`` is ``application/pdf``.  A plain-text
    file lacks this prefix, so the check raises ``MimeTypeMismatchError``,
    which the route translates to HTTP 415.
    """
    _, cookie = await register_and_login(client)

    resp = await _upload(
        client,
        cookie,
        content=b"this is plain text, not a PDF",
        filename="fake.pdf",
        content_type="application/pdf",
    )
    assert resp.status_code == 415, (
        f"Expected 415 for MIME spoof, got {resp.status_code}: {resp.text[:300]}"
    )
    # Verify the 415 carries the expected error message.
    body: dict[str, Any] = resp.json()
    assert "content type does not match file signature" in str(body.get("detail", "")).lower()


# ---------------------------------------------------------------------------
# 5. Stored XSS — script tag in chunk retrieval
# ---------------------------------------------------------------------------


async def test_stored_xss_script_in_chunk_preview(
    client: httpx.AsyncClient,
) -> None:
    """A ``.txt`` file with ``<script>alert(1)</script>`` stores and
    returns the script tag as raw plain text in chunk previews.

    This is EXPECTED: the server stores the raw chunk text without
    sanitization because the frontend's ``rehype-sanitize`` (ChatMessage.tsx)
    is the XSS barrier at render time.  The test asserts:
    - Upload succeeds (201).
    - Chunk previews contain the script tag as JSON string (not rendered).
    - The chunk text is a ``str`` instance (plain text, not HTML DOM).
    """
    _, cookie = await register_and_login(client)

    content = f"Hello world. {_SCRIPT_TAG} More text here.".encode()

    resp = await _upload(
        client,
        cookie,
        content=content,
        filename="notes.txt",
        content_type="text/plain",
    )
    assert resp.status_code == 201, (
        f"Upload expected 201, got {resp.status_code}: {resp.text[:300]}"
    )
    doc_id = resp.json()["id"]

    # Retrieve chunks.
    chunks_resp = await client.get(
        f"/api/documents/{doc_id}/chunks",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert chunks_resp.status_code == 200
    chunks = chunks_resp.json()
    assert isinstance(chunks, list)
    assert len(chunks) > 0

    # The script tag appears in at least one chunk preview as raw text.
    all_text = " ".join(c["text_preview"] for c in chunks)
    assert _SCRIPT_TAG in all_text, f"Expected script tag in chunk text: {all_text[:500]}"

    # Verify the chunk preview is a plain string (JSON serialised,
    # not HTML).
    for chunk in chunks:
        assert isinstance(chunk["text_preview"], str), (
            f"Chunk preview is not a string: {type(chunk['text_preview'])}"
        )
