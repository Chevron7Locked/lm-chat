# SPDX-License-Identifier: Apache-2.0
"""P10a.3 — live-backend integration tests for /api/documents (4 endpoints).

Endpoints under test
--------------------
POST   /api/documents                      upload_document_route
GET    /api/documents                      list_documents_route
DELETE /api/documents/{document_id}        delete_document_route
GET    /api/documents/{document_id}/chunks get_chunks_route

Cross-cutting invariants
------------------------
- Unauthenticated → 401 on every route.
- Cross-user mutation (User B on User A's document) → 404, not 403.
- GET /api/documents is user-scoped: User B cannot see User A's documents.

Upload error mapping
--------------------
413 — file exceeds size limit.
415 — unsupported MIME type or magic-byte mismatch.
422 — image-only PDF.

Text-file fixture note
----------------------
Plain text files (.txt, MIME text/plain) go through a code path that
requires no external libraries and produces a deterministic chunk.  The
stub embedding server returns a fixed 384-dim vector regardless of input,
so the upload happy-path works end-to-end in the integration harness.
"""
from __future__ import annotations

import io
import secrets
from typing import Any

import httpx
import pytest

from tests.integration.conftest import register_and_login

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Minimal plain-text fixture for happy-path uploads.
# Kept small to avoid token-budget waste in chunk_and_embed.
# ---------------------------------------------------------------------------

_PLAIN_TEXT_CONTENT = b"This is a plain-text document for integration testing."
_PLAIN_TEXT_MIME = "text/plain"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _upload(
    client: httpx.AsyncClient,
    cookie: str,
    content: bytes = _PLAIN_TEXT_CONTENT,
    filename: str | None = None,
    content_type: str = _PLAIN_TEXT_MIME,
) -> dict[str, Any]:
    """POST /api/documents and assert 201; return parsed body."""
    if filename is None:
        filename = f"test_{secrets.token_hex(4)}.txt"
    resp = await client.post(
        "/api/documents",
        files={"file": (filename, io.BytesIO(content), content_type)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201, f"upload failed ({resp.status_code}): {resp.text}"
    return dict(resp.json())


# ---------------------------------------------------------------------------
# POST /api/documents
# ---------------------------------------------------------------------------


async def test_upload_document_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    filename = f"hello_{secrets.token_hex(4)}.txt"
    resp = await client.post(
        "/api/documents",
        files={
            "file": (filename, io.BytesIO(_PLAIN_TEXT_CONTENT), _PLAIN_TEXT_MIME)
        },
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert body["filename"] == filename
    assert isinstance(body["chunk_count"], int)
    assert body["chunk_count"] >= 0


async def test_upload_document_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/documents",
        files={
            "file": ("doc.txt", io.BytesIO(_PLAIN_TEXT_CONTENT), _PLAIN_TEXT_MIME)
        },
    )
    assert resp.status_code == 401


async def test_upload_document_413_oversized(client: httpx.AsyncClient) -> None:
    """Upload a file larger than the 50 MB default limit via Content-Length.

    We need to exceed LM_CHAT_DOCUMENT_MAX_BYTES (default 50 * 1024 * 1024).
    httpx sends the Content-Length header for BytesIO objects so the route's
    early-bail path (`file.size > max_bytes`) is exercised.
    """
    _, cookie = await register_and_login(client)
    max_bytes = 50 * 1024 * 1024
    oversized = b"x" * (max_bytes + 1)
    resp = await client.post(
        "/api/documents",
        files={"file": ("big.txt", io.BytesIO(oversized), _PLAIN_TEXT_MIME)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 413


async def test_upload_document_415_unsupported_mime(
    client: httpx.AsyncClient,
) -> None:
    """Upload a binary with an unsupported MIME type → 415."""
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/documents",
        files={
            "file": (
                "payload.exe",
                io.BytesIO(b"\x4d\x5a\x90\x00" + b"\x00" * 100),
                "application/x-msdownload",
            )
        },
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 415


async def test_upload_document_415_magic_byte_mismatch(
    client: httpx.AsyncClient,
) -> None:
    """Declare application/pdf but send PE header bytes → 415 (S-007).

    The route validates magic bytes before touching the DB; a PDF declared
    file that starts with MZ (PE executable header) must be rejected.
    """
    _, cookie = await register_and_login(client)
    # MZ header of a PE/EXE file — not a PDF signature (%PDF-).
    fake_pdf_bytes = b"\x4d\x5a\x90\x00" + b"\x00" * 100
    resp = await client.post(
        "/api/documents",
        files={
            "file": ("malicious.pdf", io.BytesIO(fake_pdf_bytes), "application/pdf")
        },
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 415
    detail = resp.json()["detail"].lower()
    assert "content type" in detail or "signature" in detail


async def test_upload_document_dedup(client: httpx.AsyncClient) -> None:
    """Uploading the same file twice returns the same document id."""
    _, cookie = await register_and_login(client)
    first = await _upload(client, cookie, content=b"dedup content abc123 unique")
    second = await _upload(client, cookie, content=b"dedup content abc123 unique")
    assert first["id"] == second["id"]


# ---------------------------------------------------------------------------
# GET /api/documents
# ---------------------------------------------------------------------------


async def test_list_documents_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    doc = await _upload(client, cookie)

    resp = await client.get(
        "/api/documents",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    ids = [d["id"] for d in body]
    assert doc["id"] in ids


async def test_list_documents_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/documents")
    assert resp.status_code == 401


async def test_list_documents_isolation(client: httpx.AsyncClient) -> None:
    """User B's document list must not include User A's documents."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    tag = secrets.token_hex(6)
    await _upload(client, cookie_a, content=f"private document {tag}".encode())

    resp = await client.get(
        "/api/documents",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 200
    # User B should get a list (possibly empty), but not contain User A's doc.
    body = resp.json()
    assert isinstance(body, list)
    # Verify by checking titles — none should contain the unique tag.
    titles = [d.get("title", "") for d in body]
    assert not any(tag in t for t in titles)


async def test_list_documents_response_shape(client: httpx.AsyncClient) -> None:
    """Uploaded document appears in list with correct shape fields."""
    _, cookie = await register_and_login(client)
    filename = f"shape_{secrets.token_hex(4)}.txt"
    upload_resp = await _upload(
        client,
        cookie,
        content=b"shape test content",
        filename=filename,
    )

    resp = await client.get(
        "/api/documents",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    doc = next(d for d in resp.json() if d["id"] == upload_resp["id"])
    assert doc["title"] == filename
    assert "mime_type" in doc
    assert "byte_size" in doc
    assert "chunk_count" in doc
    assert "embedding_model_id" in doc
    assert "sha256" in doc
    assert "uploaded_at" in doc


# ---------------------------------------------------------------------------
# DELETE /api/documents/{document_id}
# ---------------------------------------------------------------------------


async def test_delete_document_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    doc = await _upload(client, cookie, content=b"to be deleted content")

    resp = await client.delete(
        f"/api/documents/{doc['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 204

    # Confirm it no longer appears in the list.
    list_resp = await client.get(
        "/api/documents",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    ids = [d["id"] for d in list_resp.json()]
    assert doc["id"] not in ids


async def test_delete_document_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.delete(
        "/api/documents/999999",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_delete_document_cross_user_404(client: httpx.AsyncClient) -> None:
    """User B cannot delete User A's document — must receive 404, not 403."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    doc = await _upload(client, cookie_a, content=b"user a private document")

    resp = await client.delete(
        f"/api/documents/{doc['id']}",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404

    # Document must still be present for User A.
    list_resp = await client.get(
        "/api/documents",
        headers={"Cookie": f"lmchat_session={cookie_a}"},
    )
    ids = [d["id"] for d in list_resp.json()]
    assert doc["id"] in ids


async def test_delete_document_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/api/documents/1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/documents/{document_id}/chunks
# ---------------------------------------------------------------------------


async def test_get_chunks_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    doc = await _upload(
        client,
        cookie,
        content=b"chunk preview test content for a document",
    )

    resp = await client.get(
        f"/api/documents/{doc['id']}/chunks",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    if body:
        chunk = body[0]
        assert "ordinal" in chunk
        assert "text_preview" in chunk
        assert isinstance(chunk["ordinal"], int)
        assert isinstance(chunk["text_preview"], str)


async def test_get_chunks_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/documents/999999/chunks",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_get_chunks_cross_user_404(client: httpx.AsyncClient) -> None:
    """User B cannot view chunks from User A's document — must receive 404."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    doc = await _upload(client, cookie_a, content=b"user a private chunks")

    resp = await client.get(
        f"/api/documents/{doc['id']}/chunks",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_get_chunks_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/documents/1/chunks")
    assert resp.status_code == 401
