# SPDX-License-Identifier: Apache-2.0
"""GET /api/memory/embedding/status — Settings UI visibility surface.

The endpoint surfaces ``active_model_id`` + indexed-message counts and an
``embedding_status`` resolver sentinel so the Settings UI and the chat-level
``/api/chats/{id}/rag_mode`` endpoint draw from the same source-of-truth.
The sentinel takes one of:

* ``"ok"`` — an embedding model is loaded; retrieval will run.
* ``"no_embedding_model"`` — no embedding model loaded; retrieval
  silently skips.
* ``"pinned_model_unavailable"`` — project-scoped only (not firable
  here since this route is user-scoped), included in the union for
  shape parity with the chat badge.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lmchat.services.auth_service import _reset_dummy_hash_cache


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/embedding_status.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv(
        "LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!"
    )
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()
    return create_app()


@pytest.fixture()
def test_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        yield client


def _register_and_login(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        data={"username": "alice", "password": "correct-horse-battery"},
    )
    client.post(
        "/api/auth/login",
        data={"username": "alice", "password": "correct-horse-battery"},
    )


# ─── Tests ────────────────────────────────────────────────────────────────


def test_embedding_status_returns_resolver_sentinel(
    test_client: TestClient,
) -> None:
    """Response carries the ``embedding_status`` field with one of the
    three sentinel values. (Default test app has no live LM Studio →
    expected status is ``"no_embedding_model"``.)"""
    _register_and_login(test_client)
    resp = test_client.get("/api/memory/embedding/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "embedding_status" in body, body
    assert body["embedding_status"] in {
        "ok",
        "no_embedding_model",
        "pinned_model_unavailable",
    }


def test_embedding_status_sentinel_matches_active_model_id_absence(
    test_client: TestClient,
) -> None:
    """When ``active_model_id`` is None (no LM Studio embedding model
    loaded in the test fixture), the sentinel reports
    ``no_embedding_model``."""
    _register_and_login(test_client)
    resp = test_client.get("/api/memory/embedding/status")
    body = resp.json()
    if body.get("active_model_id") is None:
        assert body["embedding_status"] == "no_embedding_model", body


def test_embedding_status_shape_is_stable(
    test_client: TestClient,
) -> None:
    """Pin every documented field so a future schema drift fires a
    regression here, not at the FE consumer (which reads
    ``data.embedding_status`` non-optionally per
    ``useEmbeddingStatus.ts``)."""
    _register_and_login(test_client)
    resp = test_client.get("/api/memory/embedding/status")
    body = resp.json()
    expected_keys = {
        "active_model_id",
        "active_model_error_reason",
        "loaded_embedding_models",
        "total_indexed_messages",
        "last_indexed_at",
        "models_in_use",
        "embedding_status",
        "write_failure_count",
        "write_last_error",
    }
    assert set(body.keys()) == expected_keys, (
        f"unexpected shape: missing={expected_keys - set(body.keys())}, "
        f"extra={set(body.keys()) - expected_keys}"
    )


def test_embedding_status_requires_auth(test_client: TestClient) -> None:
    """The route is auth-gated; unauthenticated callers get 401."""
    resp = test_client.get("/api/memory/embedding/status")
    assert resp.status_code == 401, resp.text


# ─── resolver-reason discriminator ───────────────────────────
#
# Both of these tests fake ``memory_service.embedding_status()`` (via
# dependency_overrides) and patch ``retrieval_service.resolve_embedding_model_status``
# so the assertions are deterministic regardless of whatever LM Studio state
# happens to be reachable from this test environment.


def _override_memory_service(
    test_client: TestClient, snap: dict[str, Any]
) -> Any:  # noqa: ANN401
    """Install a fake memory_service whose embedding_status() returns *snap*.

    Returns the app so the caller can pop the override afterward.
    """
    from unittest.mock import AsyncMock

    from lmchat.routes.memory import _get_memory_service

    fake = AsyncMock()
    fake.embedding_status = AsyncMock(return_value=snap)
    app = cast(FastAPI, test_client.app)
    app.dependency_overrides[_get_memory_service] = lambda: fake
    return app


def test_embedding_status_preferred_not_loaded_returns_distinct_status(
    test_client: TestClient,
) -> None:
    """A personally-preferred embedding model that isn't loaded must report
    a status DISTINCT from "no_embedding_model" — the admin needs to load
    their pinned model specifically, not just any embedder. Reuses
    "pinned_model_unavailable" (same shape as the project-pin case)."""
    from unittest.mock import AsyncMock, patch

    from lmchat.services.memory_service import (
        EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED,
    )

    _register_and_login(test_client)

    app = _override_memory_service(
        test_client,
        {
            "active_model_id": None,
            "active_model_error_reason": EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED,
            "loaded_embedding_models": [],
            "total_indexed_messages": 0,
            "last_indexed_at": None,
            "models_in_use": {},
            "write_failure_count": 0,
            "write_last_error": None,
        },
    )
    try:
        with patch(
            "lmchat.services.retrieval_service.resolve_embedding_model_status",
            new_callable=AsyncMock,
            return_value=(None, "no_embedding_model"),
        ):
            resp = test_client.get("/api/memory/embedding/status")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["embedding_status"] == "pinned_model_unavailable", body
    assert (
        body["active_model_error_reason"]
        == EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED
    )


def test_embedding_status_generic_resolver_error_reason(
    test_client: TestClient,
) -> None:
    """A generic (non-NoEmbeddingModelLoadedError) resolver failure surfaces
    active_model_error_reason == "resolver_error" — distinct from the
    pinned-not-loaded case — while embedding_status stays no_embedding_model
    (a generic resolver error isn't known to be the pinned-specific case)."""
    from unittest.mock import AsyncMock, patch

    _register_and_login(test_client)

    app = _override_memory_service(
        test_client,
        {
            "active_model_id": None,
            "active_model_error_reason": "resolver_error",
            "loaded_embedding_models": [],
            "total_indexed_messages": 0,
            "last_indexed_at": None,
            "models_in_use": {},
            "write_failure_count": 0,
            "write_last_error": None,
        },
    )
    try:
        with patch(
            "lmchat.services.retrieval_service.resolve_embedding_model_status",
            new_callable=AsyncMock,
            return_value=(None, "no_embedding_model"),
        ):
            resp = test_client.get("/api/memory/embedding/status")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_model_error_reason"] == "resolver_error"
    assert body["embedding_status"] == "no_embedding_model"


def test_embedding_status_surfaces_write_failure_counter(
    test_client: TestClient,
) -> None:
    """The write-path failure counter/last-error reaches the
    response body additively — Settings visibility for repeated
    stream.memory_index_failed events, not just log-only."""
    from unittest.mock import AsyncMock, patch

    _register_and_login(test_client)

    app = _override_memory_service(
        test_client,
        {
            "active_model_id": None,
            "active_model_error_reason": None,
            "loaded_embedding_models": [],
            "total_indexed_messages": 0,
            "last_indexed_at": None,
            "models_in_use": {},
            "write_failure_count": 3,
            "write_last_error": "embedding model offline",
        },
    )
    try:
        with patch(
            "lmchat.services.retrieval_service.resolve_embedding_model_status",
            new_callable=AsyncMock,
            return_value=(None, "no_embedding_model"),
        ):
            resp = test_client.get("/api/memory/embedding/status")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["write_failure_count"] == 3
    assert body["write_last_error"] == "embedding model offline"
