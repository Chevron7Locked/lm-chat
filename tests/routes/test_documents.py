# SPDX-License-Identifier: Apache-2.0
"""Tests for documents route — K-002, K-005, and P9b magic-byte 415.

K-002: _sanitize_filename must strip path components and unsafe chars.
K-005: uploads exceeding max bytes are rejected before the body is read
       in full (early rejection via file.size if available).
P9b-A: MimeTypeMismatchError from magic-byte check maps to HTTP 415.
"""
from __future__ import annotations

import unittest.mock as mock

import pytest

from lmchat.routes.documents import _sanitize_filename
from lmchat.services.documents_service import MimeTypeMismatchError

# ---------------------------------------------------------------------------
# K-002 — _sanitize_filename
# ---------------------------------------------------------------------------


def test_sanitize_filename_path_traversal_unix() -> None:
    """UNIX path traversal is stripped to the basename."""
    assert _sanitize_filename("../../etc/passwd") == "passwd"


def test_sanitize_filename_path_traversal_windows() -> None:
    """Windows backslash path traversal is stripped to the basename."""
    assert _sanitize_filename("..\\..\\windows\\system32\\evil") == "evil"


def test_sanitize_filename_mixed_separators() -> None:
    """Mixed path separators: last basename component is used."""
    assert _sanitize_filename("/foo/bar/baz.txt") == "baz.txt"


def test_sanitize_filename_nul_byte() -> None:
    """NUL bytes are replaced with underscore."""
    result = _sanitize_filename("file\x00name.txt")
    assert "\x00" not in result
    assert len(result) > 0


def test_sanitize_filename_empty_string() -> None:
    """Empty filename falls back to 'upload'."""
    assert _sanitize_filename("") == "upload"


def test_sanitize_filename_none() -> None:
    """None falls back to 'upload'."""
    assert _sanitize_filename(None) == "upload"


def test_sanitize_filename_oversized() -> None:
    """Names longer than 255 chars are capped."""
    long_name = "a" * 300 + ".txt"
    result = _sanitize_filename(long_name)
    assert len(result) <= 255


def test_sanitize_filename_safe_chars_preserved() -> None:
    """Alphanumeric, dots, dashes, underscores, and spaces survive sanitization."""
    name = "my-file_v2.3 draft.pdf"
    result = _sanitize_filename(name)
    assert result == name


def test_sanitize_filename_special_chars_replaced() -> None:
    """Characters outside the allowed set are replaced with underscore."""
    result = _sanitize_filename("file<>|?*.txt")
    assert "<" not in result
    assert ">" not in result
    assert "|" not in result
    assert "?" not in result
    assert "*" not in result
    assert result.endswith(".txt")


def test_sanitize_filename_only_unsafe_chars_falls_back() -> None:
    """A name that reduces to empty after sanitization falls back to 'upload'.

    Traversal-only input (e.g. ``'../'``, ``'///'``) strips all path
    separators, leaving an empty string that falls back to ``'upload'``.
    This is intentional safety behavior: the fallback is deterministic and
    prevents the server from persisting a document with an empty or
    traversal-derived filename.

    Known limitation: in a multi-user system, concurrent uploads from
    different users that both fall back to ``'upload'`` may collide on the
    filesystem if storage is backed by a flat directory.  This is a
    documented tradeoff; the document PK (integer ID) is the canonical
    identifier used in retrieval, not the filename.
    """
    result = _sanitize_filename("../../../")
    assert result == "upload"
    # Slash-only input also reduces to empty → fallback.
    assert _sanitize_filename("///") == "upload"


# ---------------------------------------------------------------------------
# P9b Item A — MimeTypeMismatchError maps to HTTP 415 (S-007)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_mime_mismatch_returns_415() -> None:
    """MimeTypeMismatchError from upload_document maps to HTTP 415.

    Tests the route-level exception handler added in P9b.
    Closes P8 DEFERRED §17 S-007.
    """
    import io
    from datetime import UTC, datetime

    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport

    from lmchat.routes._dependencies import (
        get_embedding_client_dep,
        get_engine_dep,
        get_models_service_dep,
        require_user,
    )
    from lmchat.routes.documents import router
    from lmchat.services.auth_service import User

    # Build a minimal FastAPI app with just the documents router.
    app = FastAPI()

    # Override dependencies with stubs.
    async def _fake_user() -> User:
        now = datetime.now(UTC)
        return User(
            id=1,
            username="test",
            is_admin=False,
            created_at=now,
            updated_at=now,
            password_hash="x",
            totp_secret=None,
        )

    app.dependency_overrides[require_user] = _fake_user
    app.dependency_overrides[get_engine_dep] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_models_service_dep] = lambda: None
    app.include_router(router)

    # Patch upload_document to raise MimeTypeMismatchError.
    mime_mismatch = MimeTypeMismatchError("application/pdf", "PDF magic bytes (%PDF-)")

    with mock.patch(
        "lmchat.routes.documents.upload_document",
        side_effect=mime_mismatch,
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/documents",
                files={"file": ("evil.pdf", io.BytesIO(b"MZ\x90\x00"), "application/pdf")},
            )

    assert resp.status_code == 415
    body = resp.json()
    assert body["detail"] == "content type does not match file signature"


# ---------------------------------------------------------------------------
# Fix C — NoEmbeddingModelLoadedError maps to HTTP 503 (not 500)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_no_embedding_model_returns_503() -> None:
    """NoEmbeddingModelLoadedError from upload_document maps to HTTP 503.

    Fix C: the upload route did not catch NoEmbeddingModelLoadedError
    (a MemoryServiceError, NOT a RuntimeError), so "preferred set but
    not loaded" / "no embedder loaded" cases surfaced as a generic 500
    with the actionable message swallowed. After the fix, these surface
    as 503 with str(exc) in the detail body — mirroring projects.py.

    Red-on-revert: remove the except NoEmbeddingModelLoadedError clause
    → this test will get 500 / "Internal Server Error" instead of 503.
    """
    import io
    from datetime import UTC, datetime

    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport

    from lmchat.routes._dependencies import (
        get_embedding_client_dep,
        get_engine_dep,
        get_models_service_dep,
        require_user,
    )
    from lmchat.routes.documents import router
    from lmchat.services.auth_service import User
    from lmchat.services.memory_service import NoEmbeddingModelLoadedError

    app = FastAPI()

    async def _fake_user() -> User:
        now = datetime.now(UTC)
        return User(
            id=1,
            username="test",
            is_admin=False,
            created_at=now,
            updated_at=now,
            password_hash="x",
            totp_secret=None,
        )

    app.dependency_overrides[require_user] = _fake_user
    app.dependency_overrides[get_engine_dep] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_models_service_dep] = lambda: None
    app.include_router(router)

    _exc_msg = (
        "The selected embedding model 'nomic-embed-v1.5' is not currently "
        "loaded in LM Studio. Load it, or choose a different embedding "
        "model in Settings."
    )
    with mock.patch(
        "lmchat.routes.documents.upload_document",
        side_effect=NoEmbeddingModelLoadedError(_exc_msg),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/documents",
                files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
            )

    assert resp.status_code == 503, f"expected 503, got {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"]
    assert "not currently loaded" in detail.lower(), (
        f"expected actionable message in detail, got: {detail!r}"
    )



# ---------------------------------------------------------------------------
# EPUB/DOCX — DocumentParseError maps to HTTP 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_document_parse_error_returns_422() -> None:
    """DocumentParseError from upload_document maps to HTTP 422.

    Covers malformed or empty-text EPUB/DOCX containers — mirrors how
    ImageOnlyPdfError (image-only PDF) is mapped.
    """
    import io
    from datetime import UTC, datetime

    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport

    from lmchat.routes._dependencies import (
        get_embedding_client_dep,
        get_engine_dep,
        get_models_service_dep,
        require_user,
    )
    from lmchat.routes.documents import router
    from lmchat.services.auth_service import User
    from lmchat.services.documents_service import DocumentParseError

    app = FastAPI()

    async def _fake_user() -> User:
        now = datetime.now(UTC)
        return User(
            id=1,
            username="test",
            is_admin=False,
            created_at=now,
            updated_at=now,
            password_hash="x",
            totp_secret=None,
        )

    app.dependency_overrides[require_user] = _fake_user
    app.dependency_overrides[get_engine_dep] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_models_service_dep] = lambda: None
    app.include_router(router)

    parse_error = DocumentParseError("Could not extract any text from this EPUB.")

    with mock.patch(
        "lmchat.routes.documents.upload_document",
        side_effect=parse_error,
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/documents",
                files={
                    "file": (
                        "book.epub",
                        io.BytesIO(b"PK\x03\x04garbage"),
                        "application/epub+zip",
                    )
                },
            )

    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["detail"] == "Could not extract any text from this EPUB."
