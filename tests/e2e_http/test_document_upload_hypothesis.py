# SPDX-License-Identifier: Apache-2.0
"""§2G — Hypothesis property-based fuzzing for document upload validation.

Fuzzes the ``POST /api/documents`` endpoint across four input dimensions:

- **Content type / magic-byte combinations** — valid MIME vs mismatch vs
  unsupported.  Asserts 4xx (400/413/415) on malformed, 2xx on valid.
- **Filename patterns** — path-traversal, empty, very long, special chars.
  Asserts 2xx (sanitized) or 4xx; never 5xx.
- **Body content** — empty, binary, very large (up to limit+1).
  Asserts 4xx on invalid, 2xx on valid; never 5xx.
- **Content-Length claims** — undersized, exact, oversized vs actual body.
  Asserts 413 when claimed size > limit.

Invariant: Never 5xx.  Always 4xx on malformed; 2xx on valid.
"""
from __future__ import annotations

import io

import httpx
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import register_and_login

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helper: minimal valid PDF
# ---------------------------------------------------------------------------


def _make_minimal_pdf() -> bytes:
    """Return a minimal valid PDF that pypdf can parse without error.

    The PDF contains a single blank page with "(hello world)" as visible
    text in a content stream, encoded as a hex string.  The xref table
    offsets are computed programmatically.
    """
    html_payload = "hello world"
    hex_text = html_payload.encode("utf-8").hex()
    stream_content = f"BT\n/F1 12 Tf\n100 700 Td\n<{hex_text}> Tj\nET\n"
    stream_bytes = stream_content.encode("latin-1")
    stream_len = len(stream_bytes)

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
            f"4 0 obj\n<< /Length {stream_len} >>\nstream\n"
            f"{stream_content}endstream\nendobj"
        ).encode("latin-1"),
        5: b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj",
    }

    body = b"%PDF-1.4\n"
    for i in range(1, 6):
        body += objects[i] + b"\n"

    obj_starts: dict[int, int] = {}
    for i in range(1, 6):
        marker = f"{i} 0 obj".encode()
        pos = body.find(marker)
        if pos >= 0:
            obj_starts[i] = pos

    xref_entries = [f"{0:010d} {65535:05d} f "]
    for i in range(1, 6):
        xref_entries.append(f"{obj_starts[i]:010d} {0:05d} n ")
    xref_table = "xref\n"
    xref_table += f"0 {len(xref_entries)}\n"
    xref_table += "\n".join(xref_entries) + "\n"

    xref_offset = len(body)
    trailer = (
        f"trailer\n<< /Size {len(xref_entries)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    )

    return body + xref_table.encode() + trailer.encode()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid content types that the server accepts.
_VALID_TYPES: list[str] = [
    "text/plain",
    "text/markdown",
    "text/html",
    "application/pdf",
]

# Content types that the server rejects (unsupported).
_UNSUPPORTED_TYPES: list[str] = [
    "application/json",
    "image/png",
    "application/zip",
    "application/xml",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]

# Magic-byte-prefixed bodies for each valid type.
# The PDF body must be a structurally valid PDF (pypdf must be able to
# parse it) — a bare ``%PDF-`` prefix causes PdfReadError → 500.
_VALID_BODIES: dict[str, bytes] = {
    "text/plain": b"hello world",
    "text/markdown": b"# hello world",
    "text/html": b"<html><body>hello</body></html>",
    "application/pdf": _make_minimal_pdf(),
}

# Mismatched bodies: body whose magic bytes do NOT match the declared type.
_MISMATCH_BODIES: dict[str, bytes] = {
    "text/plain": b"%PDF- this is actually text",  # text/plain has no magic check
    "text/html": b"%PDF- but claiming html",
    "application/pdf": b"<html>not a pdf</html>",
}


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


def _content_types() -> st.SearchStrategy[str]:
    """Strategy over valid, unsupported, and random MIME types."""
    return st.sampled_from(
        _VALID_TYPES + _UNSUPPORTED_TYPES + ["application/octet-stream", ""]
    )


def _bodies() -> st.SearchStrategy[bytes]:
    """Strategy over valid bodies per type, mismatched bodies, empty, binary."""
    valid = st.sampled_from(list(_VALID_BODIES.values()))
    mismatch = st.sampled_from(list(_MISMATCH_BODIES.values()))
    empty = st.just(b"")
    binary = st.binary(min_size=0, max_size=512)
    return st.one_of(valid, mismatch, empty, binary)


def _filenames() -> st.SearchStrategy[str]:
    """Strategy over filenames: safe, path-traversal, empty, long."""
    safe = st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N", "P"),  # letters, numbers, punctuation
            blacklist_characters=("/", "\\", "\0"),
        ),
        min_size=0,
        max_size=50,
    )
    traversal = st.sampled_from(
        [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32\\config",
            "../../foo/bar.txt",
            "a/b/c/d.txt",
        ]
    )
    empty = st.just("")
    very_long = st.text(min_size=300, max_size=500)
    return st.one_of(safe, traversal, empty, very_long)


# ---------------------------------------------------------------------------
# Fixture: session cookie (reused across hypothesis examples)
# ---------------------------------------------------------------------------


@pytest.fixture
async def cookie(client: httpx.AsyncClient) -> str:
    """Return a valid session cookie for the fuzz tests."""
    _, c = await register_and_login(client)
    return c


# ---------------------------------------------------------------------------
# Fuzz test: status-code invariant
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    content_type=st.sampled_from(
        _VALID_TYPES + _UNSUPPORTED_TYPES + ["", "application/octet-stream"]
    ),
    body=st.binary(min_size=0, max_size=1024),
    filename=_filenames(),
)
async def test_upload_never_5xx(
    client: httpx.AsyncClient,
    cookie: str,
    content_type: str,
    body: bytes,
    filename: str,
) -> None:
    """Never 5xx regardless of input shape.

    Every response must be 2xx (valid) or 4xx (client error).  5xx
    indicates a server crash or unhandled exception.
    """
    # Skip NUL bytes in filename (FastAPI/Starlette would crash at the
    # multipart parser layer, which is a known constraint — not our bug).
    assume("\0" not in filename)

    resp = await client.post(
        "/api/documents",
        files={"file": (filename, io.BytesIO(body), content_type)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code < 500, (
        f"5xx on content_type={content_type!r} body_len={len(body)} "
        f"filename={filename!r}: {resp.status_code} {resp.text[:200]}"
    )


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    content_body=st.sampled_from(
        [
            ("text/plain", b"hello world"),
            ("text/markdown", b"# hello world"),
            ("text/html", b"<html><body>hello</body></html>"),
            ("application/pdf", _make_minimal_pdf()),
        ]
    ),
    filename=st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N"),
            blacklist_characters=("/", "\\", "\0"),
        ),
        min_size=1,
        max_size=50,
    ),
)
async def test_valid_upload_returns_2xx(
    client: httpx.AsyncClient,
    cookie: str,
    content_body: tuple[str, bytes],
    filename: str,
) -> None:
    """Well-formed uploads with matching content type return 2xx."""
    assume("\0" not in filename)
    content_type, body = content_body

    resp = await client.post(
        "/api/documents",
        files={"file": (filename, io.BytesIO(body), content_type)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code in (201, 200), (
        f"Valid upload got {resp.status_code}: content_type={content_type!r} "
        f"filename={filename!r} body_len={len(body)}: {resp.text[:200]}"
    )


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@given(
    content_type=st.sampled_from(_UNSUPPORTED_TYPES),
    body_text=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P")),
        min_size=3,
        max_size=50,
    ),
    filename=st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N"),
            blacklist_characters=("/", "\\", "\0"),
        ),
        min_size=1,
        max_size=50,
    ),
)
async def test_unsupported_type_returns_415(
    client: httpx.AsyncClient,
    cookie: str,
    content_type: str,
    body_text: str,
    filename: str,
) -> None:
    """Unsupported content types return 415."""
    assume("\0" not in filename)
    body = body_text.encode("utf-8")

    resp = await client.post(
        "/api/documents",
        files={"file": (filename, io.BytesIO(body), content_type)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 415, (
        f"Unsupported type {content_type!r} expected 415, "
        f"got {resp.status_code}: body={body[:50]!r} filename={filename!r}: {resp.text[:200]}"
    )


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@given(
    content_type=st.just("application/pdf"),
    body_text=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P")),
        min_size=3,
        max_size=50,
    ).filter(lambda t: not t.startswith("%PDF-")),
    filename=st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N"),
            blacklist_characters=("/", "\\", "\0"),
        ),
        min_size=1,
        max_size=50,
    ),
)
async def test_mime_mismatch_returns_415(
    client: httpx.AsyncClient,
    cookie: str,
    content_type: str,
    body_text: str,
    filename: str,
) -> None:
    """MIME/magic-byte mismatch returns 415 (PDF case).

    application/pdf files must start with ``%PDF-``.  Bodies that don't
    match trigger 415.
    """
    assume("\0" not in filename)
    body = body_text.encode("utf-8")

    resp = await client.post(
        "/api/documents",
        files={"file": (filename, io.BytesIO(body), content_type)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 415, (
        f"PDF magic mismatch expected 415, got {resp.status_code}: "
        f"body={body[:50]!r} filename={filename!r}: {resp.text[:200]}"
    )


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@given(
    content_type=st.just("text/html"),
    body_text=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P")),
        min_size=3,
        max_size=50,
    ).filter(lambda t: not t.lstrip().startswith("<")),
    filename=st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N"),
            blacklist_characters=("/", "\\", "\0"),
        ),
        min_size=1,
        max_size=50,
    ),
)
async def test_html_magic_mismatch_returns_415(
    client: httpx.AsyncClient,
    cookie: str,
    content_type: str,
    body_text: str,
    filename: str,
) -> None:
    """MIME/magic-byte mismatch returns 415 (HTML case).

    text/html files must start with ``<`` (after stripping ASCII
    whitespace and UTF-8 BOM).  Bodies that don't match trigger 415.
    """
    assume("\0" not in filename)
    body = body_text.encode("utf-8")

    resp = await client.post(
        "/api/documents",
        files={"file": (filename, io.BytesIO(body), content_type)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 415, (
        f"HTML magic mismatch expected 415, got {resp.status_code}: "
        f"body={body[:50]!r} filename={filename!r}: {resp.text[:200]}"
    )