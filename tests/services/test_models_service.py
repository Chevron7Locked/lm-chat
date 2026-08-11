# SPDX-License-Identifier: Apache-2.0
"""Tests for ModelsService (capability probe + cache; lifecycle ops).

Tests for the models service (list/cache, load/unload/download).

Uses a mock httpx.AsyncClient.  The fixture shape matches LM Studio's
live probe responses (capability probe + lifecycle ops).

Covers:
- list_loaded with a mock client → parses into list[ModelInfo].
- loaded_instance_ids populated from loaded_instances[*].id.
- get_capabilities returns correct Capabilities for a known model_id.
- refresh re-probes (mock client called again).
- Capabilities with reasoning: None for non-reasoning models.
- Capabilities with reasoning present: parses ReasoningCapability shape.
- load_model success → ModelLoadResult.
- load_model upstream 4xx → UpstreamModelError.
- load_model upstream 5xx → UpstreamGatewayError.
- unload_instance success → ModelUnloadResult.
- unload_instance upstream 4xx → UpstreamModelError.
- unload_all_instances → calls unload_instance for each loaded instance.
- download_model success → ModelDownloadResult.
- download_model upstream 4xx → UpstreamModelError.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from lmchat.services.models_service import (
    Capabilities,
    ModelInfo,
    ModelLoadResult,
    ModelsService,
    ModelUnloadResult,
    ReasoningCapability,
    ResolvedModel,
    UnloadAllResult,
    UpstreamGatewayError,
    UpstreamModelError,
    make_models_service,
)
from lmchat.services.params_service import ParamsService

# ---------------------------------------------------------------------------
# Fixtures — inline /api/v1/models response shape
# ---------------------------------------------------------------------------

# Minimal model entry without reasoning capability.
# loaded_instances uses the {id, config} shape LM Studio returns.
_PLAIN_MODEL: dict[str, Any] = {
    "key": "qwen3-8b",
    "type": "llm",
    "publisher": "Qwen",
    "displayName": "Qwen3.6 35B A3B MLX",
    "architecture": "qwen3",
    "sizeBytes": 19_000_000_000,
    "paramsString": "35.4B",
    # loaded_instances is [{id, config}] in the live response.
    "loaded_instances": [{"id": "qwen3-8b", "config": {"context_length": 32768}}],
    "maxContextLength": 32768,
    "format": "mlx",
    "capabilities": {
        "vision": True,
        "trained_for_tool_use": True,
    },
}

# Model entry WITH reasoning capability.
_REASONING_MODEL: dict[str, Any] = {
    "key": "deepseek-r1-7b-gguf",
    "type": "llm",
    "publisher": "DeepSeek",
    "displayName": "DeepSeek R1 7B",
    "architecture": "deepseek",
    "sizeBytes": 7_000_000_000,
    "paramsString": "7B",
    "loaded_instances": [],
    "maxContextLength": 8192,
    "format": "gguf",
    "capabilities": {
        "vision": False,
        "trained_for_tool_use": False,
        "reasoning": {
            "allowed_options": ["off", "low", "medium", "high"],
            "default": "medium",
        },
    },
}

_MODELS_RESPONSE: dict[str, Any] = {
    "models": [_PLAIN_MODEL, _REASONING_MODEL],
}


# ---------------------------------------------------------------------------
# Helper to build a mock httpx.AsyncClient
# ---------------------------------------------------------------------------


def _make_mock_client(response_json: dict[str, Any]) -> httpx.AsyncClient:
    """Return an httpx.AsyncClient mock that returns *response_json* on GET."""
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status = MagicMock(return_value=None)
    mock_response.json = MagicMock(return_value=response_json)
    mock_response.status_code = 200

    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=mock_response)
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client() -> httpx.AsyncClient:
    """A mock httpx.AsyncClient returning the standard models fixture."""
    return _make_mock_client(_MODELS_RESPONSE)


@pytest.fixture()
def svc(mock_client: httpx.AsyncClient) -> ModelsService:
    """A ModelsService backed by the mock client."""
    return make_models_service(mock_client, "http://localhost:1234")


# ---------------------------------------------------------------------------
# list_loaded
# ---------------------------------------------------------------------------


async def test_list_loaded_returns_model_info_list(svc: ModelsService) -> None:
    """list_loaded parses the mock response into list[ModelInfo]."""
    models = await svc.list_loaded()
    assert len(models) == 2
    assert all(isinstance(m, ModelInfo) for m in models)


async def test_list_loaded_model_keys(svc: ModelsService) -> None:
    """list_loaded parses model keys correctly."""
    models = await svc.list_loaded()
    keys = [m.key for m in models]
    assert "qwen3-8b" in keys
    assert "deepseek-r1-7b-gguf" in keys


async def test_list_loaded_loaded_instances_normalised(svc: ModelsService) -> None:
    """loaded_instances is normalised from array to count."""
    models = await svc.list_loaded()
    qwen = next(m for m in models if m.key == "qwen3-8b")
    # live response has 1 instance in the array → normalised to 1.
    assert qwen.loaded_instances == 1


async def test_list_loaded_caches_result(
    mock_client: httpx.AsyncClient, svc: ModelsService
) -> None:
    """list_loaded uses the cache on the second call (no second HTTP request)."""
    await svc.list_loaded()
    await svc.list_loaded()
    # get() should have been called exactly once (first call triggers refresh).
    assert mock_client.get.call_count == 1  # type: ignore[attr-defined]


async def test_list_loaded_display_name(svc: ModelsService) -> None:
    """display_name is parsed from the camelCase validation_alias 'displayName'."""
    models = await svc.list_loaded()
    qwen = next(m for m in models if m.key == "qwen3-8b")
    assert qwen.display_name == "Qwen3.6 35B A3B MLX"


async def test_list_loaded_size_bytes(svc: ModelsService) -> None:
    """size_bytes is parsed from the camelCase validation_alias 'sizeBytes'."""
    models = await svc.list_loaded()
    qwen = next(m for m in models if m.key == "qwen3-8b")
    assert qwen.size_bytes == 19_000_000_000


# max_context_length wire-through. The pydantic field at
# models_service.py has validation_alias="maxContextLength".
# The BE pre-flight budget gate and the FE context-meter UI both
# consume this — verify the cached ModelInfo exposes it.
async def test_list_loaded_max_context_length(svc: ModelsService) -> None:
    """max_context_length is parsed from the camelCase validation_alias."""
    models = await svc.list_loaded()
    qwen = next(m for m in models if m.key == "qwen3-8b")
    assert qwen.max_context_length == 32_768
    deepseek = next(m for m in models if m.key == "deepseek-r1-7b-gguf")
    assert deepseek.max_context_length == 8_192


# Casing policy. The upstream LM Studio aliases are VALIDATION-only
# (validation_alias): serialization
# must carry NO aliases, so the lm-chat wire (GET /api/models, FastAPI
# response_model serializes by_alias=True) is snake_case everywhere. This
# is the structural guard for the FE's generated-types adoption: if a
# serialization alias ever reappears, the camelCase key would leak back
# onto the wire and the generated components["schemas"]["ModelInfo"]
# consumers would silently read defaults.
async def test_model_info_wire_serialization_is_snake_case_only(
    svc: ModelsService,
) -> None:
    """model_dump(by_alias=True) emits snake_case keys, never camelCase."""
    models = await svc.list_loaded()
    qwen = next(m for m in models if m.key == "qwen3-8b")
    dumped = qwen.model_dump(by_alias=True)
    # The five formerly-aliased fields serialize under their field names…
    assert dumped["display_name"] == "Qwen3.6 35B A3B MLX"
    assert dumped["size_bytes"] == 19_000_000_000
    assert dumped["params_string"] == "35.4B"
    assert dumped["max_context_length"] == 32_768
    assert dumped["loaded_context_length"] == 32_768
    # …and the camelCase upstream spellings never reach the wire.
    camel = {
        "displayName",
        "sizeBytes",
        "paramsString",
        "maxContextLength",
        "loadedContextLength",
    }
    assert camel.isdisjoint(dumped.keys())


async def test_list_loaded_loaded_context_length_from_instance_config(
    svc: ModelsService,
) -> None:
    """``loaded_context_length`` mirrors the per-instance
    ``config.context_length`` (the ACTUALLY-LOADED context),
    NOT the model's architectural max. The plain model's fixture has one
    loaded instance with ``config.context_length=32768``; the reasoning
    model has zero instances so its loaded value is 0 (consumers should
    fall back to ``max_context_length`` for display in that case).
    """
    models = await svc.list_loaded()
    qwen = next(m for m in models if m.key == "qwen3-8b")
    assert qwen.loaded_context_length == 32_768
    deepseek = next(m for m in models if m.key == "deepseek-r1-7b-gguf")
    assert deepseek.loaded_context_length == 0  # nothing loaded


async def test_probe_dedups_duplicate_model_keys_from_upstream(
    mock_client: httpx.AsyncClient,
) -> None:
    """LM Studio anomaly: the upstream /api/v1/models can
    return the same ``key`` more than once (observed: the admin's
    nomic-embed embedding model appears twice with no clear reason).

    Without dedup, every FE consumer renders duplicate React keys
    ("Encountered two children with the same key …") and dropdowns
    silently render half the entries. The probe MUST dedupe by ``key``,
    keeping the first occurrence.
    """
    # Same model key returned twice. Two distinct shapes — second has
    # extra metadata — to prove the dedup keeps the FIRST occurrence
    # (not the last, not merged).
    fixture: dict[str, Any] = {
        "models": [
            {
                "key": "text-embedding-nomic-embed-text-v1.5",
                "type": "embedding",
                "publisher": "nomic",
                "displayName": "First Copy",
                "loaded_instances": [
                    {"id": "text-embedding-nomic-embed-text-v1.5",
                     "config": {"context_length": 8192}},
                ],
                "maxContextLength": 8192,
            },
            {
                "key": "qwen3-vl-8b-instruct",
                "type": "llm",
                "publisher": "Qwen",
                "displayName": "Qwen VL",
                "loaded_instances": [],
                "maxContextLength": 32_768,
                "capabilities": {"vision": True, "trained_for_tool_use": True},
            },
            {
                "key": "text-embedding-nomic-embed-text-v1.5",  # dup
                "type": "embedding",
                "publisher": "nomic",
                "displayName": "Second Copy (should be dropped)",
                "loaded_instances": [],
                "maxContextLength": 8192,
            },
        ],
    }
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status = MagicMock(return_value=None)
    mock_response.json = MagicMock(return_value=fixture)
    mock_response.status_code = 200
    mock_client.get = AsyncMock(  # type: ignore[method-assign]
        return_value=mock_response,
    )
    svc = make_models_service(
        http_client=mock_client, base_url="http://localhost:1234"
    )
    await svc.refresh()
    models = await svc.list_loaded()
    # Two unique keys in, two unique keys out (no third entry).
    keys = [m.key for m in models]
    assert keys.count("text-embedding-nomic-embed-text-v1.5") == 1, keys
    assert len(models) == 2
    # First occurrence was kept (the one with "First Copy" display name).
    embedding = next(
        m for m in models if m.key == "text-embedding-nomic-embed-text-v1.5"
    )
    assert embedding.display_name == "First Copy"


async def test_probe_merge_keeps_loaded_instance_when_loaded_copy_is_second(
    mock_client: httpx.AsyncClient,
) -> None:
    """When a key is returned twice with the LOADED copy
    SECOND, first-occurrence dedup kept the UNLOADED copy — making a live model
    look unloaded (the cause of spurious 'no embedding model' / 'model not
    loaded'). The merge must union loaded_instance_ids so the loaded instance
    survives regardless of probe ordering.
    """
    fixture: dict[str, Any] = {
        "models": [
            {  # UNLOADED copy FIRST
                "key": "text-embedding-nomic-embed-text-v1.5",
                "type": "embedding",
                "loaded_instances": [],
                "maxContextLength": 8192,
            },
            {  # LOADED copy SECOND
                "key": "text-embedding-nomic-embed-text-v1.5",
                "type": "embedding",
                "loaded_instances": [
                    {"id": "nomic-inst-1", "config": {"context_length": 8192}},
                ],
                "maxContextLength": 8192,
            },
        ],
    }
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status = MagicMock(return_value=None)
    mock_response.json = MagicMock(return_value=fixture)
    mock_response.status_code = 200
    mock_client.get = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    svc = make_models_service(
        http_client=mock_client, base_url="http://localhost:1234"
    )
    await svc.refresh()
    models = await svc.list_loaded()
    assert len(models) == 1
    nomic = models[0]
    # The loaded instance from the SECOND copy survived the merge.
    assert "nomic-inst-1" in nomic.loaded_instance_ids
    assert nomic.loaded_instances >= 1


async def test_probe_merge_keeps_min_of_nonzero_loaded_context_length(
    mock_client: httpx.AsyncClient,
) -> None:
    """When a key appears twice with DIFFERENT non-zero
    loaded_context_length values (two loaded instances at different
    context sizes), the merge must take the MIN — the safe floor used
    everywhere else in this module (see the per-instance min collapse
    just above the dedup loop) — not the first non-zero value
    encountered. Taking the larger value would let a request budget
    itself against a context window bigger than what's actually
    guaranteed safe for every loaded instance.
    """
    fixture: dict[str, Any] = {
        "models": [
            {  # Larger context FIRST.
                "key": "qwen3-8b",
                "type": "llm",
                "loaded_instances": [
                    {"id": "qwen-inst-1", "config": {"context_length": 32768}},
                ],
                "maxContextLength": 131072,
            },
            {  # Smaller (safer floor) context SECOND.
                "key": "qwen3-8b",
                "type": "llm",
                "loaded_instances": [
                    {"id": "qwen-inst-2", "config": {"context_length": 8192}},
                ],
                "maxContextLength": 131072,
            },
        ],
    }
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status = MagicMock(return_value=None)
    mock_response.json = MagicMock(return_value=fixture)
    mock_response.status_code = 200
    mock_client.get = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    svc = make_models_service(
        http_client=mock_client, base_url="http://localhost:1234"
    )
    await svc.refresh()
    models = await svc.list_loaded()
    assert len(models) == 1
    # MIN(32768, 8192) == 8192 — the safer floor, not the first-seen value.
    assert models[0].loaded_context_length == 8192


async def test_force_refresh_storm_guard_single_probe(
    mock_client: httpx.AsyncClient,
) -> None:
    """N concurrent force_refresh() calls trigger AT MOST ONE
    upstream probe (single in-flight reprobe via the Event guard + 5s
    min-interval), so a flurry of stuck chat turns can't storm a down LM Studio.
    """
    import asyncio

    svc = make_models_service(
        http_client=mock_client, base_url="http://localhost:1234"
    )
    results = await asyncio.gather(*[svc.force_refresh() for _ in range(10)])
    # Exactly one upstream GET despite 10 concurrent callers.
    assert mock_client.get.call_count == 1, mock_client.get.call_count  # type: ignore[attr-defined]
    # At least one caller observed the successful probe.
    assert any(results)


async def test_get_max_context_length_prefers_loaded_over_architectural_max(
    mock_client: httpx.AsyncClient,
) -> None:
    """A 9B model loaded at 98304 was displaying as 262144 in Settings
    because the BE was reading ``max_context_length`` (the architectural
    max) instead of the loaded instance's ``config.context_length``.
    ``get_max_context_length`` — which feeds both the FE meter AND the
    BE pre-flight budget gate — MUST return the LOADED value when an
    instance is loaded. Otherwise the budget gate trusts a number 2.6×
    the actual available context, and requests silently overflow LM
    Studio's loaded window (the same silent-stream-death failure mode).
    """
    # A model where loaded != max — the exact scenario.
    fixture: dict[str, Any] = {
        "models": [
            {
                "key": "qwen3.5-9b",
                "type": "llm",
                "publisher": "Qwen",
                "displayName": "Qwen3.5 9B",
                "architecture": "qwen3",
                "loaded_instances": [
                    # config.context_length is the ACTUAL loaded window.
                    {"id": "qwen3.5-9b-i1", "config": {"context_length": 98_304}},
                ],
                # maxContextLength is the model's architectural ceiling.
                "maxContextLength": 262_144,
                "capabilities": {"vision": False, "trained_for_tool_use": True},
            },
        ],
    }
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status = MagicMock(return_value=None)
    mock_response.json = MagicMock(return_value=fixture)
    mock_response.status_code = 200
    mock_client.get = AsyncMock(  # type: ignore[method-assign]
        return_value=mock_response,
    )
    svc = make_models_service(
        http_client=mock_client, base_url="http://localhost:1234"
    )
    await svc.refresh()
    # The gate MUST see 98304 (loaded), not 262144 (max).
    assert await svc.get_max_context_length("qwen3.5-9b") == 98_304


# ---------------------------------------------------------------------------
# get_capabilities
# ---------------------------------------------------------------------------


async def test_get_capabilities_plain_model(svc: ModelsService) -> None:
    """get_capabilities returns correct Capabilities for a plain model."""
    caps = await svc.get_capabilities("qwen3-8b")
    assert isinstance(caps, Capabilities)
    assert caps.vision is True
    assert caps.trained_for_tool_use is True
    assert caps.reasoning is None


async def test_get_capabilities_reasoning_model(svc: ModelsService) -> None:
    """get_capabilities returns ReasoningCapability for a reasoning model."""
    caps = await svc.get_capabilities("deepseek-r1-7b-gguf")
    assert isinstance(caps, Capabilities)
    assert caps.vision is False
    assert caps.trained_for_tool_use is False
    assert isinstance(caps.reasoning, ReasoningCapability)
    assert "medium" in caps.reasoning.allowed_options
    assert caps.reasoning.default == "medium"


async def test_get_capabilities_missing_model(svc: ModelsService) -> None:
    """get_capabilities raises KeyError for a model not in the cache."""
    with pytest.raises(KeyError, match="not-loaded-model"):
        await svc.get_capabilities("not-loaded-model")


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_refresh_re_probes_upstream(
    mock_client: httpx.AsyncClient, svc: ModelsService
) -> None:
    """refresh() calls upstream again, replacing the cache."""
    # Warm the cache.
    await svc.list_loaded()
    assert mock_client.get.call_count == 1  # type: ignore[attr-defined]

    # Explicit refresh.
    await svc.refresh()
    assert mock_client.get.call_count == 2  # type: ignore[attr-defined]


async def test_refresh_atomic_cache_replacement(
    mock_client: httpx.AsyncClient,
) -> None:
    """After refresh, list_loaded returns the updated model list."""
    svc = make_models_service(mock_client, "http://localhost:1234")

    # First probe — 2 models.
    await svc.list_loaded()
    models_v1 = await svc.list_loaded()
    assert len(models_v1) == 2

    # Override the mock to return a single model.
    single_model_response = {"models": [_PLAIN_MODEL]}
    mock_client.get.return_value.json.return_value = single_model_response  # type: ignore[attr-defined]

    await svc.refresh()
    models_v2 = await svc.list_loaded()
    assert len(models_v2) == 1
    assert models_v2[0].key == "qwen3-8b"


async def test_refresh_on_http_error_keeps_stale_cache(
    mock_client: httpx.AsyncClient,
) -> None:
    """refresh() on an HTTP error keeps the previous cache intact."""
    svc = make_models_service(mock_client, "http://localhost:1234")

    # Warm the cache.
    await svc.list_loaded()
    original_models = await svc.list_loaded()

    # Make the next call raise.
    mock_client.get.side_effect = httpx.RequestError("connection refused")  # type: ignore[attr-defined]

    await svc.refresh()  # must not raise

    # Cache still has the old data.
    models_after_error = await svc.list_loaded()
    assert len(models_after_error) == len(original_models)


async def test_refresh_cold_start_on_error(
    mock_client: httpx.AsyncClient,
) -> None:
    """refresh() with no prior cache and an HTTP error returns [] from list_loaded."""
    svc = make_models_service(mock_client, "http://localhost:1234")
    mock_client.get.side_effect = httpx.RequestError("connection refused")  # type: ignore[attr-defined]

    await svc.refresh()  # must not raise

    # Cold start error → empty list.
    models = await svc.list_loaded()
    assert models == []


# ---------------------------------------------------------------------------
# probe-completion hook clears rejected params for (re)loaded models
# ---------------------------------------------------------------------------


async def test_refresh_clears_rejected_params_for_newly_loaded_model(
    mock_client: httpx.AsyncClient,
) -> None:
    """A model going unloaded → loaded between probes gets its rejected
    params cleared (load-after-unload / reload detection via instance ids)."""
    params_service = ParamsService()
    svc = make_models_service(
        mock_client, "http://localhost:1234", params_service=params_service
    )

    # First probe: deepseek has NO loaded instances (fixture default).
    await svc.list_loaded()
    # A 400 during the session records a rejection for deepseek.
    await params_service.record_rejection(
        model_id="deepseek-r1-7b-gguf", param="reasoning"
    )

    # Second probe: deepseek now shows a loaded instance — it was loaded.
    loaded_deepseek = dict(
        _REASONING_MODEL,
        loaded_instances=[{"id": "deepseek-r1-7b-gguf", "config": {"context_length": 8192}}],
    )
    mock_client.get.return_value.json.return_value = {  # type: ignore[attr-defined]
        "models": [_PLAIN_MODEL, loaded_deepseek],
    }
    await svc.refresh()

    # The reload cleared deepseek's rejected set — the param re-probes.
    assert await params_service.get_rejected(model_id="deepseek-r1-7b-gguf") == frozenset()


async def test_refresh_calls_clear_for_model_for_newly_listed_key(
    mock_client: httpx.AsyncClient,
) -> None:
    """A key absent from the prior cache triggers clear_for_model(key);
    models unchanged between probes are NOT cleared."""
    params_service = MagicMock(spec=ParamsService)
    svc = make_models_service(
        mock_client, "http://localhost:1234", params_service=params_service
    )

    # First probe: only the qwen model is listed.
    mock_client.get.return_value.json.return_value = {  # type: ignore[attr-defined]
        "models": [_PLAIN_MODEL],
    }
    await svc.list_loaded()
    params_service.clear_for_model.reset_mock()

    # Second probe: deepseek appears for the first time.
    mock_client.get.return_value.json.return_value = {  # type: ignore[attr-defined]
        "models": [_PLAIN_MODEL, _REASONING_MODEL],
    }
    await svc.refresh()

    params_service.clear_for_model.assert_called_once_with("deepseek-r1-7b-gguf")


async def test_refresh_first_probe_does_not_clear(
    mock_client: httpx.AsyncClient,
) -> None:
    """First probe of the process lifetime (prior cache is None) clears nothing."""
    params_service = MagicMock(spec=ParamsService)
    svc = make_models_service(
        mock_client, "http://localhost:1234", params_service=params_service
    )

    await svc.list_loaded()

    params_service.clear_for_model.assert_not_called()


async def test_refresh_steady_state_does_not_clear(
    mock_client: httpx.AsyncClient,
) -> None:
    """An unchanged models list between probes clears nothing."""
    params_service = MagicMock(spec=ParamsService)
    svc = make_models_service(
        mock_client, "http://localhost:1234", params_service=params_service
    )

    await svc.list_loaded()
    await svc.refresh()  # identical response — no load/unload happened.

    params_service.clear_for_model.assert_not_called()


# ---------------------------------------------------------------------------
# P11b: loaded_instance_ids
# ---------------------------------------------------------------------------


async def test_list_loaded_instance_ids_populated(svc: ModelsService) -> None:
    """loaded_instance_ids is populated from loaded_instances[*].id."""
    models = await svc.list_loaded()
    qwen = next(m for m in models if m.key == "qwen3-8b")
    assert qwen.loaded_instance_ids == ["qwen3-8b"]


async def test_list_loaded_instance_ids_empty_for_unloaded(svc: ModelsService) -> None:
    """loaded_instance_ids is [] for models with no loaded instances."""
    models = await svc.list_loaded()
    ds = next(m for m in models if m.key == "deepseek-r1-7b-gguf")
    assert ds.loaded_instance_ids == []


# ---------------------------------------------------------------------------
# make_models_service factory
# ---------------------------------------------------------------------------


def test_make_models_service_returns_instance() -> None:
    """make_models_service returns a properly constructed ModelsService."""
    client = MagicMock(spec=httpx.AsyncClient)
    svc = make_models_service(client, "http://localhost:1234")
    assert isinstance(svc, ModelsService)


# ---------------------------------------------------------------------------
# P11b lifecycle: load_model
# ---------------------------------------------------------------------------


def _make_post_mock_client(
    status_code: int,
    response_json: dict[str, Any],
    is_success: bool | None = None,
) -> httpx.AsyncClient:
    """Return a mock client whose POST returns the given response."""
    if is_success is None:
        is_success = 200 <= status_code < 300
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = status_code
    mock_response.is_success = is_success
    mock_response.json = MagicMock(return_value=response_json)
    mock_response.content = b"{}"
    mock_response.text = str(response_json)

    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=MagicMock(
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value=_MODELS_RESPONSE),
        status_code=200,
    ))
    client.post = AsyncMock(return_value=mock_response)
    return client


async def test_load_model_success() -> None:
    """load_model returns ModelLoadResult on 200."""
    load_resp: dict[str, Any] = {
        "type": "llm",
        "instance_id": "qwen3-8b:2",
        "load_time_seconds": 10.32,
        "status": "loaded",
    }
    client = _make_post_mock_client(200, load_resp)
    svc = make_models_service(client, "http://localhost:1234")

    result = await svc.load_model("qwen3-8b")

    assert isinstance(result, ModelLoadResult)
    assert result.instance_id == "qwen3-8b:2"
    assert result.status == "loaded"
    assert abs(result.load_time_seconds - 10.32) < 0.01


async def test_load_model_upstream_4xx_raises_upstream_model_error() -> None:
    """load_model raises UpstreamModelError on 404; code/param are None when absent."""
    err_resp: dict[str, Any] = {
        "error": {"type": "model_not_found", "message": "Model not found"}
    }
    client = _make_post_mock_client(404, err_resp, is_success=False)
    svc = make_models_service(client, "http://localhost:1234")

    with pytest.raises(UpstreamModelError) as exc_info:
        await svc.load_model("nonexistent-model")

    err = exc_info.value
    assert err.status_code == 404
    assert err.error_type == "model_not_found"
    assert "not found" in err.message.lower()
    assert err.code is None
    assert err.param is None


async def test_load_model_upstream_4xx_parses_code_and_param() -> None:
    """load_model parses upstream error.code and error.param when present.

    Live shape from PROBES_p11b_lifecycle.md §4a (missing required field):
        {"error": {"message": "...", "type": "invalid_request",
                   "code": "missing_required_parameter", "param": "model"}}
    """
    err_resp: dict[str, Any] = {
        "error": {
            "message": "Missing required field 'model'",
            "type": "invalid_request",
            "code": "missing_required_parameter",
            "param": "model",
        }
    }
    client = _make_post_mock_client(400, err_resp, is_success=False)
    svc = make_models_service(client, "http://localhost:1234")

    with pytest.raises(UpstreamModelError) as exc_info:
        await svc.load_model("")

    err = exc_info.value
    assert err.status_code == 400
    assert err.error_type == "invalid_request"
    assert err.code == "missing_required_parameter"
    assert err.param == "model"


async def test_load_model_upstream_5xx_raises_gateway_error() -> None:
    """load_model raises UpstreamGatewayError on 503."""
    err_resp: dict[str, Any] = {"error": {"message": "upstream error"}}
    client = _make_post_mock_client(503, err_resp, is_success=False)
    svc = make_models_service(client, "http://localhost:1234")

    with pytest.raises(UpstreamGatewayError) as exc_info:
        await svc.load_model("some-model")

    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# P11b lifecycle: unload_instance
# ---------------------------------------------------------------------------


async def test_unload_instance_success() -> None:
    """unload_instance returns ModelUnloadResult on 200."""
    unload_resp: dict[str, Any] = {"instance_id": "qwen3-8b"}
    client = _make_post_mock_client(200, unload_resp)
    svc = make_models_service(client, "http://localhost:1234")

    result = await svc.unload_instance("qwen3-8b")

    assert isinstance(result, ModelUnloadResult)
    assert result.instance_id == "qwen3-8b"


async def test_unload_instance_upstream_4xx_raises_upstream_model_error() -> None:
    """unload_instance raises UpstreamModelError on 404."""
    err_resp: dict[str, Any] = {
        "error": {
            "type": "model_not_found",
            "message": "Model with instance identifier 'x' is not loaded.",
        }
    }
    client = _make_post_mock_client(404, err_resp, is_success=False)
    svc = make_models_service(client, "http://localhost:1234")

    with pytest.raises(UpstreamModelError) as exc_info:
        await svc.unload_instance("nonexistent-instance")

    assert exc_info.value.status_code == 404
    assert exc_info.value.error_type == "model_not_found"


# ---------------------------------------------------------------------------
# P11b lifecycle: unload_all_instances
# ---------------------------------------------------------------------------


async def test_unload_all_instances_calls_unload_for_each() -> None:
    """unload_all_instances returns UnloadAllResult with both succeeded."""
    # Set up a GET /api/v1/models response with 2 instances.
    model_with_two_instances: dict[str, Any] = {
        "key": "qwen3-8b",
        "type": "llm",
        "loaded_instances": [
            {"id": "qwen3-8b", "config": {}},
            {"id": "qwen3-8b:2", "config": {}},
        ],
        "capabilities": {"vision": True, "trained_for_tool_use": True},
    }
    models_resp: dict[str, Any] = {"models": [model_with_two_instances]}

    unload_resp1: dict[str, Any] = {"instance_id": "qwen3-8b"}
    unload_resp2: dict[str, Any] = {"instance_id": "qwen3-8b:2"}

    get_response = MagicMock(spec=httpx.Response)
    get_response.raise_for_status = MagicMock(return_value=None)
    get_response.json = MagicMock(return_value=models_resp)
    get_response.status_code = 200

    post_resp1 = MagicMock(spec=httpx.Response)
    post_resp1.status_code = 200
    post_resp1.is_success = True
    post_resp1.json = MagicMock(return_value=unload_resp1)
    post_resp1.content = b"{}"

    post_resp2 = MagicMock(spec=httpx.Response)
    post_resp2.status_code = 200
    post_resp2.is_success = True
    post_resp2.json = MagicMock(return_value=unload_resp2)
    post_resp2.content = b"{}"

    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=get_response)
    client.post = AsyncMock(side_effect=[post_resp1, post_resp2])

    svc = make_models_service(client, "http://localhost:1234")
    result = await svc.unload_all_instances("qwen3-8b")

    assert isinstance(result, UnloadAllResult)
    assert len(result.succeeded) == 2
    assert len(result.failed) == 0
    assert "qwen3-8b" in result.succeeded
    assert "qwen3-8b:2" in result.succeeded


async def test_unload_all_instances_empty_when_not_loaded(svc: ModelsService) -> None:
    """unload_all_instances returns empty UnloadAllResult when no instances are loaded."""
    result = await svc.unload_all_instances("deepseek-r1-7b-gguf")
    assert isinstance(result, UnloadAllResult)
    assert result.succeeded == []
    assert result.failed == []


async def test_unload_all_instances_best_effort_on_mid_batch_failure() -> None:
    """unload_all_instances continues past a failed instance and records both lists."""
    model_with_two_instances: dict[str, Any] = {
        "key": "qwen3-8b",
        "type": "llm",
        "loaded_instances": [
            {"id": "qwen3-8b", "config": {}},
            {"id": "qwen3-8b:2", "config": {}},
        ],
        "capabilities": {"vision": True, "trained_for_tool_use": True},
    }
    models_resp: dict[str, Any] = {"models": [model_with_two_instances]}

    get_response = MagicMock(spec=httpx.Response)
    get_response.raise_for_status = MagicMock(return_value=None)
    get_response.json = MagicMock(return_value=models_resp)
    get_response.status_code = 200

    # First POST succeeds; second raises UpstreamModelError (e.g. already unloaded).
    post_ok = MagicMock(spec=httpx.Response)
    post_ok.status_code = 200
    post_ok.is_success = True
    post_ok.json = MagicMock(return_value={"instance_id": "qwen3-8b"})
    post_ok.content = b"{}"

    post_err = MagicMock(spec=httpx.Response)
    post_err.status_code = 404
    post_err.is_success = False
    post_err.json = MagicMock(
        return_value={"error": {"type": "model_not_found", "message": "not loaded"}}
    )
    post_err.content = b"{}"
    post_err.text = "not loaded"

    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=get_response)
    client.post = AsyncMock(side_effect=[post_ok, post_err])

    svc = make_models_service(client, "http://localhost:1234")
    result = await svc.unload_all_instances("qwen3-8b")

    assert isinstance(result, UnloadAllResult)
    assert result.succeeded == ["qwen3-8b"]
    assert len(result.failed) == 1
    assert result.failed[0][0] == "qwen3-8b:2"
    assert "not loaded" in result.failed[0][1]


# ---------------------------------------------------------------------------
# P11b lifecycle: download_model
# ---------------------------------------------------------------------------


async def test_download_model_success() -> None:
    """download_model returns ModelDownloadResult on 200."""
    dl_resp: dict[str, Any] = {"status": "ok"}
    client = _make_post_mock_client(200, dl_resp)
    svc = make_models_service(client, "http://localhost:1234")

    from lmchat.services.models_service import ModelDownloadResult

    result = await svc.download_model("bartowski/Phi-3.5-mini-instruct-GGUF/file.gguf")
    assert isinstance(result, ModelDownloadResult)
    assert result.status == "ok"


async def test_download_model_upstream_4xx_raises_upstream_model_error() -> None:
    """download_model raises UpstreamModelError on 404."""
    err_resp: dict[str, Any] = {
        "error": {
            "type": "model_not_found",
            "message": "hub-model not found",
        }
    }
    client = _make_post_mock_client(404, err_resp, is_success=False)
    svc = make_models_service(client, "http://localhost:1234")

    with pytest.raises(UpstreamModelError) as exc_info:
        await svc.download_model("nonexistent/hub/model")

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Regression: embedding model without capabilities block
# ---------------------------------------------------------------------------

# Live-observed shape for text-embedding-nomic-embed-text-v1.5.
# No 'capabilities' key — ModelInfo.capabilities must be Optional to accept.
_EMBEDDING_MODEL_NO_CAPS: dict[str, Any] = {
    "type": "embedding",
    "publisher": "nomic-ai",
    "key": "text-embedding-nomic-embed-text-v1.5",
    "display_name": "Nomic Embed Text v1.5",
    "size_bytes": 274_000_000,
    "format": "gguf",
    # No 'capabilities' key — intentionally absent.
}

_MODELS_WITH_EMBEDDING: dict[str, Any] = {
    "models": [_PLAIN_MODEL, _EMBEDDING_MODEL_NO_CAPS],
}


async def test_embedding_model_without_capabilities_parses_successfully() -> None:
    """list_loaded must accept models without a capabilities block.

    Regression guard: before the fix, ModelInfo.capabilities was required,
    so an embedding model response without that key raised a Pydantic
    ValidationError logged as models_service.model_parse_error, silently
    dropping the model from the cache.
    """
    client = _make_mock_client(_MODELS_WITH_EMBEDDING)
    svc = make_models_service(client, "http://localhost:1234")

    models = await svc.list_loaded()
    keys = [m.key for m in models]
    assert "text-embedding-nomic-embed-text-v1.5" in keys, (
        "Embedding model without capabilities block was silently dropped"
    )


async def test_embedding_model_capabilities_surfaces_embedding_flag() -> None:
    """ModelInfo.capabilities.embedding is True for type='embedding' entries.

    LM Studio's REST surface omits the capabilities block entirely for
    embedding models — see _EMBEDDING_MODEL_NO_CAPS. The probe normalizer
    derives `capabilities.embedding=True` from `type=='embedding'` so
    downstream consumers (frontend Memory page reindex dropdown, any
    caller that filters by capabilities.embedding) don't have to special-
    case the type field. Without this, the admin-facing reindex
    dropdown rendered empty even with a loaded embedding model.
    """
    client = _make_mock_client(_MODELS_WITH_EMBEDDING)
    svc = make_models_service(client, "http://localhost:1234")

    models = await svc.list_loaded()
    embed = next(m for m in models if m.key == "text-embedding-nomic-embed-text-v1.5")
    assert embed.capabilities is not None
    assert embed.capabilities.embedding is True
    # Other capability flags default to False — there's no signal from
    # LM Studio one way or the other; safe-by-default.
    assert embed.capabilities.vision is False
    assert embed.capabilities.trained_for_tool_use is False


async def test_get_capabilities_returns_default_for_embedding_model() -> None:
    """get_capabilities returns a safe default when capabilities is None.

    Callers of get_capabilities must receive a valid Capabilities object
    even for embedding models that don't surface a capabilities block.
    The default: vision=False, trained_for_tool_use=False, reasoning=None.
    """
    client = _make_mock_client(_MODELS_WITH_EMBEDDING)
    svc = make_models_service(client, "http://localhost:1234")

    caps = await svc.get_capabilities("text-embedding-nomic-embed-text-v1.5")
    assert isinstance(caps, Capabilities)
    assert caps.vision is False
    assert caps.trained_for_tool_use is False
    assert caps.reasoning is None


# ---------------------------------------------------------------------------
# Prometheus counter for dropped model rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_probe_dropped_counter_increments_on_malformed_row() -> None:
    """_probe_upstream increments lm_chat_models_probe_dropped_total on validation failure.

    A malformed model row (missing required 'key' field causes a Pydantic
    ValidationError inside _probe_upstream) must increment the counter with
    reason='validation_error'.  The valid rows in the same response are still
    parsed successfully and returned.
    """

    from lmchat.metrics import MODELS_PROBE_DROPPED

    # Build a response with one valid row and one malformed row.
    # ModelInfo.key is required (no default).  A row that is missing `key`
    # entirely triggers a Pydantic ValidationError ("Field required").
    malformed: dict[str, Any] = {
        # No 'key' field — required by ModelInfo → ValidationError
        "type": "llm",
        "publisher": "bad-publisher",
    }
    response_with_bad_row: dict[str, Any] = {
        "models": [_PLAIN_MODEL, malformed],
    }

    # Sample the counter BEFORE the probe so the delta is unambiguous.
    before = MODELS_PROBE_DROPPED.labels(reason="validation_error")._value.get()

    client = _make_mock_client(response_with_bad_row)
    svc = make_models_service(client, "http://localhost:1234")
    await svc.refresh()

    after = MODELS_PROBE_DROPPED.labels(reason="validation_error")._value.get()

    # The counter must have increased by exactly 1.
    assert after - before == 1.0

    # The valid row was still parsed.
    models = await svc.list_loaded()
    assert len(models) == 1
    assert models[0].key == _PLAIN_MODEL["key"]


# ---------------------------------------------------------------------------
# Probe upstream shape handling (models / data / both / neither)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response_shape", "expected_keys"),
    [
        # Native shape: {"models": [...]}
        ({"models": [_PLAIN_MODEL]}, ["qwen3-8b"]),
        # Compat shape: {"data": [...]} — accepted as fallback.
        ({"data": [_PLAIN_MODEL]}, ["qwen3-8b"]),
        # Both keys: native wins (models is preferred).
        (
            {"models": [_PLAIN_MODEL], "data": [_REASONING_MODEL]},
            ["qwen3-8b"],
        ),
    ],
)
async def test_probe_upstream_shape_handling(
    response_shape: dict[str, Any], expected_keys: list[str]
) -> None:
    """_probe_upstream accepts both native {models:[]} and compat {data:[]} shapes.

    When both keys are present the native ``models`` key wins (it is the
    documented native shape; ``data`` is only a fallback for compat proxies).
    """
    client = _make_mock_client(response_shape)
    svc = make_models_service(client, "http://localhost:1234")
    models = await svc.list_loaded()
    assert [m.key for m in models] == expected_keys


async def test_probe_upstream_unexpected_shape_warns_and_returns_empty() -> None:
    """When neither ``models`` nor ``data`` is present, log a warning + empty result.

    The probe must not crash on an unexpected upstream shape; it should
    return an empty model list and emit a structured warning bound with the
    observed keys so an admin can debug.
    """
    from unittest.mock import patch as _patch

    client = _make_mock_client({"unexpected_key": []})
    svc = make_models_service(client, "http://localhost:1234")

    # Capture structlog bound logger calls directly — avoids the
    # structlog-config / caplog / capsys interaction issues that vary
    # depending on which other tests configure logging earlier in the
    # session.
    from lmchat.services import models_service as _mod

    with _patch.object(_mod.log, "warning") as warn_mock:
        models = await svc.list_loaded()

    assert models == []
    warn_calls = [call for call in warn_mock.call_args_list]
    assert any(
        call.args and call.args[0] == "models_service.upstream_shape_unexpected"
        for call in warn_calls
    ), f"expected upstream_shape_unexpected warning; got {warn_calls!r}"
    # The warning carries the observed keys for admin debugging.
    matching = next(
        call for call in warn_calls
        if call.args and call.args[0] == "models_service.upstream_shape_unexpected"
    )
    assert matching.kwargs.get("keys") == ["unexpected_key"]


# ---------------------------------------------------------------------------
# resolve_to_loaded_or_fallback (stranded-research / dead-key fix)
# ---------------------------------------------------------------------------


def _mi(
    key: str,
    *,
    loaded: bool = True,
    type_: str = "llm",
    instance: str | None = None,
) -> ModelInfo:
    """Build a ModelInfo with or without a live loaded instance."""
    ids = [instance or f"{key}-i1"] if loaded else []
    return ModelInfo(
        key=key,
        type=type_,
        loaded_instances=len(ids),
        loaded_instance_ids=ids,
    )


def _svc_with(models: list[ModelInfo]) -> ModelsService:
    """A ModelsService whose list_loaded yields a controlled model list."""
    svc = make_models_service(
        _make_mock_client(_MODELS_RESPONSE), "http://localhost:1234"
    )
    svc.list_loaded = AsyncMock(return_value=models)  # type: ignore[method-assign]
    return svc


@pytest.mark.asyncio
async def test_fallback_passthrough_when_loaded_instance_id_given() -> None:
    svc = _svc_with([_mi("model-a", instance="model-a-i1")])
    res = await svc.resolve_to_loaded_or_fallback("model-a-i1")
    assert isinstance(res, ResolvedModel)
    assert res.wire_id == "model-a-i1"
    assert res.substituted is False


@pytest.mark.asyncio
async def test_fallback_resolves_loaded_key_to_instance() -> None:
    svc = _svc_with([_mi("model-a", instance="model-a-i1")])
    res = await svc.resolve_to_loaded_or_fallback("model-a")
    assert res.wire_id == "model-a-i1"
    assert res.substituted is False


@pytest.mark.asyncio
async def test_fallback_substitutes_when_requested_unloaded() -> None:
    # 'pinned' is in catalog but has NO loaded instance (the reported bug).
    svc = _svc_with([_mi("pinned", loaded=False), _mi("other", instance="other-i1")])
    res = await svc.resolve_to_loaded_or_fallback("pinned")
    assert res.wire_id == "other-i1"
    assert res.substituted is True
    assert res.fallback_key == "other"
    assert res.reason == "requested_not_loaded"


@pytest.mark.asyncio
async def test_fallback_substitutes_when_requested_absent_from_catalog() -> None:
    svc = _svc_with([_mi("other", instance="other-i1")])
    res = await svc.resolve_to_loaded_or_fallback("ghost-model")
    assert res.wire_id == "other-i1"
    assert res.substituted is True
    assert res.fallback_key == "other"


@pytest.mark.asyncio
async def test_fallback_prefers_prefer_key_when_loaded() -> None:
    svc = _svc_with(
        [_mi("first-llm", instance="first-i1"), _mi("preferred", instance="pref-i1")]
    )
    res = await svc.resolve_to_loaded_or_fallback("unloaded", prefer_key="preferred")
    assert res.wire_id == "pref-i1"
    assert res.fallback_key == "preferred"
    assert res.substituted is True


@pytest.mark.asyncio
async def test_fallback_ignores_embedding_models() -> None:
    svc = _svc_with(
        [
            _mi("embed", type_="embedding", instance="embed-i1"),
            _mi("llm", instance="llm-i1"),
        ]
    )
    res = await svc.resolve_to_loaded_or_fallback("unloaded")
    assert res.wire_id == "llm-i1"
    assert res.fallback_key == "llm"


@pytest.mark.asyncio
async def test_fallback_none_when_no_models_loaded() -> None:
    svc = _svc_with([])
    res = await svc.resolve_to_loaded_or_fallback("anything")
    assert res.wire_id is None
    assert res.reason == "no_models_loaded"


@pytest.mark.asyncio
async def test_fallback_none_when_only_embeddings_loaded() -> None:
    svc = _svc_with([_mi("embed", type_="embedding", instance="embed-i1")])
    res = await svc.resolve_to_loaded_or_fallback("anything")
    assert res.wire_id is None
    assert res.reason == "only_non_llm_loaded"


@pytest.mark.asyncio
async def test_fallback_uses_instance_list_not_int_count() -> None:
    """A stale cache can show loaded_instances>0 with an empty
    loaded_instance_ids list; the resolver must treat the empty LIST as
    'not loaded' and fall back rather than ship a non-loaded key."""
    stale = ModelInfo(
        key="stale", type="llm", loaded_instances=1, loaded_instance_ids=[]
    )
    svc = _svc_with([stale, _mi("good", instance="good-i1")])
    res = await svc.resolve_to_loaded_or_fallback("stale")
    assert res.wire_id == "good-i1"
    assert res.substituted is True
    assert res.fallback_key == "good"


# ---------------------------------------------------------------------------
# resolve_embedding_wire_id (embedding @quant-suffix 400 fix)
#
# EXACT / PREFIX / PASSTHROUGH only — NO cross-model fallback (a nomic query
# must never resolve to bge-m3, which would corrupt the vector space for
# per-chunk re-embedding). Distinct from resolve_to_loaded_or_fallback (which
# DOES cross-fallback for CHAT models) and from resolve_embedding_model_status
# (which cross-falls-back for SELECTING the write model).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_wire_passthrough_when_instance_id_given() -> None:
    """Caller already holds a loaded embedding instance id → returned as-is."""
    svc = _svc_with(
        [_mi("nomic", type_="embedding", instance="nomic@q8_0")]
    )
    res = await svc.resolve_embedding_wire_id("nomic@q8_0")
    assert res == "nomic@q8_0"


@pytest.mark.asyncio
async def test_embed_wire_exact_key_resolves_to_instance() -> None:
    """Bare catalog key → loaded_instance_ids[0] (the @quant wire id)."""
    svc = _svc_with(
        [_mi("nomic", type_="embedding", instance="nomic@q8_0")]
    )
    res = await svc.resolve_embedding_wire_id("nomic")
    assert res == "nomic@q8_0"


@pytest.mark.asyncio
async def test_embed_wire_prefix_resolves_quant_suffix() -> None:
    """Configured key is a prefix of a loaded instance id via '@<quant>'.

    This is the real-world shape: the stored/configured key is
    ``text-embedding-nomic-embed-text-v1.5`` but LM Studio's catalog ``key``
    AND the loaded instance differ only by the @quant suffix. Here the model's
    catalog key does NOT exactly match the configured key (simulating a stored
    row written before the catalog key included/excluded the suffix), so only
    the prefix branch can resolve it.
    """
    svc = _svc_with(
        [
            _mi(
                "text-embedding-nomic-embed-text-v1.5@q8_0",
                type_="embedding",
                instance="text-embedding-nomic-embed-text-v1.5@q8_0",
            )
        ]
    )
    res = await svc.resolve_embedding_wire_id(
        "text-embedding-nomic-embed-text-v1.5"
    )
    assert res == "text-embedding-nomic-embed-text-v1.5@q8_0"


@pytest.mark.asyncio
async def test_embed_wire_prefix_guard_rejects_wrong_family() -> None:
    """The '@' guard prevents a prefix from matching a different family.

    ``nomic`` must NOT match ``nomic-large@q8_0`` — only ``nomic@<quant>``.
    With no exact/prefix match and NO cross-model fallback, the result is None.
    """
    svc = _svc_with(
        [
            _mi(
                "nomic-large",
                type_="embedding",
                instance="nomic-large@q8_0",
            )
        ]
    )
    res = await svc.resolve_embedding_wire_id("nomic")
    assert res is None


@pytest.mark.asyncio
async def test_embed_wire_no_cross_model_fallback() -> None:
    """Requested embedder absent → None, never another embedder's instance.

    A ``nomic`` request with only ``bge-m3`` loaded must return None — NOT
    ``bge-m3@...``. Cross-model substitution would compare the query against
    nomic-written chunks in bge-m3's vector space.
    """
    svc = _svc_with(
        [_mi("bge-m3", type_="embedding", instance="bge-m3@f16")]
    )
    res = await svc.resolve_embedding_wire_id("nomic")
    assert res is None


@pytest.mark.asyncio
async def test_embed_wire_ignores_llm_models() -> None:
    """Only embedding-type models are considered for embedding-wire resolution."""
    svc = _svc_with(
        [_mi("nomic", type_="llm", instance="nomic@q8_0")]  # wrong type
    )
    res = await svc.resolve_embedding_wire_id("nomic")
    assert res is None


@pytest.mark.asyncio
async def test_embed_wire_none_when_nothing_loaded() -> None:
    """No embedding model loaded → None (caller keeps degrade/raise behavior)."""
    svc = _svc_with([])
    res = await svc.resolve_embedding_wire_id("nomic")
    assert res is None


@pytest.mark.asyncio
async def test_resolve_embedding_wire_id_strips_stale_quant_to_bare_loaded() -> None:
    """A row stored under a stale @quant suffix must resolve to the
    bare loaded instance when the quant variant is no longer loaded.

    Mirrors resolve_embedding_model_status's reverse @-strip fallback
    (branch 4): quants of the SAME embedding model share output dimensions,
    so falling back from a stale ``…@q8_0`` to the bare ``…v1.5`` loaded
    instance is dimension-safe and prevents a whole-corpus recall/retrieval
    failure (memory_hits=0) when LM Studio reloads the model unsuffixed.
    """
    svc = _svc_with(
        [
            _mi(
                "text-embedding-nomic-embed-text-v1.5",
                type_="embedding",
                instance="text-embedding-nomic-embed-text-v1.5",
            )
        ]
    )
    res = await svc.resolve_embedding_wire_id(
        "text-embedding-nomic-embed-text-v1.5@q8_0"
    )
    assert res == "text-embedding-nomic-embed-text-v1.5"


@pytest.mark.asyncio
async def test_resolve_embedding_wire_id_bare_fallback_never_crosses_family() -> None:
    """The bare-fallback branch must NEVER cross model families.

    A stale-quant ``nomic@q8_0`` request with only ``bge-m3`` loaded must
    still return None — never ``bge-m3@...`` — because comparing the query
    against nomic-written vectors in bge-m3's vector space would corrupt
    the cosine comparison.
    """
    svc = _svc_with([_mi("bge-m3", type_="embedding", instance="bge-m3@f16")])
    res = await svc.resolve_embedding_wire_id("nomic@q8_0")
    assert res is None


# ---------------------------------------------------------------------------
# TTL-guarded loaded-set re-probe (stale-loaded-cache / silent-stream-death fix)
# ---------------------------------------------------------------------------
#
# Root cause: resolve_to_loaded_or_fallback read list_loaded() which returned
# the 30-minute-interval cache with no TTL guard.  When a model was unloaded
# externally (user action in LM Studio UI or idle-TTL eviction) the cache
# continued to show it as loaded, so the resolver shipped a dead instance id
# to LM Studio which JIT-reloaded / stalled.  The clean "model not loaded"
# fast-fail in streaming_service never fired.
#
# Fix: _refresh_if_loaded_cache_stale() is called at the top of
# resolve_to_loaded_or_fallback.  It re-probes upstream via refresh() when
# the cache is older than _loaded_models_ttl (default 5 s).
#
# These tests:
#   1. Verify the probe is NOT called when the cache is still fresh.
#   2. Verify the probe IS called and the result reflects the new state after
#      the TTL elapses (model now unloaded → substituted=True).
#   3. Verify the auth-failed backoff window suppresses the re-probe even
#      when the TTL has elapsed.
# ---------------------------------------------------------------------------


def _build_models_response(
    *,
    loaded: bool,
) -> dict:
    """Build a /api/v1/models-shaped dict with 'pinned' LOADED or UNLOADED."""
    if loaded:
        return {
            "models": [
                {
                    "key": "pinned",
                    "type": "llm",
                    "loaded_instances": [{"id": "pinned-i1", "config": {"context_length": 4096}}],
                    "maxContextLength": 4096,
                    "capabilities": {"vision": False, "trained_for_tool_use": True},
                },
                {
                    "key": "other",
                    "type": "llm",
                    "loaded_instances": [{"id": "other-i1", "config": {"context_length": 4096}}],
                    "maxContextLength": 4096,
                    "capabilities": {"vision": False, "trained_for_tool_use": True},
                },
            ],
        }
    else:
        return {
            "models": [
                {
                    "key": "pinned",
                    "type": "llm",
                    "loaded_instances": [],  # externally unloaded
                    "maxContextLength": 4096,
                    "capabilities": {"vision": False, "trained_for_tool_use": True},
                },
                {
                    "key": "other",
                    "type": "llm",
                    "loaded_instances": [{"id": "other-i1", "config": {"context_length": 4096}}],
                    "maxContextLength": 4096,
                    "capabilities": {"vision": False, "trained_for_tool_use": True},
                },
            ],
        }


@pytest.mark.asyncio
async def test_resolve_does_not_reprobe_within_ttl(
    mock_client: httpx.AsyncClient,
) -> None:
    """No upstream probe is triggered when the cache is fresher than the TTL.

    The first resolve call populates the cache (1 probe).  A second call made
    before the TTL expires must NOT trigger another probe — the call count
    stays at 1.
    """
    from unittest.mock import patch

    # Initial response: 'pinned' loaded.
    mock_client.get.return_value.json.return_value = _build_models_response(loaded=True)  # type: ignore[attr-defined]

    svc = make_models_service(
        mock_client,
        "http://localhost:1234",
        loaded_models_ttl=10.0,  # 10-second TTL
    )

    # Monotonic clock starts at t=0.
    fake_time = 0.0

    with patch("lmchat.services.models_service.time") as mock_time:
        mock_time.monotonic.side_effect = lambda: fake_time

        # First resolve: cold cache → triggers refresh() (1 probe).
        res1 = await svc.resolve_to_loaded_or_fallback("pinned")
        assert res1.wire_id == "pinned-i1"
        assert not res1.substituted

        # Advance clock by 4 s — still within the 10-s TTL.
        fake_time = 4.0

        # Second resolve: cache is still fresh → NO additional probe.
        res2 = await svc.resolve_to_loaded_or_fallback("pinned")
        assert res2.wire_id == "pinned-i1"
        assert not res2.substituted

    # Exactly 1 upstream GET (the initial cold-cache probe).
    assert mock_client.get.call_count == 1, (  # type: ignore[attr-defined]
        f"Expected 1 upstream probe within TTL; got {mock_client.get.call_count}"  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_resolve_reprobes_after_ttl_and_detects_unload(
    mock_client: httpx.AsyncClient,
) -> None:
    """After the TTL elapses, resolve re-probes and returns substituted=True
    when the model has been unloaded externally.

    Sequence:
      t=0   first resolve  → probe #1 → 'pinned' LOADED  → wire_id='pinned-i1'
      t=6   second resolve → TTL expired → probe #2 → 'pinned' UNLOADED
                          → substituted=True, fallback to 'other'
    """
    from unittest.mock import patch

    # First probe: 'pinned' is loaded.
    loaded_response = _build_models_response(loaded=True)
    # Second probe: 'pinned' has been externally unloaded.
    unloaded_response = _build_models_response(loaded=False)

    mock_client.get.return_value.json.side_effect = [  # type: ignore[attr-defined]
        loaded_response,
        unloaded_response,
    ]

    svc = make_models_service(
        mock_client,
        "http://localhost:1234",
        loaded_models_ttl=5.0,  # 5-second TTL
    )

    fake_time = 0.0

    with patch("lmchat.services.models_service.time") as mock_time:
        mock_time.monotonic.side_effect = lambda: fake_time

        # First resolve at t=0: cold cache → probe #1 → pinned loaded.
        res1 = await svc.resolve_to_loaded_or_fallback("pinned")
        assert res1.wire_id == "pinned-i1", f"Expected pinned-i1, got {res1.wire_id}"
        assert not res1.substituted

        # Advance clock by 6 s — past the 5-s TTL.
        fake_time = 6.0

        # Second resolve at t=6: TTL expired → probe #2 → pinned now UNLOADED.
        res2 = await svc.resolve_to_loaded_or_fallback("pinned")

    # The resolver must have fired a second upstream probe.
    assert mock_client.get.call_count == 2, (  # type: ignore[attr-defined]
        f"Expected 2 upstream probes (initial + TTL re-probe); got {mock_client.get.call_count}"  # type: ignore[attr-defined]
    )
    # 'pinned' is now unloaded → substituted to 'other'.
    assert res2.substituted is True, "Expected substituted=True after model was externally unloaded"
    assert res2.fallback_key == "other"
    assert res2.wire_id == "other-i1"
    assert res2.reason == "requested_not_loaded"


@pytest.mark.asyncio
async def test_resolve_ttl_reprobe_skipped_during_auth_backoff(
    mock_client: httpx.AsyncClient,
) -> None:
    """TTL-triggered re-probe is suppressed while in the 401 backoff window.

    Even if the TTL has elapsed, _refresh_if_loaded_cache_stale must NOT
    call refresh() when _auth_failed is True and the backoff window has not
    expired.  This prevents a cascade of 401s from streaming turns that all
    hit the resolve path concurrently.
    """
    from unittest.mock import patch

    from lmchat.services.models_service import _AUTH_FAILED_BACKOFF_SEC

    mock_client.get.return_value.json.return_value = _build_models_response(loaded=True)  # type: ignore[attr-defined]

    svc = make_models_service(
        mock_client,
        "http://localhost:1234",
        loaded_models_ttl=5.0,
    )

    fake_time = 0.0

    with patch("lmchat.services.models_service.time") as mock_time:
        mock_time.monotonic.side_effect = lambda: fake_time

        # Warm the cache at t=0 (probe #1).
        await svc.refresh()

        # Simulate a 401: set auth_failed + record the failure timestamp.
        svc._auth_failed = True
        svc._auth_failed_at = fake_time  # backoff started at t=0

        # Advance clock past the TTL but WITHIN the 401 backoff window.
        fake_time = 10.0  # > TTL (5 s) but < _AUTH_FAILED_BACKOFF_SEC (60 s)
        assert 10.0 < _AUTH_FAILED_BACKOFF_SEC, "Sanity: still inside backoff window"

        # resolve_to_loaded_or_fallback must NOT trigger another probe.
        await svc.resolve_to_loaded_or_fallback("pinned")

    # Only the initial refresh() call should have produced an upstream GET.
    assert mock_client.get.call_count == 1, (  # type: ignore[attr-defined]
        f"Expected 1 upstream probe (backoff should suppress TTL re-probe); "
        f"got {mock_client.get.call_count}"  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# TTL-path probe-storm dedup (concurrent stale-cache callers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_stale_reprobe_dedup_concurrent_callers(
    mock_client: httpx.AsyncClient,
) -> None:
    """N concurrent resolve_to_loaded_or_fallback() calls that all see a
    stale cache must trigger EXACTLY ONE upstream probe (not N).

    The _stale_reprobe_lock + double-check pattern mirrors force_refresh()'s
    in-flight guard but uses an asyncio.Lock to serialize late callers and let
    them skip the reprobe once the first caller has refreshed.

    Red-on-revert: removing the double-check inside _stale_reprobe_lock causes
    every concurrent caller to probe → call_count == N.
    """
    import asyncio
    from unittest.mock import patch

    # BARRIER: a plain AsyncMock can't reproduce the race — by the time caller 2
    # reads the fast-path, caller 1 may already have refreshed, so the fast-path
    # (not the double-check) dedups and the test passes even with the double-
    # check removed (false-green). We HOLD the first reprobe until all N callers
    # have passed the (stale) fast-path and queued on _stale_reprobe_lock; only
    # then does removing the double-check make every late caller probe.
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status = MagicMock(return_value=None)
    response.json = MagicMock(return_value=_build_models_response(loaded=True))
    response.status_code = 200

    get_calls = 0
    first_probe_entered = asyncio.Event()
    release_first_probe = asyncio.Event()

    async def _gated_get(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal get_calls
        get_calls += 1
        # get #1 is the warm refresh below; the first REPROBE is get #2 — hold it
        # in flight (caller 1 owns the lock) so callers 2..N queue on the lock.
        if get_calls == 2:
            first_probe_entered.set()
            await release_first_probe.wait()
        return response

    mock_client.get = AsyncMock(side_effect=_gated_get)  # type: ignore[method-assign]

    svc = make_models_service(
        mock_client,
        "http://localhost:1234",
        loaded_models_ttl=5.0,
    )

    fake_time = 0.0

    with patch("lmchat.services.models_service.time") as mock_time:
        mock_time.monotonic.side_effect = lambda: fake_time

        # Warm the cache at t=0 (get #1). refresh() sets _cache_timestamp AFTER
        # the probe, so while the first reprobe is held below the timestamp is
        # still 0 and every concurrent caller sees a stale fast-path.
        await svc.refresh()
        assert get_calls == 1

        fake_time = 10.0  # > 5 s TTL — stale

        N = 8
        tasks = [
            asyncio.create_task(svc.resolve_to_loaded_or_fallback("pinned"))
            for _ in range(N)
        ]
        # Wait until the first reprobe is in flight (caller 1 holds the lock),
        # then give callers 2..N a tick to pass the fast-path + queue on the lock.
        await first_probe_entered.wait()
        await asyncio.sleep(0.05)
        # Release: caller 1's refresh completes (sets _cache_timestamp=10); late
        # callers acquire the lock and the double-check (10-10<5) makes them skip.
        release_first_probe.set()
        results = await asyncio.gather(*tasks)

    # Exactly 2 gets: the warm + ONE dedup'd reprobe. >2 ⇒ the double-check did
    # not collapse the concurrent late callers (the regression).
    assert get_calls == 2, (
        f"Expected exactly 2 gets (warm + one dedup'd reprobe); got {get_calls}. "
        f">2 means concurrent late callers each probed (double-check not working)."
    )
    for res in results:
        assert res.wire_id is not None, "Expected a resolved wire_id for all callers"


@pytest.mark.asyncio
async def test_ttl_stale_reprobe_dedup_fast_path_still_lock_free(
    mock_client: httpx.AsyncClient,
) -> None:
    """The fast path (cache is fresh) must NOT acquire _stale_reprobe_lock.

    A fresh-cache call must return without contending on the storm-dedup lock,
    so it isn't serialised behind in-flight probes from the stale path.
    """
    from unittest.mock import patch

    mock_client.get.return_value.json.return_value = _build_models_response(loaded=True)  # type: ignore[attr-defined]

    svc = make_models_service(
        mock_client,
        "http://localhost:1234",
        loaded_models_ttl=10.0,
    )

    fake_time = 0.0

    with patch("lmchat.services.models_service.time") as mock_time:
        mock_time.monotonic.side_effect = lambda: fake_time

        # Warm the cache.
        await svc.refresh()

        # Simulate the lock being held by a concurrent stale-path caller.
        # The fast path must NOT block on it.
        await svc._stale_reprobe_lock.acquire()
        try:
            # Fresh cache — fast path should return immediately (no lock attempt).
            res = await svc.resolve_to_loaded_or_fallback("pinned")
        finally:
            svc._stale_reprobe_lock.release()

    # The fast path returned correctly without ever touching the lock.
    assert res.wire_id is not None
    # Only the initial warm probe happened.
    assert mock_client.get.call_count == 1  # type: ignore[attr-defined]
