# SPDX-License-Identifier: Apache-2.0
"""Integration tests for per-preset model defaults routes (W5).

Covers:
- ``GET /api/settings/preset-models`` — 401 unauthenticated; 200 with {}
  when no defaults saved.
- ``PUT /api/settings/preset-models`` — persists mapping; GET returns it.
- ``PUT`` with unknown provider → entry dropped (partial persist).
- ``PUT`` empty body → clears; GET returns {}.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_models_service_dep
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/preset_models_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()
    app = create_app()

    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models
    return app


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()


async def _insert_user(tmp_path: Path, username: str = "alice") -> None:
    """Seed a user directly into the DB (mirrors test_lm_studio_settings pattern)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/preset_models_route_test.db"
    pw_hash = hash_password("test-pw", n=_LOW_N, r=8, p=1)
    eng = create_async_engine(db_url, pool_pre_ping=True)
    try:
        from sqlalchemy import text
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO users (username, password_hash, is_admin) "
                    "VALUES (:u, :pw, 0)"
                ),
                {"u": username, "pw": pw_hash},
            )
    finally:
        await eng.dispose()


@pytest.fixture()
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[return]
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        # No provider_registry → service accepts all slugs (no registry = test env).
        yield client


def _login(client: TestClient, username: str = "alice") -> None:
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": "test-pw"},
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_requires_auth(test_client: TestClient) -> None:
    """Unauthenticated GET returns 401."""
    resp = test_client.get("/api/settings/preset-models")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_returns_empty_when_nothing_saved(
    tmp_path: Path, test_client: TestClient
) -> None:
    """No preset models saved → GET returns {} with 200."""
    await _insert_user(tmp_path)
    _login(test_client)
    resp = test_client.get("/api/settings/preset-models")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {}


@pytest.mark.asyncio
async def test_put_requires_auth(test_client: TestClient) -> None:
    """Unauthenticated PUT returns 401."""
    resp = test_client.put(
        "/api/settings/preset-models",
        json={"general": {"provider": "lmstudio", "model_id": "phi-4"}},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_persists_and_get_returns_it(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT a mapping with lmstudio provider → GET returns the same mapping."""
    await _insert_user(tmp_path)
    _login(test_client)
    mapping = {
        "general": {"provider": "lmstudio", "model_id": "phi-4"},
        "coder": {"provider": "lmstudio", "model_id": "qwen-coder"},
    }
    put_resp = test_client.put("/api/settings/preset-models", json=mapping)
    assert put_resp.status_code == 200, put_resp.text
    saved = put_resp.json()
    assert saved == mapping

    get_resp = test_client.get("/api/settings/preset-models")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json() == mapping


@pytest.mark.asyncio
async def test_put_unknown_provider_entry_dropped(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Unknown provider entry is dropped; valid lmstudio entry is kept."""
    await _insert_user(tmp_path)
    _login(test_client)
    # The live app's provider_registry won't know "ghost-provider".
    mapping = {
        "general": {"provider": "lmstudio", "model_id": "phi-4"},
        "research": {"provider": "ghost-provider", "model_id": "m1"},
    }
    put_resp = test_client.put("/api/settings/preset-models", json=mapping)
    assert put_resp.status_code == 200, put_resp.text
    saved = put_resp.json()
    # lmstudio entry kept; ghost entry dropped.
    assert "general" in saved
    assert "research" not in saved


@pytest.mark.asyncio
async def test_put_empty_clears_mapping(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT {} clears the stored mapping; subsequent GET returns {}."""
    await _insert_user(tmp_path)
    _login(test_client)
    test_client.put(
        "/api/settings/preset-models",
        json={"coder": {"provider": "lmstudio", "model_id": "qwen-coder"}},
    )
    clear_resp = test_client.put("/api/settings/preset-models", json={})
    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json() == {}

    get_resp = test_client.get("/api/settings/preset-models")
    assert get_resp.json() == {}


@pytest.mark.asyncio
async def test_put_overwrites_previous(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Second PUT replaces the first stored mapping."""
    await _insert_user(tmp_path)
    _login(test_client)
    test_client.put(
        "/api/settings/preset-models",
        json={"general": {"provider": "lmstudio", "model_id": "old"}},
    )
    test_client.put(
        "/api/settings/preset-models",
        json={"coder": {"provider": "lmstudio", "model_id": "new"}},
    )
    resp = test_client.get("/api/settings/preset-models")
    data = resp.json()
    assert "coder" in data
    assert "general" not in data


@pytest.mark.asyncio
async def test_put_lmstudio_provider_always_accepted(
    tmp_path: Path, test_client: TestClient
) -> None:
    """provider=lmstudio is always accepted even without a registry."""
    await _insert_user(tmp_path)
    _login(test_client)
    mapping = {"analyst": {"provider": "lmstudio", "model_id": "phi-4"}}
    resp = test_client.put("/api/settings/preset-models", json=mapping)
    assert resp.status_code == 200, resp.text
    assert resp.json() == mapping
