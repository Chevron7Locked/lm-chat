# SPDX-License-Identifier: Apache-2.0
"""Tests for ModelCatalogService — W1 catalog merge.

Covers:
- ModelInfo.provider field present + defaults to "lmstudio".
- LM-Studio-only path unchanged when no cloud providers configured (regression).
- Merge: with a stub cloud provider, /api/models includes its models with
  provider=<slug> + mapped capabilities; same-id models across providers stay
  distinct.
- Unreachable provider: list_merged still returns LM Studio + reachable ones;
  provider_status reports unreachable.
- Cache: second call within TTL does not re-hit the provider; auth failure
  invalidates immediately.
- _map_capabilities: vision from architecture.input_modalities, tool_use from
  supported_parameters, reasoning from supported_parameters. Absent (or
  non-list) supported_parameters defaults trained_for_tool_use to True —
  only an explicit list lacking "tools"/"tool_choice" forces False.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lmchat.services.model_catalog import (
    ModelCatalogService,
    _map_capabilities,
    _map_model_info,
)
from lmchat.services.models_service import Capabilities, ModelInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model_info(key: str = "qwen3-local", provider: str = "lmstudio") -> ModelInfo:
    return ModelInfo(
        key=key,
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        provider=provider,
    )


def _make_registry(
    names: list[str] | None = None,
    providers: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock ProviderRegistry."""
    reg = MagicMock()
    _names = names or ["lmstudio"]
    reg.names = MagicMock(return_value=_names)
    _providers = providers or {}

    def _get(name: str) -> Any:
        return _providers.get(name)

    reg.get = MagicMock(side_effect=_get)
    return reg


def _make_models_svc(models: list[ModelInfo] | None = None) -> MagicMock:
    svc = MagicMock()
    svc.list_loaded = AsyncMock(return_value=models or [])
    return svc


def _make_compat_provider(
    *,
    items: list[dict[str, Any]] | None = None,
    http_status: int | None = 200,
    error: str | None = None,
) -> MagicMock:
    """Build a mock OpenAICompatProvider with list_models_detailed stubbed."""
    _spec = ["name", "list_models", "list_models_detailed", "stream_chat", "context_mode"]
    prov = MagicMock(spec=_spec)
    prov.list_models_detailed = AsyncMock(
        return_value=(items or [], http_status, error)
    )
    return prov


# ---------------------------------------------------------------------------
# ModelInfo.provider field
# ---------------------------------------------------------------------------


class TestModelInfoProvider:
    def test_default_provider_is_lmstudio(self) -> None:
        """ModelInfo.provider defaults to 'lmstudio' — regression guard."""
        m = ModelInfo(
            key="some-model",
            capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        )
        assert m.provider == "lmstudio"

    def test_provider_explicit(self) -> None:
        """ModelInfo.provider carries the provided value."""
        m = ModelInfo(
            key="gpt-4o",
            capabilities=Capabilities(vision=True, trained_for_tool_use=True),
            provider="openrouter",
        )
        assert m.provider == "openrouter"

    def test_provider_in_model_dump(self) -> None:
        """provider appears in model_dump() output."""
        m = ModelInfo(key="k", capabilities=None)
        d = m.model_dump()
        assert "provider" in d
        assert d["provider"] == "lmstudio"


# ---------------------------------------------------------------------------
# _map_capabilities
# ---------------------------------------------------------------------------


class TestMapCapabilities:
    def test_vision_from_input_modalities(self) -> None:
        item = {"architecture": {"input_modalities": ["text", "image"]}}
        caps = _map_capabilities(item)
        assert caps.vision is True

    def test_vision_from_arch_modality_string(self) -> None:
        item = {"architecture": {"modality": "text+image->text"}}
        caps = _map_capabilities(item)
        assert caps.vision is True

    def test_vision_from_top_level_modality(self) -> None:
        item = {"modality": "text+image->text"}
        caps = _map_capabilities(item)
        assert caps.vision is True

    def test_no_vision_when_text_only(self) -> None:
        item = {"architecture": {"input_modalities": ["text"]}}
        caps = _map_capabilities(item)
        assert caps.vision is False

    def test_tool_use_from_tools_param(self) -> None:
        item = {"supported_parameters": ["temperature", "tools", "tool_choice"]}
        caps = _map_capabilities(item)
        assert caps.trained_for_tool_use is True

    def test_tool_use_false_without_tools(self) -> None:
        """supported_parameters explicitly PRESENT (not absent) as a list
        that omits both "tools" and "tool_choice" — this is the ONLY case
        that forces trained_for_tool_use to False under the new contract.
        """
        item = {"supported_parameters": ["temperature", "max_tokens"]}
        caps = _map_capabilities(item)
        assert caps.trained_for_tool_use is False

    def test_tool_use_false_when_present_but_empty(self) -> None:
        """An explicit empty list is still "present" — must NOT be treated
        as absent. Guards against a naive falsy-check implementation that
        would otherwise default an empty list to True like a missing key.
        """
        item = {"supported_parameters": []}
        caps = _map_capabilities(item)
        assert caps.trained_for_tool_use is False

    def test_reasoning_from_reasoning_param(self) -> None:
        item = {"supported_parameters": ["reasoning", "temperature"]}
        caps = _map_capabilities(item)
        assert caps.reasoning is not None

    def test_reasoning_from_include_reasoning(self) -> None:
        item = {"supported_parameters": ["include_reasoning"]}
        caps = _map_capabilities(item)
        assert caps.reasoning is not None

    def test_no_capabilities_when_empty_item(self) -> None:
        """An entirely empty item has no supported_parameters key, so
        trained_for_tool_use defaults to True under the new contract (see
        test_missing_supported_parameters_no_crash); vision/reasoning still
        default False since nothing suggests otherwise.
        """
        caps = _map_capabilities({})
        assert caps.vision is False
        assert caps.trained_for_tool_use is True
        assert caps.reasoning is None

    def test_missing_supported_parameters_no_crash(self) -> None:
        """Absent supported_parameters defaults gracefully — never block.

        Providers that don't emit OpenRouter's supported_parameters shape
        (Groq native, generic OpenAI-compat) must not be penalized with
        trained_for_tool_use=False just for not advertising the field;
        that used to hide the FE's entire integrations/tools picker for a
        perfectly tool-capable model.
        """
        item = {"id": "gpt-4o", "context_length": 128000}
        caps = _map_capabilities(item)
        assert caps.vision is False
        assert caps.trained_for_tool_use is True

    def test_tool_use_true_when_supported_parameters_not_a_list(self) -> None:
        """A malformed (non-list) supported_parameters is treated the same
        as absent — unknown, not "no tools" — so it also defaults True.
        """
        item = {"id": "weird-provider-model", "supported_parameters": "tools"}
        caps = _map_capabilities(item)
        assert caps.trained_for_tool_use is True


class TestMapModelInfo:
    def test_basic_mapping(self) -> None:
        item: dict[str, Any] = {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "context_length": 128000,
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools"],
        }
        info = _map_model_info(item, provider_slug="openrouter")
        assert info.key == "openai/gpt-4o"
        assert info.display_name == "GPT-4o"
        assert info.provider == "openrouter"
        assert info.max_context_length == 128000
        assert info.capabilities is not None
        assert info.capabilities.vision is True
        assert info.capabilities.trained_for_tool_use is True
        assert info.type == "llm"
        assert info.loaded_instances == 1  # cloud models always "available"

    def test_missing_context_length_defaults_zero(self) -> None:
        item: dict[str, Any] = {"id": "m1"}
        info = _map_model_info(item, provider_slug="groq")
        assert info.max_context_length == 0

    def test_id_used_as_display_name_fallback(self) -> None:
        item: dict[str, Any] = {"id": "meta-llama/llama-3.3-70b"}
        info = _map_model_info(item, provider_slug="openrouter")
        assert info.display_name == "meta-llama/llama-3.3-70b"


# ---------------------------------------------------------------------------
# ModelCatalogService — LM-Studio-only regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lmstudio_only_no_cloud_providers() -> None:
    """When registry has only 'lmstudio', list_merged returns svc.list_loaded() unchanged.

    REGRESSION-CRITICAL: the result must be the same list object (or an equal
    list) — adding cloud providers must not alter the LM-Studio-only path.
    """
    lm_models = [
        _make_model_info("qwen3-35b"),
        _make_model_info("qwen3-9b"),
    ]
    svc = _make_models_svc(lm_models)
    reg = _make_registry(names=["lmstudio"])

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    result = await catalog.list_merged()

    assert result == lm_models
    # provider field on each item is "lmstudio"
    for m in result:
        assert m.provider == "lmstudio"


@pytest.mark.asyncio
async def test_lmstudio_only_empty_cloud_list() -> None:
    """When cloud provider names list is empty, fallback path is taken."""
    svc = _make_models_svc([])
    reg = _make_registry(names=["lmstudio"])

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    result = await catalog.list_merged()
    assert result == []


# ---------------------------------------------------------------------------
# Merge with cloud provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_includes_cloud_models() -> None:
    """list_merged includes LM Studio + cloud provider models."""
    lm_models = [_make_model_info("local-model")]
    cloud_items = [
        {"id": "openai/gpt-4o", "context_length": 128000,
         "architecture": {"input_modalities": ["text", "image"]},
         "supported_parameters": ["tools"]},
        {"id": "meta-llama/llama-3.3-70b", "context_length": 131072},
    ]
    cloud_prov = _make_compat_provider(items=cloud_items, http_status=200)
    svc = _make_models_svc(lm_models)
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    result = await catalog.list_merged()

    assert len(result) == 3  # 1 local + 2 cloud
    keys = {m.key for m in result}
    assert "local-model" in keys
    assert "openai/gpt-4o" in keys
    assert "meta-llama/llama-3.3-70b" in keys

    # Provider field is set correctly
    local = next(m for m in result if m.key == "local-model")
    assert local.provider == "lmstudio"
    gpt4o = next(m for m in result if m.key == "openai/gpt-4o")
    assert gpt4o.provider == "openrouter"


@pytest.mark.asyncio
async def test_merge_same_id_different_providers_are_distinct() -> None:
    """Same model id under two providers stays as two distinct entries."""
    lm_models = []
    cloud_items_a = [{"id": "gpt-4o"}]
    cloud_items_b = [{"id": "gpt-4o"}]

    prov_a = _make_compat_provider(items=cloud_items_a, http_status=200)
    prov_b = _make_compat_provider(items=cloud_items_b, http_status=200)

    svc = _make_models_svc(lm_models)
    reg = _make_registry(
        names=["lmstudio", "openai", "openrouter"],
        providers={"openai": prov_a, "openrouter": prov_b},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    result = await catalog.list_merged()

    assert len(result) == 2, f"Expected 2 distinct entries, got {result}"
    providers = {m.provider for m in result}
    assert "openai" in providers
    assert "openrouter" in providers


@pytest.mark.asyncio
async def test_merge_capabilities_mapped() -> None:
    """Cloud model capabilities are mapped from provider metadata."""
    cloud_items = [
        {
            "id": "vision-model",
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "reasoning"],
        }
    ]
    cloud_prov = _make_compat_provider(items=cloud_items, http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    result = await catalog.list_merged()

    assert len(result) == 1
    m = result[0]
    assert m.capabilities is not None
    assert m.capabilities.vision is True
    assert m.capabilities.trained_for_tool_use is True
    assert m.capabilities.reasoning is not None


# ---------------------------------------------------------------------------
# Unreachable provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_provider_does_not_block_lmstudio() -> None:
    """An unreachable cloud provider doesn't prevent LM Studio models from appearing."""
    lm_models = [_make_model_info("local-qwen")]
    cloud_prov = _make_compat_provider(items=[], http_status=None, error="Connection refused")
    svc = _make_models_svc(lm_models)
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    result = await catalog.list_merged()

    assert len(result) == 1
    assert result[0].key == "local-qwen"


@pytest.mark.asyncio
async def test_unreachable_provider_reported_in_status() -> None:
    """provider_status() reports reachable=False for a failed provider.

    No prior list_merged() call is required — provider_status() fetches
    actively and surfaces the real error.
    """
    cloud_prov = _make_compat_provider(items=[], http_status=None, error="timeout")
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    # No prior list_merged() — provider_status() must fetch on its own.
    statuses = await catalog.provider_status()
    assert len(statuses) == 1
    assert statuses[0].provider == "openrouter"
    assert statuses[0].reachable is False
    assert statuses[0].error is not None
    assert statuses[0].error != "not yet fetched"


@pytest.mark.asyncio
async def test_reachable_provider_reported_in_status() -> None:
    """provider_status() reports reachable=True without a prior list_merged() call."""
    cloud_prov = _make_compat_provider(items=[{"id": "m1"}], http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    # Standalone call — no list_merged() first.
    statuses = await catalog.provider_status()
    assert len(statuses) == 1
    assert statuses[0].provider == "openrouter"
    assert statuses[0].reachable is True
    assert statuses[0].error is None


@pytest.mark.asyncio
async def test_status_cold_cache_fetches_actively() -> None:
    """provider_status() fetches provider reachability even when cache is cold.

    The old behaviour was to return 'not yet fetched' for a cold cache entry.
    The new behaviour actively calls _fetch_provider so a freshly-added provider
    gets a real status result immediately — no prior list_merged() needed.
    """
    cloud_prov = _make_compat_provider(items=[{"id": "m1"}], http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    # Cold cache — no prior calls.
    statuses = await catalog.provider_status()

    assert len(statuses) == 1
    assert statuses[0].provider == "openrouter"
    # Must be reachable=True with a real fetch, NOT "not yet fetched".
    assert statuses[0].reachable is True
    assert statuses[0].error is None
    # Confirm a real network call was made (not skipped).
    cloud_prov.list_models_detailed.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_no_instance_in_registry() -> None:
    """provider_status() for a slug with no live instance returns a clear error."""
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "ghost"],
        providers={},  # "ghost" slug has no instance
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    statuses = await catalog.provider_status()

    assert len(statuses) == 1
    assert statuses[0].provider == "ghost"
    assert statuses[0].reachable is False
    assert statuses[0].error == "no provider instance in registry"


@pytest.mark.asyncio
async def test_timeout_provider_degrades_gracefully() -> None:
    """A provider that times out is treated as unreachable; LM Studio models still returned."""
    lm_models = [_make_model_info("local")]
    svc = _make_models_svc(lm_models)

    async def _hang() -> tuple[list, int | None, str | None]:
        await asyncio.sleep(100)  # blocks forever in test
        return [], None, None

    slow_prov = MagicMock()
    slow_prov.list_models_detailed = _hang

    reg = _make_registry(
        names=["lmstudio", "slowprovider"],
        providers={"slowprovider": slow_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, fetch_timeout=0.05)
    result = await catalog.list_merged()

    assert len(result) == 1
    assert result[0].key == "local"


# ---------------------------------------------------------------------------
# Cache TTL behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_second_call_within_ttl_does_not_refetch() -> None:
    """Second call within TTL uses cached data — list_models_detailed called once."""
    cloud_prov = _make_compat_provider(items=[{"id": "m1"}], http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=60.0)
    await catalog.list_merged()
    await catalog.list_merged()

    cloud_prov.list_models_detailed.assert_awaited_once()


@pytest.mark.asyncio
async def test_cache_expired_triggers_refetch() -> None:
    """After TTL expiry a new fetch is triggered."""
    cloud_prov = _make_compat_provider(items=[{"id": "m1"}], http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=0.001)
    await catalog.list_merged()
    # Let TTL expire.
    await asyncio.sleep(0.01)
    await catalog.list_merged()

    assert cloud_prov.list_models_detailed.await_count == 2


# ---------------------------------------------------------------------------
# Auth-failure cache invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_stores_entry_in_cache() -> None:
    """A 401 response stores a failure entry in cache so provider_status() surfaces it.

    Previously the entry was evicted, leaving provider_status() to report
    "not yet fetched".  After Fix 1 the entry IS cached with reachable=False
    and the correct auth-failure error message.  Key correction goes through
    PUT /api/admin/providers → registry.refresh() → catalog.invalidate(), which
    evicts the stale entry — so caching the failure does not prevent a corrected
    key from taking effect.
    """
    cloud_prov = _make_compat_provider(items=[], http_status=401, error="HTTP 401")
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=60.0)

    # First call: 401 → entry IS stored (not evicted).
    await catalog.list_merged()
    assert "openrouter" in catalog._cache
    assert catalog._cache["openrouter"].reachable is False
    assert catalog._cache["openrouter"].error == "Auth failure (HTTP 401)"

    # Second call within TTL: cache hit — no second fetch.
    await catalog.list_merged()
    assert cloud_prov.list_models_detailed.await_count == 1


@pytest.mark.asyncio
async def test_auth_failure_provider_status_surfaces_real_error() -> None:
    """provider_status() after a 401 reports auth-failure, not 'not yet fetched'.

    provider_status() now fetches actively, so no prior list_merged() is
    needed.  A 401 result is cached and returned directly.
    """
    cloud_prov = _make_compat_provider(items=[], http_status=401, error="HTTP 401")
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=60.0)
    # No prior list_merged() — provider_status() fetches on its own.
    statuses = await catalog.provider_status()
    assert len(statuses) == 1
    st = statuses[0]
    assert st.provider == "openrouter"
    assert st.reachable is False
    assert st.error is not None
    assert "Auth failure" in st.error
    assert "not yet fetched" not in (st.error or "")


@pytest.mark.asyncio
async def test_403_also_stores_failure_entry_in_cache() -> None:
    """A 403 Forbidden also stores a failure entry (same as 401)."""
    cloud_prov = _make_compat_provider(items=[], http_status=403, error="HTTP 403")
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=60.0)
    await catalog.list_merged()
    assert "openrouter" in catalog._cache
    assert catalog._cache["openrouter"].reachable is False
    assert catalog._cache["openrouter"].error == "Auth failure (HTTP 403)"


# ---------------------------------------------------------------------------
# Manual invalidation (registry refresh path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_evicts_single_provider() -> None:
    """invalidate(slug) removes a provider's cache entry."""
    cloud_prov = _make_compat_provider(items=[{"id": "m1"}], http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=60.0)
    await catalog.list_merged()
    assert "openrouter" in catalog._cache

    catalog.invalidate("openrouter")
    assert "openrouter" not in catalog._cache

    # Next call fetches again.
    await catalog.list_merged()
    assert cloud_prov.list_models_detailed.await_count == 2


@pytest.mark.asyncio
async def test_invalidate_all_clears_cache() -> None:
    """invalidate_all() clears all cache entries."""
    cloud_prov = _make_compat_provider(items=[{"id": "m1"}], http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=60.0)
    await catalog.list_merged()

    catalog.invalidate_all()
    assert len(catalog._cache) == 0


# ---------------------------------------------------------------------------
# Fetch de-duplication (per-provider lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_fetch_provider_calls_deduplicated() -> None:
    """Two concurrent _fetch_provider calls for the same provider issue only one network fetch.

    This tests the per-provider asyncio.Lock: the second concurrent caller
    waits for the first to finish, then returns the freshly-cached result
    without issuing a duplicate call to list_models_detailed.
    """
    import asyncio as _asyncio

    fetch_started = _asyncio.Event()
    fetch_can_proceed = _asyncio.Event()
    call_count = 0

    async def _slow_list() -> tuple[list[dict[str, Any]], int, None]:
        nonlocal call_count
        call_count += 1
        fetch_started.set()
        await fetch_can_proceed.wait()
        return [{"id": "m1"}], 200, None

    slow_prov = MagicMock()
    slow_prov.list_models_detailed = _slow_list

    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": slow_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=60.0)

    # Start two concurrent fetches.
    t1 = _asyncio.create_task(catalog._fetch_provider("openrouter", slow_prov))
    # Wait until the first fetch has started before launching the second.
    await fetch_started.wait()
    t2 = _asyncio.create_task(catalog._fetch_provider("openrouter", slow_prov))

    # Let the first fetch complete.
    fetch_can_proceed.set()
    e1, e2 = await _asyncio.gather(t1, t2)

    # Only one actual network call should have been made.
    assert call_count == 1, f"Expected 1 fetch, got {call_count}"
    # Both callers get a reachable=True entry.
    assert e1.reachable is True
    assert e2.reachable is True


@pytest.mark.asyncio
async def test_provider_status_and_list_merged_concurrent_deduplicated() -> None:
    """Concurrent list_merged() + provider_status() for the same provider fetch only once.

    Simulates the FE firing GET /api/models + GET /api/providers/status at the
    same time right after a provider is added (both calls now go through
    _fetch_provider, which uses the per-provider lock).
    """
    import asyncio as _asyncio

    call_count = 0

    async def _counting_list() -> tuple[list[dict[str, Any]], int, None]:
        nonlocal call_count
        call_count += 1
        await _asyncio.sleep(0)  # yield to let both tasks start
        return [{"id": "m1"}], 200, None

    prov = MagicMock()
    prov.list_models_detailed = _counting_list

    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, ttl=60.0)

    # Fire both concurrently (cold cache).
    results = await _asyncio.gather(
        catalog.list_merged(),
        catalog.provider_status(),
    )

    # Only one real fetch despite two concurrent callers.
    assert call_count == 1, f"Expected 1 fetch, got {call_count}"
    # provider_status result is the second gather result.
    statuses = results[1]
    assert len(statuses) == 1
    assert statuses[0].reachable is True


# ---------------------------------------------------------------------------
# Allowlist filter tests
# ---------------------------------------------------------------------------


def _make_config_svc(
    allowlists: dict[str, list[str] | None] | None = None,
) -> MagicMock:
    """Build a mock ProviderConfigService returning configured safe views."""
    from lmchat.services.provider_config_service import ProviderConfigSafeView

    svc = MagicMock()
    views = []
    for provider, models in (allowlists or {}).items():
        views.append(
            ProviderConfigSafeView(
                provider=provider,
                base_url="https://example.com",
                default_model=None,
                extra_headers=None,
                enabled=True,
                api_key_set=False,
                allowed_models=models,
            )
        )
    svc.list_all = AsyncMock(return_value=views)
    return svc


@pytest.mark.asyncio
async def test_allowlist_filters_cloud_models() -> None:
    """With allowed_models set, /api/models returns ONLY those ids for that provider."""
    cloud_items = [
        {"id": "model-a"},
        {"id": "model-b"},
        {"id": "model-c"},
    ]
    cloud_prov = _make_compat_provider(items=cloud_items, http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )
    config_svc = _make_config_svc({"openrouter": ["model-a", "model-c"]})

    catalog = ModelCatalogService(models_svc=svc, registry=reg, config_svc=config_svc)
    result = await catalog.list_merged()

    keys = {m.key for m in result}
    assert keys == {"model-a", "model-c"}, f"Expected only allowed ids, got {keys}"
    assert "model-b" not in keys


@pytest.mark.asyncio
async def test_allowlist_null_shows_all_models() -> None:
    """With allowed_models=None, all models from the provider are shown."""
    cloud_items = [{"id": "x"}, {"id": "y"}]
    cloud_prov = _make_compat_provider(items=cloud_items, http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )
    config_svc = _make_config_svc({"openrouter": None})

    catalog = ModelCatalogService(models_svc=svc, registry=reg, config_svc=config_svc)
    result = await catalog.list_merged()
    assert {m.key for m in result} == {"x", "y"}


@pytest.mark.asyncio
async def test_allowlist_no_config_svc_shows_all_models() -> None:
    """When config_svc is None (regression path), all models are shown."""
    cloud_items = [{"id": "a"}, {"id": "b"}]
    cloud_prov = _make_compat_provider(items=cloud_items, http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )

    catalog = ModelCatalogService(models_svc=svc, registry=reg, config_svc=None)
    result = await catalog.list_merged()
    assert {m.key for m in result} == {"a", "b"}


@pytest.mark.asyncio
async def test_allowlist_does_not_filter_lmstudio_models() -> None:
    """LM Studio models are never filtered regardless of any allowlist config."""
    lm_models = [_make_model_info("local-qwen"), _make_model_info("local-llama")]
    svc = _make_models_svc(lm_models)
    reg = _make_registry(names=["lmstudio"])
    # Even if we accidentally put "lmstudio" in the config svc, the regression-safe
    # code path returns lm_models directly before any filtering.
    config_svc = _make_config_svc({})

    catalog = ModelCatalogService(models_svc=svc, registry=reg, config_svc=config_svc)
    result = await catalog.list_merged()
    assert {m.key for m in result} == {"local-qwen", "local-llama"}


@pytest.mark.asyncio
async def test_allowlist_regression_no_providers_unchanged() -> None:
    """No providers + no config_svc: identical to legacy path (regression guard)."""
    lm_models = [_make_model_info("qwen3-35b"), _make_model_info("qwen3-9b")]
    svc = _make_models_svc(lm_models)
    reg = _make_registry(names=["lmstudio"])

    catalog = ModelCatalogService(models_svc=svc, registry=reg)
    result = await catalog.list_merged()
    assert result == lm_models


@pytest.mark.asyncio
async def test_list_merged_openrouter_style_ids_surface_and_pass_allowlist() -> None:
    """OpenRouter-style namespaced ids (e.g. 'openai/gpt-4o-mini') surface
    verbatim with no allowlist, and pass an allowlist that names them
    explicitly — confirms the slash in the id is never mangled/mismatched
    anywhere in the merge + filter pipeline (namespacing is not the cause
    of a cloud provider showing 0 models)."""
    cloud_items = [{"id": "openai/gpt-4o-mini"}]

    # No allowlist configured for this provider — the model surfaces as-is.
    cloud_prov = _make_compat_provider(items=cloud_items, http_status=200)
    svc = _make_models_svc([])
    reg = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov},
    )
    catalog = ModelCatalogService(models_svc=svc, registry=reg, config_svc=None)
    result = await catalog.list_merged()
    assert {m.key for m in result} == {"openai/gpt-4o-mini"}

    # An allowlist naming the SAME namespaced id still lets it through.
    cloud_prov2 = _make_compat_provider(items=cloud_items, http_status=200)
    reg2 = _make_registry(
        names=["lmstudio", "openrouter"],
        providers={"openrouter": cloud_prov2},
    )
    config_svc = _make_config_svc({"openrouter": ["openai/gpt-4o-mini"]})
    catalog2 = ModelCatalogService(models_svc=svc, registry=reg2, config_svc=config_svc)
    result2 = await catalog2.list_merged()
    assert {m.key for m in result2} == {"openai/gpt-4o-mini"}
