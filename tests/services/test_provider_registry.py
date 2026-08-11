# SPDX-License-Identifier: Apache-2.0
"""Tests for ProviderRegistry — Workstream A4.

Covers:
- Construction seeds the "lmstudio" entry from the injected adapter.
- names() returns sorted list including "lmstudio".
- get() returns the registered provider; None for unknown name.
- refresh(): enabled DB rows → OpenAICompatProvider added; disabled rows skipped.
- refresh(): row that vanishes between list_all and get is skipped (no crash).
- refresh(): construction failure (bad base_url type) is skipped (no crash).
- refresh(): replaces old entries on second call (stale provider removed).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

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
    api_key_set: bool = True,
) -> ProviderConfigSafeView:
    return ProviderConfigSafeView(
        provider=provider,
        base_url=base_url,
        default_model=None,
        extra_headers=None,
        enabled=enabled,
        api_key_set=api_key_set,
    )


def _make_internal_view(
    provider: str,
    *,
    base_url: str = "https://api.example.com",
    enabled: bool = True,
    api_key: str | None = "test-key",
) -> ProviderConfigInternalView:
    return ProviderConfigInternalView(
        provider=provider,
        base_url=base_url,
        default_model=None,
        extra_headers=None,
        enabled=enabled,
        api_key=api_key,
        api_key_set=api_key is not None,
    )


def _make_registry(
    list_all_return: list[ProviderConfigSafeView] | None = None,
    get_return_map: dict[str, ProviderConfigInternalView | None] | None = None,
) -> ProviderRegistry:
    """Build a ProviderRegistry with a mocked ProviderConfigService."""
    stub_adapter = MagicMock()
    stub_adapter.name = "lmstudio"

    config_svc = MagicMock()
    config_svc.list_all = AsyncMock(return_value=list_all_return or [])

    get_map = get_return_map or {}

    async def _fake_get(provider: str) -> ProviderConfigInternalView | None:
        return get_map.get(provider)

    config_svc.get = _fake_get

    http_client = MagicMock(spec=httpx.AsyncClient)

    return ProviderRegistry(
        lmstudio_provider=stub_adapter,
        config_service=config_svc,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# Tests: construction
# ---------------------------------------------------------------------------


def test_construction_seeds_lmstudio() -> None:
    """Registry is pre-seeded with the 'lmstudio' entry at construction."""
    reg = _make_registry()
    assert "lmstudio" in reg.names()
    provider = reg.get("lmstudio")
    assert provider is not None
    assert getattr(provider, "name", None) == "lmstudio"


def test_get_unknown_returns_none() -> None:
    """get() returns None for an unregistered name."""
    reg = _make_registry()
    assert reg.get("unknown-provider") is None


def test_names_sorted() -> None:
    """names() returns a sorted list."""
    reg = _make_registry()
    result = reg.names()
    assert result == sorted(result)
    assert "lmstudio" in result


# ---------------------------------------------------------------------------
# Tests: refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_adds_enabled_provider() -> None:
    """refresh() builds an OpenAICompatProvider for each enabled DB row."""
    from lmchat.providers.openai_compat import OpenAICompatProvider

    safe = _make_safe_view("openai", base_url="https://api.openai.com")
    internal = _make_internal_view("openai", base_url="https://api.openai.com", api_key="sk-test")
    reg = _make_registry(
        list_all_return=[safe],
        get_return_map={"openai": internal},
    )

    await reg.refresh()

    assert "openai" in reg.names()
    provider = reg.get("openai")
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.name == "openai"


@pytest.mark.asyncio
async def test_refresh_skips_disabled_provider() -> None:
    """refresh() does not add providers with enabled=False."""
    safe = _make_safe_view("groq", enabled=False)
    reg = _make_registry(list_all_return=[safe])

    await reg.refresh()

    assert "groq" not in reg.names()


@pytest.mark.asyncio
async def test_refresh_preserves_lmstudio() -> None:
    """lmstudio entry is always present after refresh(), even with no DB rows."""
    reg = _make_registry(list_all_return=[])

    await reg.refresh()

    assert "lmstudio" in reg.names()
    assert reg.get("lmstudio") is not None


@pytest.mark.asyncio
async def test_refresh_row_vanished_skipped() -> None:
    """If get() returns None for a row that list_all returned, skip gracefully."""
    safe = _make_safe_view("openrouter")
    reg = _make_registry(
        list_all_return=[safe],
        get_return_map={"openrouter": None},  # row vanished
    )

    # Must not raise.
    await reg.refresh()
    assert "openrouter" not in reg.names()


@pytest.mark.asyncio
async def test_refresh_removes_stale_provider() -> None:
    """Second refresh with empty DB removes a previously registered cloud provider."""
    safe = _make_safe_view("openai", base_url="https://api.openai.com")
    internal = _make_internal_view("openai", base_url="https://api.openai.com")

    config_svc = MagicMock()
    # First call returns one row; second returns empty.
    config_svc.list_all = AsyncMock(side_effect=[
        [safe],
        [],
    ])
    config_svc.get = AsyncMock(return_value=internal)

    stub_adapter = MagicMock()
    stub_adapter.name = "lmstudio"
    http_client = MagicMock(spec=httpx.AsyncClient)

    reg = ProviderRegistry(
        lmstudio_provider=stub_adapter,
        config_service=config_svc,
        http_client=http_client,
    )

    await reg.refresh()
    assert "openai" in reg.names()

    await reg.refresh()
    assert "openai" not in reg.names()
    assert "lmstudio" in reg.names()


@pytest.mark.asyncio
async def test_refresh_get_exception_skipped() -> None:
    """If config_svc.get() raises, the provider is skipped without crashing the registry."""
    safe = _make_safe_view("badprovider")

    config_svc = MagicMock()
    config_svc.list_all = AsyncMock(return_value=[safe])
    config_svc.get = AsyncMock(side_effect=RuntimeError("DB exploded"))

    stub_adapter = MagicMock()
    stub_adapter.name = "lmstudio"
    http_client = MagicMock(spec=httpx.AsyncClient)

    reg = ProviderRegistry(
        lmstudio_provider=stub_adapter,
        config_service=config_svc,
        http_client=http_client,
    )

    # Must not raise.
    await reg.refresh()
    assert "badprovider" not in reg.names()
    assert "lmstudio" in reg.names()


@pytest.mark.asyncio
async def test_refresh_concurrent_calls_are_serialised() -> None:
    """Two concurrent refresh() calls do not produce a torn self._providers write.

    Both calls complete successfully; the final state must be consistent
    (lmstudio always present; the provider built from the enabled row is
    present when at least one refresh saw it).  The key assertion is that no
    exception is raised and the registry is not in an inconsistent state.
    """
    import asyncio as _asyncio

    safe = _make_safe_view("openrouter", base_url="https://openrouter.ai/api/v1")
    internal = _make_internal_view("openrouter", base_url="https://openrouter.ai/api/v1")

    config_svc = MagicMock()
    config_svc.list_all = AsyncMock(return_value=[safe])
    config_svc.get = AsyncMock(return_value=internal)

    stub_adapter = MagicMock()
    stub_adapter.name = "lmstudio"
    http_client = MagicMock(spec=httpx.AsyncClient)

    reg = ProviderRegistry(
        lmstudio_provider=stub_adapter,
        config_service=config_svc,
        http_client=http_client,
    )

    # Fire two refresh() coroutines concurrently.
    await _asyncio.gather(reg.refresh(), reg.refresh())

    # Registry must be consistent: lmstudio always present.
    assert "lmstudio" in reg.names()
    # The enabled provider must be registered (both refreshes saw the same DB).
    assert "openrouter" in reg.names()
    # get() must not return None for registered names.
    assert reg.get("lmstudio") is not None
    assert reg.get("openrouter") is not None


# ---------------------------------------------------------------------------
# Tests: reconfigure_http_client (sixth-singleton rewire fix)
# ---------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_reconfigure_http_client_rebinds_registry_client() -> None:
    """reconfigure_http_client() updates self._http_client on the registry."""
    reg = _make_registry()
    new_client = MagicMock(spec=httpx.AsyncClient)
    await reg.reconfigure_http_client(new_client)
    assert reg._http_client is new_client


@pytest.mark.asyncio
async def test_reconfigure_http_client_rebinds_cloud_providers() -> None:
    """reconfigure_http_client() calls set_http_client() on all cloud providers."""
    from lmchat.providers.openai_compat import OpenAICompatProvider

    safe = _make_safe_view("openrouter", base_url="https://openrouter.ai/api/v1")
    internal = _make_internal_view(
        "openrouter", base_url="https://openrouter.ai/api/v1", api_key="test-key"
    )
    reg = _make_registry(
        list_all_return=[safe],
        get_return_map={"openrouter": internal},
    )
    old_client = reg._http_client
    await reg.refresh()

    provider = reg.get("openrouter")
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._http_client is old_client

    new_client = MagicMock(spec=httpx.AsyncClient)
    await reg.reconfigure_http_client(new_client)

    # Registry-level rebind.
    assert reg._http_client is new_client
    # Provider-level rebind.
    assert provider._http_client is new_client, (
        "reconfigure_http_client did not rebind _http_client on OpenAICompatProvider"
    )


@pytest.mark.asyncio
async def test_reconfigure_http_client_skips_lmstudio_anchor() -> None:
    """reconfigure_http_client() does NOT call set_http_client on the lmstudio anchor."""
    stub_adapter = MagicMock()
    stub_adapter.name = "lmstudio"

    config_svc = MagicMock()
    config_svc.list_all = AsyncMock(return_value=[])

    http_client = MagicMock(spec=httpx.AsyncClient)
    from lmchat.services.provider_registry import ProviderRegistry

    reg = ProviderRegistry(
        lmstudio_provider=stub_adapter,
        config_service=config_svc,
        http_client=http_client,
    )

    new_client = MagicMock(spec=httpx.AsyncClient)
    await reg.reconfigure_http_client(new_client)

    # The lmstudio adapter must NOT have set_http_client called on it.
    stub_adapter.set_http_client.assert_not_called()


@pytest.mark.asyncio
async def test_reconfigure_http_client_multiple_cloud_providers() -> None:
    """reconfigure_http_client() rebinds every cloud provider, not just the first."""
    from lmchat.providers.openai_compat import OpenAICompatProvider

    safe_or = _make_safe_view("openrouter", base_url="https://openrouter.ai/api/v1")
    safe_oa = _make_safe_view("openai", base_url="https://api.openai.com")
    int_or = _make_internal_view(
        "openrouter", base_url="https://openrouter.ai/api/v1", api_key="key-or"
    )
    int_oa = _make_internal_view(
        "openai", base_url="https://api.openai.com", api_key="key-oa"
    )

    reg = _make_registry(
        list_all_return=[safe_or, safe_oa],
        get_return_map={"openrouter": int_or, "openai": int_oa},
    )
    await reg.refresh()

    prov_or = reg.get("openrouter")
    prov_oa = reg.get("openai")
    assert isinstance(prov_or, OpenAICompatProvider)
    assert isinstance(prov_oa, OpenAICompatProvider)

    new_client = MagicMock(spec=httpx.AsyncClient)
    await reg.reconfigure_http_client(new_client)

    assert prov_or._http_client is new_client, "openrouter provider not rebound"
    assert prov_oa._http_client is new_client, "openai provider not rebound"


# ---------------------------------------------------------------------------
# Regression: TOCTOU race — concurrent refresh + reconfigure_http_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_does_not_race_with_concurrent_refresh() -> None:
    """Concurrent refresh() + reconfigure_http_client() leaves every cloud
    provider on the NEW client — not the old one.

    Reproduces the TOCTOU race from commit 546efae where reconfigure_http_client
    was sync and took no lock, so a concurrent refresh() could build providers
    reading the OLD self._http_client just before reconfigure updated it, then
    swap them into self._providers *after* reconfigure's rebind loop had already
    run — leaving those new providers permanently on the soon-to-be-closed old
    client.

    The fix serialises both operations under _refresh_lock so whichever runs last
    leaves the registry in a consistent state.

    Test strategy:
    - Gate list_all() on an asyncio.Event so refresh() suspends mid-flight while
      holding _refresh_lock.
    - Concurrently launch reconfigure_http_client(new_client); it must block on
      _refresh_lock and not proceed until the event is released.
    - Release the gate; await both coroutines.
    - Assert every cloud provider in registry._providers holds new_client (none
      left on old_client), and registry._http_client is new_client.

    This test FAILS against the old sync/no-lock version of
    reconfigure_http_client and PASSES with the fix.
    """
    import asyncio as _asyncio

    from lmchat.providers.openai_compat import OpenAICompatProvider
    from lmchat.services.provider_config_service import (
        ProviderConfigInternalView,
        ProviderConfigSafeView,
    )
    from lmchat.services.provider_registry import ProviderRegistry

    old_client = MagicMock(spec=httpx.AsyncClient)
    new_client = MagicMock(spec=httpx.AsyncClient)

    # Gate that makes list_all() (and therefore refresh()) suspend
    # mid-flight while still holding _refresh_lock.
    gate = _asyncio.Event()

    safe = ProviderConfigSafeView(
        provider="openai",
        base_url="https://api.openai.com",
        default_model=None,
        extra_headers=None,
        enabled=True,
        api_key_set=True,
    )
    internal = ProviderConfigInternalView(
        provider="openai",
        base_url="https://api.openai.com",
        default_model=None,
        extra_headers=None,
        enabled=True,
        api_key=None,
        api_key_set=False,
    )

    async def _gated_list_all() -> list[ProviderConfigSafeView]:
        # Suspend here so refresh() is paused holding _refresh_lock,
        # giving reconfigure_http_client() a chance to race on the lock.
        await gate.wait()
        return [safe]

    config_svc = MagicMock()
    config_svc.list_all = _gated_list_all
    config_svc.get = AsyncMock(return_value=internal)

    stub_adapter = MagicMock()
    stub_adapter.name = "lmstudio"

    reg = ProviderRegistry(
        lmstudio_provider=stub_adapter,
        config_service=config_svc,
        http_client=old_client,
    )

    # Launch refresh() first — it will park at gate.wait() holding _refresh_lock.
    refresh_task = _asyncio.create_task(reg.refresh())

    # Yield control so refresh_task actually starts and parks at gate.wait().
    await _asyncio.sleep(0)

    # Launch reconfigure_http_client(new_client) — with the fix it must block
    # on _refresh_lock and cannot proceed until refresh() exits.
    reconfigure_task = _asyncio.create_task(
        reg.reconfigure_http_client(new_client)
    )

    # Yield once more so reconfigure_task starts and blocks on the lock.
    await _asyncio.sleep(0)

    # Release the gate — refresh() resumes, completes, releases _refresh_lock;
    # reconfigure_http_client() then acquires it and runs.
    gate.set()

    await _asyncio.gather(refresh_task, reconfigure_task)

    # --- Assertions ---

    # 1. The registry's own _http_client must be the new one.
    assert reg._http_client is new_client, (
        "registry._http_client was not updated to new_client"
    )

    # 2. Every cloud provider in _providers must hold new_client — none left
    #    on old_client.  This is the key invariant the race could violate.
    for name, provider in reg._providers.items():
        if name == "lmstudio":
            continue
        assert isinstance(provider, OpenAICompatProvider), (
            f"expected OpenAICompatProvider for {name!r}"
        )
        assert provider._http_client is new_client, (
            f"provider {name!r} still holds old_client after concurrent "
            "refresh + reconfigure_http_client — TOCTOU race not fixed"
        )
