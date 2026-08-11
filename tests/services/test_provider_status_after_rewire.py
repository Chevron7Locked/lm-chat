# SPDX-License-Identifier: Apache-2.0
"""Tests for ProviderRegistry + rewire_singletons interaction (sixth-singleton fix).

Without the fix, cloud providers built by ProviderRegistry.refresh() hold a
reference to the old httpx.AsyncClient.  After OLD_CLIENT_GRACE_SECONDS the
old client is closed and every /api/providers/status probe raises:
    RuntimeError: Cannot send a request, as the client has been closed.

reconfigure_http_client() rebinds the registry's own _http_client AND iterates
all cloud-provider instances, calling set_http_client() on each — matching the
pattern used for web_search_service.reconfigure().

rewire_singletons() now calls provider_registry.reconfigure_http_client() inside
the rewire_lock block (the sixth singleton re-point).

Covers:
- After rewire, the registry's cloud providers use the new client (not the old).
- Closing the old client does NOT raise "client has been closed" for the rebound provider.
- rewire_singletons calls provider_registry.reconfigure_http_client(new_client).
- getattr guard: app_state without provider_registry does not crash rewire_singletons.
"""
from __future__ import annotations

import asyncio
import tempfile
import types
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lmchat.providers.openai_compat import OpenAICompatProvider
from lmchat.services.provider_config_service import (
    ProviderConfigInternalView,
    ProviderConfigSafeView,
)
from lmchat.services.provider_registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_safe_view(
    provider: str,
    *,
    base_url: str = "https://api.example.com",
    enabled: bool = True,
) -> ProviderConfigSafeView:
    return ProviderConfigSafeView(
        provider=provider,
        base_url=base_url,
        default_model=None,
        extra_headers=None,
        enabled=enabled,
        api_key_set=True,
    )


def _make_internal_view(
    provider: str,
    *,
    base_url: str = "https://api.example.com",
    api_key: str | None = "test-key",
) -> ProviderConfigInternalView:
    return ProviderConfigInternalView(
        provider=provider,
        base_url=base_url,
        default_model=None,
        extra_headers=None,
        enabled=True,
        api_key=api_key,
        api_key_set=api_key is not None,
    )


def _make_registry_with_provider(
    provider_name: str = "openrouter",
    base_url: str = "https://openrouter.ai/api/v1",
    http_client: httpx.AsyncClient | None = None,
) -> tuple[ProviderRegistry, httpx.AsyncClient]:
    """Build a ProviderRegistry pre-seeded with one cloud provider."""
    client = http_client or MagicMock(spec=httpx.AsyncClient)

    stub_adapter = MagicMock()
    stub_adapter.name = "lmstudio"

    safe = _make_safe_view(provider_name, base_url=base_url)
    internal = _make_internal_view(provider_name, base_url=base_url)

    config_svc = MagicMock()
    config_svc.list_all = AsyncMock(return_value=[safe])

    async def _fake_get(p: str) -> ProviderConfigInternalView | None:
        return internal if p == provider_name else None

    config_svc.get = _fake_get

    reg = ProviderRegistry(
        lmstudio_provider=stub_adapter,
        config_service=config_svc,
        http_client=client,
    )
    return reg, client


# ---------------------------------------------------------------------------
# Tests: registry rebind after rewire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_uses_new_client_after_reconfigure() -> None:
    """After reconfigure_http_client(), the cloud provider uses the new client.

    This is the core regression test: before the fix, the provider retained the
    old client.  After the fix, it holds the new one.
    """
    old_client = MagicMock(spec=httpx.AsyncClient)
    reg, _ = _make_registry_with_provider(http_client=old_client)
    await reg.refresh()

    provider = reg.get("openrouter")
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._http_client is old_client, "pre-condition: provider holds old client"

    new_client = MagicMock(spec=httpx.AsyncClient)
    await reg.reconfigure_http_client(new_client)

    assert provider._http_client is new_client, (
        "provider still holds old client after reconfigure_http_client"
    )
    assert reg._http_client is new_client


@pytest.mark.asyncio
async def test_old_client_close_does_not_affect_rebound_provider() -> None:
    """After rebind + old client close, list_models_detailed does not raise 'closed'.

    Simulates the rewire flow: old client is closed after grace period;
    provider must use the new client and succeed.
    """
    # Use a real (but non-networked) httpx client for the close test.
    old_client = httpx.AsyncClient(timeout=5.0)
    reg, _ = _make_registry_with_provider(http_client=old_client)
    await reg.refresh()

    provider = reg.get("openrouter")
    assert isinstance(provider, OpenAICompatProvider)

    # Build a new client whose GET is mocked to return success.
    new_client = httpx.AsyncClient(timeout=5.0)
    new_client.get = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"data": [{"id": "gpt-4o"}]}),
        )
    )

    # Rebind (what rewire_singletons now does).
    await reg.reconfigure_http_client(new_client)

    # Close the OLD client (simulating OLD_CLIENT_GRACE_SECONDS expiry).
    await old_client.aclose()

    # Provider must use new client — no RuntimeError.
    items, status, error = await provider.list_models_detailed()
    assert error is None, f"list_models_detailed raised after rebind: {error}"
    assert status == 200
    assert items == [{"id": "gpt-4o"}]

    await new_client.aclose()


# ---------------------------------------------------------------------------
# Tests: rewire_singletons plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewire_singletons_calls_reconfigure_on_provider_registry() -> None:
    """rewire_singletons() calls provider_registry.reconfigure_http_client(new_client).

    Contract test: the mock registry attached to app.state must receive
    reconfigure_http_client() with the new httpx.AsyncClient after rewire.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from lmchat.config import Settings
    from lmchat.db.schema import metadata
    from lmchat.services.lm_studio_overrides_service import LmStudioOverridesService

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp}/pr_contract.db",
            pool_pre_ping=True,
        )
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        try:
            settings = Settings(  # type: ignore[call-arg]
                lm_studio_base_url="http://old.host:1234",
                lm_studio_api_key="old-key",
                lm_studio_default_model="model",
                lm_chat_secret="test-secret-32-bytes-of-entropy!!",
            )
            svc = LmStudioOverridesService(engine=engine, settings=settings)

            old_http = httpx.AsyncClient(base_url="http://old.host:1234")

            models_svc = MagicMock()
            models_svc._http_client = old_http
            models_svc._base_url = "http://old.host:1234"
            models_svc._cache = None
            models_svc._cache_lock = asyncio.Lock()

            embed = MagicMock()
            embed._http = old_http
            embed._base_url = "http://old.host:1234"
            embed._endpoint = "http://old.host:1234/api/v1/embeddings"

            adapter = MagicMock()
            adapter._http_client = old_http
            adapter._base_url = "http://old.host:1234"

            web_svc_mock = MagicMock()
            web_svc_mock.reconfigure = MagicMock()

            registry_mock = MagicMock()
            registry_mock.reconfigure_http_client = AsyncMock()

            state = types.SimpleNamespace(
                http_client=old_http,
                http=old_http,
                lmstudio_adapter=adapter,
                models_service=models_svc,
                embedding_client=embed,
                web_search_service=web_svc_mock,
                provider_registry=registry_mock,
                rewire_lock=asyncio.Lock(),
            )

            with patch(
                "lmchat.services.lm_studio_overrides_service._delayed_close",
                new=AsyncMock(),
            ):
                await svc.rewire_singletons(
                    state,
                    new_base_url="http://new.host:5678",
                    new_api_key="new-key",
                )

            # Key assertion: reconfigure_http_client was called with the new client.
            new_http_client = state.http_client
            registry_mock.reconfigure_http_client.assert_called_once_with(new_http_client)

            await old_http.aclose()
            await new_http_client.aclose()
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_rewire_singletons_no_crash_without_provider_registry() -> None:
    """rewire_singletons does not crash when app_state lacks provider_registry.

    Unit-test stubs and early lifespan states may not set provider_registry.
    The getattr guard must make this a no-op rather than raising AttributeError.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from lmchat.config import Settings
    from lmchat.db.schema import metadata
    from lmchat.services.lm_studio_overrides_service import LmStudioOverridesService

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp}/pr_noattr.db",
            pool_pre_ping=True,
        )
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        try:
            settings = Settings(  # type: ignore[call-arg]
                lm_studio_base_url="http://old.host:1234",
                lm_studio_api_key="old-key",
                lm_studio_default_model="model",
                lm_chat_secret="test-secret-32-bytes-of-entropy!!",
            )
            svc = LmStudioOverridesService(engine=engine, settings=settings)

            old_http = httpx.AsyncClient(base_url="http://old.host:1234")

            models_svc = MagicMock()
            models_svc._http_client = old_http
            models_svc._base_url = "http://old.host:1234"
            models_svc._cache = None
            models_svc._cache_lock = asyncio.Lock()

            embed = MagicMock()
            embed._http = old_http
            embed._base_url = "http://old.host:1234"
            embed._endpoint = "http://old.host:1234/api/v1/embeddings"

            adapter = MagicMock()
            adapter._http_client = old_http
            adapter._base_url = "http://old.host:1234"

            web_svc_mock = MagicMock()
            web_svc_mock.reconfigure = MagicMock()

            # Deliberately omit provider_registry from state.
            state = types.SimpleNamespace(
                http_client=old_http,
                http=old_http,
                lmstudio_adapter=adapter,
                models_service=models_svc,
                embedding_client=embed,
                web_search_service=web_svc_mock,
                rewire_lock=asyncio.Lock(),
            )

            with patch(
                "lmchat.services.lm_studio_overrides_service._delayed_close",
                new=AsyncMock(),
            ):
                # Must not raise AttributeError or any other exception.
                await svc.rewire_singletons(
                    state,
                    new_base_url="http://new.host:5678",
                    new_api_key="new-key",
                )

            new_http_client = state.http_client
            await old_http.aclose()
            await new_http_client.aclose()
        finally:
            await engine.dispose()
