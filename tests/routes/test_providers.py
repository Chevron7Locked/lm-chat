# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the admin provider-config routes (Workstream A4).

Covers:
- GET /api/admin/providers: 401 unauthenticated; 403 non-admin; 200 admin.
- PUT /api/admin/providers/{provider}: 401/403 gates; 200 upsert with registry
  refresh; safe view returned (no api_key cleartext).
- DELETE /api/admin/providers/{provider}: 401/403 gates; 204 success.
- POST /api/admin/providers/{provider}/test: 401/403 gates; 200 ok=True when
  list_models returns ids; 200 ok=False when stored config missing + no body
  base_url; ok=False on probe error.

Tests use a real SQLite DB + full lifespan (same pattern as
test_lm_studio_settings.py), with the provider_registry and
provider_config_service mocked on app.state to keep tests fast and isolated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_models_service_dep
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.provider_config_service import (
    ProviderConfigInternalView,
    ProviderConfigSafeView,
)
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


# ---------------------------------------------------------------------------
# App / DB helpers (mirrors test_lm_studio_settings pattern)
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/providers_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://env.example:1234")
    monkeypatch.setenv("LM_STUDIO_API_KEY", "env-api-key")
    monkeypatch.setenv("LM_STUDIO_DEFAULT_MODEL", "env-model")

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
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()


@pytest.fixture()
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/providers_route_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(
    tmp_path: Path,
    username: str,
    is_admin: bool = False,
) -> int:
    pw_hash = hash_password("test-pw", n=_LOW_N, r=8, p=1)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO users (username, password_hash, is_admin) "
                    "VALUES (:u, :pw, :admin)"
                ),
                {"u": username, "pw": pw_hash, "admin": 1 if is_admin else 0},
            )
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE username = :u"), {"u": username}
                )
            ).fetchone()
            return int(row[0])  # type: ignore[index]
    finally:
        await eng.dispose()


def _login(client: TestClient, username: str, password: str = "test-pw") -> None:
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Helpers to inject mock services into app.state
# ---------------------------------------------------------------------------


def _install_mock_registry(client: TestClient) -> MagicMock:
    """Install a mock ProviderRegistry on app.state; return it."""
    reg = MagicMock()
    reg.refresh = AsyncMock(return_value=None)
    client.app.state.provider_registry = reg  # type: ignore[attr-defined]
    return reg


def _install_mock_config_svc(
    client: TestClient,
    *,
    list_all_return: list[ProviderConfigSafeView] | None = None,
    get_return: ProviderConfigInternalView | None = None,
) -> MagicMock:
    """Install a mock ProviderConfigService on app.state; return it."""
    svc = MagicMock()
    svc.list_all = AsyncMock(return_value=list_all_return or [])
    svc.get = AsyncMock(return_value=get_return)
    svc.add_or_update = AsyncMock(return_value=None)
    svc.delete = AsyncMock(return_value=None)
    client.app.state.provider_config_service = svc  # type: ignore[attr-defined]
    return svc


def _make_safe_view(provider: str = "openai") -> ProviderConfigSafeView:
    return ProviderConfigSafeView(
        provider=provider,
        base_url="https://api.openai.com",
        default_model=None,
        extra_headers=None,
        enabled=True,
        api_key_set=True,
    )


def _make_internal_view(provider: str = "openai") -> ProviderConfigInternalView:
    return ProviderConfigInternalView(
        provider=provider,
        base_url="https://api.openai.com",
        default_model=None,
        extra_headers=None,
        enabled=True,
        api_key=None,
        api_key_set=False,
    )


# ---------------------------------------------------------------------------
# GET /api/admin/providers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_providers_requires_auth(test_client: TestClient) -> None:
    """Unauthenticated GET returns 401."""
    resp = test_client.get("/api/admin/providers")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_providers_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Non-admin authenticated GET returns 403."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")
    resp = test_client.get("/api/admin/providers")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_providers_returns_safe_views(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Admin GET returns safe views; api_key cleartext never returned."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    safe = _make_safe_view("openai")
    _install_mock_config_svc(test_client, list_all_return=[safe])
    _install_mock_registry(test_client)

    resp = test_client.get("/api/admin/providers")
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["provider"] == "openai"
    assert data[0]["base_url"] == "https://api.openai.com"
    assert "api_key" not in data[0]        # cleartext never returned
    assert "api_key_set" in data[0]        # boolean flag present


@pytest.mark.asyncio
async def test_list_providers_empty(tmp_path: Path, test_client: TestClient) -> None:
    """Admin GET with no DB rows returns empty list."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    _install_mock_config_svc(test_client, list_all_return=[])
    _install_mock_registry(test_client)

    resp = test_client.get("/api/admin/providers")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ---------------------------------------------------------------------------
# PUT /api/admin/providers/{provider}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_provider_requires_auth(test_client: TestClient) -> None:
    """Unauthenticated PUT returns 401."""
    resp = test_client.put(
        "/api/admin/providers/openai",
        json={"base_url": "https://api.openai.com"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_provider_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Non-admin PUT returns 403."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")

    resp = test_client.put(
        "/api/admin/providers/openai",
        json={"base_url": "https://api.openai.com"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_provider_upsert_and_refresh(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Admin PUT calls add_or_update + registry.refresh(), returns safe view."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    safe = _make_safe_view("openai")
    svc = _install_mock_config_svc(test_client, list_all_return=[safe])
    reg = _install_mock_registry(test_client)

    resp = test_client.put(
        "/api/admin/providers/openai",
        json={
            "base_url": "https://api.openai.com",
            "api_key": "sk-secret",
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provider"] == "openai"
    assert "api_key" not in data

    # Verify service + registry were called.
    svc.add_or_update.assert_awaited_once()
    reg.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_provider_rejects_non_http_url(
    tmp_path: Path, test_client: TestClient
) -> None:
    """file:// URL rejected with 422."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    _install_mock_config_svc(test_client)
    _install_mock_registry(test_client)

    resp = test_client.put(
        "/api/admin/providers/bad",
        json={"base_url": "file:///etc/passwd"},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# DELETE /api/admin/providers/{provider}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_provider_requires_auth(test_client: TestClient) -> None:
    """Unauthenticated DELETE returns 401."""
    resp = test_client.delete("/api/admin/providers/openai")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_provider_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Non-admin DELETE returns 403."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")

    resp = test_client.delete("/api/admin/providers/openai")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_provider_calls_service_and_refresh(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Admin DELETE calls svc.delete + registry.refresh(), returns 204."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    svc = _install_mock_config_svc(test_client)
    reg = _install_mock_registry(test_client)

    resp = test_client.delete("/api/admin/providers/openai")
    assert resp.status_code == 204, resp.text

    svc.delete.assert_awaited_once_with("openai")
    reg.refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /api/admin/providers/{provider}/test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_provider_requires_auth(test_client: TestClient) -> None:
    """Unauthenticated POST test returns 401."""
    resp = test_client.post(
        "/api/admin/providers/openai/test",
        json={"base_url": "https://api.openai.com"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_test_provider_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Non-admin POST test returns 403."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")

    resp = test_client.post(
        "/api/admin/providers/openai/test",
        json={"base_url": "https://api.openai.com"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_test_provider_ok_with_body_base_url(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /test with base_url in body → list_models_detailed called → ok=True."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    _install_mock_config_svc(test_client)
    _install_mock_registry(test_client)

    # Patch list_models_detailed at the class level — this is the SAME method
    # the live model_catalog fetch calls, so /test now exercises the
    # identical success/failure contract as the live picker.
    with patch(
        "lmchat.providers.openai_compat.OpenAICompatProvider.list_models_detailed",
        new_callable=AsyncMock,
        return_value=([{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}], 200, None),
    ):
        resp = test_client.post(
            "/api/admin/providers/openai/test",
            json={"base_url": "https://api.openai.com", "api_key": "sk-test"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["model_count"] == 2


@pytest.mark.asyncio
async def test_test_provider_ok_falls_back_to_stored_config(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /test with no body → reads stored config → ok=True."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    internal = _make_internal_view("openai")
    # Override api_key to avoid None in the test
    internal = ProviderConfigInternalView(
        provider="openai",
        base_url="https://api.openai.com",
        default_model=None,
        extra_headers=None,
        enabled=True,
        api_key="sk-stored",
        api_key_set=True,
    )
    _install_mock_config_svc(test_client, get_return=internal)
    _install_mock_registry(test_client)

    with patch(
        "lmchat.providers.openai_compat.OpenAICompatProvider.list_models_detailed",
        new_callable=AsyncMock,
        return_value=([{"id": "m1"}], 200, None),
    ):
        resp = test_client.post(
            "/api/admin/providers/openai/test",
            json={},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["model_count"] == 1


@pytest.mark.asyncio
async def test_test_provider_400_when_no_base_url_and_no_stored(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /test with no base_url and no stored config → 400."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    # get() returns None (no stored config)
    _install_mock_config_svc(test_client, get_return=None)
    _install_mock_registry(test_client)

    resp = test_client.post(
        "/api/admin/providers/newprovider/test",
        json={},
    )
    assert resp.status_code == 400, resp.text
    assert "No stored config" in resp.text or "nothing to probe" in resp.text.lower()


@pytest.mark.asyncio
async def test_test_provider_ok_false_on_list_models_empty(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /test with a genuinely empty (but well-formed) model list →
    ok=True, model_count=0 — this is NOT an error, just zero enabled models."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    _install_mock_config_svc(test_client)
    _install_mock_registry(test_client)

    with patch(
        "lmchat.providers.openai_compat.OpenAICompatProvider.list_models_detailed",
        new_callable=AsyncMock,
        return_value=([], 200, None),
    ):
        resp = test_client.post(
            "/api/admin/providers/openai/test",
            json={"base_url": "https://api.openai.com"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    # A well-formed 200 with an empty data array is not an error — it means
    # 0 models available, distinct from a non-200 / malformed response.
    assert data["ok"] is True
    assert data["model_count"] == 0


# ---------------------------------------------------------------------------
# allowed_models — PUT carries it; safe view returns it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_provider_with_allowed_models(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT with allowed_models passes it to add_or_update; safe view returns it."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    safe = ProviderConfigSafeView(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        default_model=None,
        extra_headers=None,
        enabled=True,
        api_key_set=False,
        allowed_models=["openai/gpt-4o", "meta-llama/llama-3.3-70b"],
    )
    svc = _install_mock_config_svc(test_client, list_all_return=[safe])
    _install_mock_registry(test_client)

    resp = test_client.put(
        "/api/admin/providers/openrouter",
        json={
            "base_url": "https://openrouter.ai/api/v1",
            "allowed_models": ["openai/gpt-4o", "meta-llama/llama-3.3-70b"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["allowed_models"] == ["openai/gpt-4o", "meta-llama/llama-3.3-70b"]

    # Verify allowed_models was passed through to the service
    call_kwargs = svc.add_or_update.call_args.kwargs
    assert call_kwargs.get("allowed_models") == ["openai/gpt-4o", "meta-llama/llama-3.3-70b"]


@pytest.mark.asyncio
async def test_put_provider_allowed_models_null_when_omitted(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT without allowed_models → safe view returns allowed_models=None (not present or null)."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    safe = _make_safe_view("openai")  # allowed_models=None by default
    _install_mock_config_svc(test_client, list_all_return=[safe])
    _install_mock_registry(test_client)

    resp = test_client.put(
        "/api/admin/providers/openai",
        json={"base_url": "https://api.openai.com"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("allowed_models") is None


# ---------------------------------------------------------------------------
# /test returns model_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_provider_returns_model_ids(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /test returns model_ids list alongside model_count."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    _install_mock_config_svc(test_client)
    _install_mock_registry(test_client)

    with patch(
        "lmchat.providers.openai_compat.OpenAICompatProvider.list_models_detailed",
        new_callable=AsyncMock,
        return_value=(
            [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": "o1"}],
            200,
            None,
        ),
    ):
        resp = test_client.post(
            "/api/admin/providers/openai/test",
            json={"base_url": "https://api.openai.com", "api_key": "sk-test"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["model_count"] == 3
    assert data["model_ids"] == ["gpt-4o", "gpt-4o-mini", "o1"]


@pytest.mark.asyncio
async def test_test_provider_model_ids_none_on_failure(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /test with a probe exception returns model_ids=None."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    _install_mock_config_svc(test_client)
    _install_mock_registry(test_client)

    with patch(
        "lmchat.providers.openai_compat.OpenAICompatProvider.list_models_detailed",
        new_callable=AsyncMock,
        side_effect=Exception("Connection refused"),
    ):
        resp = test_client.post(
            "/api/admin/providers/openai/test",
            json={"base_url": "https://api.openai.com"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is False
    assert data["model_ids"] is None


@pytest.mark.asyncio
async def test_put_provider_without_api_key_preserves_stored_key(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT an existing provider without api_key in the body → api_key_set stays True.

    Verifies that add_or_update is called with api_key=None (omitted from body)
    and that the safe view returned still shows api_key_set=True.
    """
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    # Simulate a provider that already has a key (api_key_set=True).
    safe = _make_safe_view("openai")  # api_key_set=True by default
    svc = _install_mock_config_svc(test_client, list_all_return=[safe])
    _install_mock_registry(test_client)

    # PUT body deliberately omits api_key — only changes default_model.
    resp = test_client.put(
        "/api/admin/providers/openai",
        json={
            "base_url": "https://api.openai.com",
            "default_model": "gpt-4o",
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # The response reflects the stored safe view (api_key_set still True).
    assert data["api_key_set"] is True
    assert "api_key" not in data

    # The service was called with api_key=None (not with an empty string).
    call_kwargs = svc.add_or_update.await_args.kwargs
    assert call_kwargs.get("api_key") is None


# ---------------------------------------------------------------------------
# /test reflects the SAME live URL the model_catalog fetch would hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_provider_reflects_live_url(
    tmp_path: Path, test_client: TestClient
) -> None:
    """A green /test must mean the live fetch would ALSO succeed.

    Exercises the real OpenAICompatProvider.list_models_detailed HTTP path
    (patching httpx.AsyncClient.get itself, not the method), so this proves
    actual URL convergence rather than just that /test calls the right
    method name.  ``https://openrouter.ai/api/v1`` (OpenRouter's own
    documented base_url) and ``https://openrouter.ai/api`` (the form
    without the redundant /v1) must both normalize to the same canonical
    ``.../api/v1/models`` probe URL and report ok:true; a base_url that
    would still 404 live must NOT report ok:true.
    """
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    _install_mock_config_svc(test_client)
    _install_mock_registry(test_client)

    canonical_url = "https://openrouter.ai/api/v1/models"

    async def _fake_get(
        self, url, *, headers=None, **kwargs
    ):  # noqa: ANN001, ARG001, ANN201
        if url == canonical_url:
            return httpx.Response(
                200, json={"data": [{"id": "openai/gpt-4o-mini"}]}
            )
        return httpx.Response(404, json={"error": "not found"})

    with patch("httpx.AsyncClient.get", new=_fake_get):
        # Saved with the trailing /v1 OpenRouter documents — pre-fix this
        # doubled to ".../api/v1/v1/models" and 404'd live.
        resp = test_client.post(
            "/api/admin/providers/openrouter/test",
            json={"base_url": "https://openrouter.ai/api/v1", "api_key": "or-key"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True, data
        assert data["model_count"] == 1

        # The equivalent form without the /v1 suffix converges on the same
        # canonical URL — also ok:true.
        resp2 = test_client.post(
            "/api/admin/providers/openrouter/test",
            json={"base_url": "https://openrouter.ai/api", "api_key": "or-key"},
        )
        assert resp2.status_code == 200, resp2.text
        data2 = resp2.json()
        assert data2["ok"] is True, data2
        assert data2["model_count"] == 1

        # A genuinely different (wrong) URL still 404s — must NOT report ok:true.
        resp3 = test_client.post(
            "/api/admin/providers/openrouter/test",
            json={"base_url": "https://openrouter.ai/wrong-path", "api_key": "or-key"},
        )
        assert resp3.status_code == 200, resp3.text
        data3 = resp3.json()
        assert data3["ok"] is False, data3
