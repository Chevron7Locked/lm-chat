# SPDX-License-Identifier: Apache-2.0
"""Tests for resolve_background_model_id — fail-soft background-task model selection.

The OUT-OF-BAND background tasks (auto-memory distillation, chat-title
generation, follow-up chips) use the admin-pinned background model instead
of the chat's model so they stop competing with the user's next turn. Unlike
the embedding resolver (which fails LOUD), this resolver FAILS SOFT: every
"can't honour the preference" path falls back to the chat model and NEVER
raises.

Branches covered:

1. preference UNSET → returns chat_model_id (default "Same as chat model").
2. preference SET + that model LOADED as an llm → returns the background model.
3. preference SET + NOT loaded → returns chat_model_id (fail-soft).
3b. preference SET pins a stale "@quant" whose bare family IS loaded under a
    DIFFERENT quant → reverse bare-@-strip fallback resolves to the bare
    family key (mirrors resolve_active_embedding_model_key's fallback);
    entirely-absent family still falls back to chat_model_id.
4. preference SET + only loaded as an EMBEDDING (not an llm) → chat_model_id.
5. models_service is None → chat_model_id (legacy/test path).
6. list_loaded() raises → chat_model_id (never propagates).
7. instance-id / "@quant" prefix match counts as loaded.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.config import Settings
from lmchat.db.schema import metadata
from lmchat.services.lm_studio_overrides_service import (
    LmStudioOverridesService,
)
from lmchat.services.models_service import (
    Capabilities,
    ModelInfo,
    ModelsService,
    resolve_background_model_id,
)

_CHAT_MODEL = "qwen3-8b"
_BG_MODEL = "small-llm-3b"
_SECRET = "test-secret-32-bytes-of-entropy!!"


def _settings() -> Settings:
    return Settings(lm_chat_secret=_SECRET)  # type: ignore[call-arg]


def _overrides_service(engine: AsyncEngine) -> LmStudioOverridesService:
    return LmStudioOverridesService(engine=engine, settings=_settings())


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema applied."""
    db_path = tmp_path / "test_background_resolver.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


def _make_llm(key: str, instance_ids: list[str] | None = None) -> ModelInfo:
    return ModelInfo(
        key=key,
        type="llm",
        capabilities=Capabilities(vision=False, trained_for_tool_use=True),
        loaded_instance_ids=instance_ids or [],
    )


def _make_embedding(key: str, instance_ids: list[str] | None = None) -> ModelInfo:
    return ModelInfo(
        key=key,
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        loaded_instance_ids=instance_ids or [],
    )


def _models_service(loaded: list[ModelInfo]) -> ModelsService:
    mock = AsyncMock(spec=ModelsService)
    mock.list_loaded.return_value = loaded
    return mock


async def _set_pref(engine: AsyncEngine, value: str | None) -> None:
    svc = _overrides_service(engine)
    await svc.set_preferred_background_model(value)


# ---------------------------------------------------------------------------
# Branch 1: preference UNSET → chat model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unset_returns_chat_model(engine: AsyncEngine) -> None:
    ms = _models_service([_make_llm(_BG_MODEL, [f"{_BG_MODEL}@q4"])])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    assert result == _CHAT_MODEL


# ---------------------------------------------------------------------------
# Branch 2: preference SET + loaded → background model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_loaded_returns_background_model(engine: AsyncEngine) -> None:
    await _set_pref(engine, _BG_MODEL)
    ms = _models_service([_make_llm(_BG_MODEL, [f"{_BG_MODEL}@q4"])])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    assert result == _BG_MODEL


@pytest.mark.asyncio
async def test_set_and_loaded_by_exact_key(engine: AsyncEngine) -> None:
    # No "@quant" suffix on the instance id — exact key match path.
    await _set_pref(engine, _BG_MODEL)
    ms = _models_service([_make_llm(_BG_MODEL, [_BG_MODEL])])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    assert result == _BG_MODEL


# ---------------------------------------------------------------------------
# Branch 3: preference SET but NOT loaded → fail-soft to chat model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_but_not_loaded_returns_chat_model(engine: AsyncEngine) -> None:
    await _set_pref(engine, _BG_MODEL)
    # Only a DIFFERENT llm is loaded.
    ms = _models_service([_make_llm("other-llm-7b", ["other-llm-7b@q4"])])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    assert result == _CHAT_MODEL


@pytest.mark.asyncio
async def test_set_but_unloaded_instance_returns_chat_model(
    engine: AsyncEngine,
) -> None:
    # Background model is in the catalog but has NO live instance.
    await _set_pref(engine, _BG_MODEL)
    ms = _models_service([_make_llm(_BG_MODEL, [])])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    assert result == _CHAT_MODEL


# ---------------------------------------------------------------------------
# Branch 3b: preferred pins a stale @quant, family loaded under a DIFFERENT
# quant → reverse bare-@-strip fallback resolves to the bare family key
# (mirrors resolve_active_embedding_model_key's fallback; regression for the
# divergence flagged 2026-07-14 — this resolver lacked it while the embedding
# resolver had it since eda0d82).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_stale_quant_falls_back_to_same_family_bare_key(
    engine: AsyncEngine,
) -> None:
    """Preferred pins bg-model@q4 but it's loaded under @q8 → resolves, not chat model.

    Before the fix, ``_is_loaded`` had no reverse fallback: a stale pinned
    quant matched nothing (exact key, instance id, and ``key + "@"`` prefix
    all miss when the SUFFIX itself is stale), so the resolver silently fell
    back to the chat model even though the intended background model's
    family was loaded — just under a different quant.
    """
    stale_pinned = f"{_BG_MODEL}@q4"
    await _set_pref(engine, stale_pinned)
    # Same family loaded, but under a DIFFERENT quant than the pin.
    ms = _models_service([_make_llm(_BG_MODEL, [f"{_BG_MODEL}@q8"])])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    # Returns the BARE family key (not the stale-quant string verbatim):
    # unlike the embedding wire-id resolver, resolve_to_loaded_or_fallback
    # (the downstream LLM wire-id resolver) has no family-aware fallback of
    # its own — it would substitute an unrelated loaded LLM if handed a
    # key it can't exact-match. The bare key hits its exact m.key match.
    assert result == _BG_MODEL


@pytest.mark.asyncio
async def test_set_stale_quant_still_falls_back_to_chat_model_when_family_absent(
    engine: AsyncEngine,
) -> None:
    """Stale @quant whose ENTIRE family is unloaded → still fail-soft to chat model.

    The reverse fallback is same-family ONLY. If the background model's
    family isn't loaded at all (only an unrelated llm is), a pinned
    ``bg-model@q4`` must NOT resolve to that unrelated model.
    """
    stale_pinned = f"{_BG_MODEL}@q4"
    await _set_pref(engine, stale_pinned)
    # Only a DIFFERENT family loaded; the background model's family is absent.
    ms = _models_service([_make_llm("other-llm-7b", ["other-llm-7b@q8"])])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    assert result == _CHAT_MODEL


# ---------------------------------------------------------------------------
# Branch 4: preference matches an EMBEDDING, not an llm → chat model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_matches_embedding_not_llm_returns_chat_model(
    engine: AsyncEngine,
) -> None:
    await _set_pref(engine, "nomic-embed-v1.5")
    ms = _models_service(
        [_make_embedding("nomic-embed-v1.5", ["nomic-embed-v1.5@q8_0"])]
    )
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    assert result == _CHAT_MODEL


# ---------------------------------------------------------------------------
# Branch 5/6: None service / list_loaded error → chat model (never raises)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_models_service_returns_chat_model(engine: AsyncEngine) -> None:
    await _set_pref(engine, _BG_MODEL)
    result = await resolve_background_model_id(
        engine=engine, models_service=None, chat_model_id=_CHAT_MODEL
    )
    assert result == _CHAT_MODEL


@pytest.mark.asyncio
async def test_list_loaded_error_returns_chat_model(engine: AsyncEngine) -> None:
    await _set_pref(engine, _BG_MODEL)
    ms = AsyncMock(spec=ModelsService)
    ms.list_loaded.side_effect = RuntimeError("upstream down")
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CHAT_MODEL
    )
    assert result == _CHAT_MODEL


# ---------------------------------------------------------------------------
# Service getter/setter round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_getter_setter_roundtrip(engine: AsyncEngine) -> None:
    svc = _overrides_service(engine)
    assert await svc.fetch_preferred_background_model() is None
    await svc.set_preferred_background_model(_BG_MODEL)
    assert await svc.fetch_preferred_background_model() == _BG_MODEL
    await svc.set_preferred_background_model(None)
    assert await svc.fetch_preferred_background_model() is None


# ---------------------------------------------------------------------------
# Branch 1b: preference UNSET, chat model is a coder → swap to non-coder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unset_coder_chat_model_swaps_to_noncoder(engine: AsyncEngine) -> None:
    """When no preference is set and the chat model is a coder model, the
    resolver returns the first loaded non-coder LLM instead — so auto-memory
    distillation doesn't land on a coding-specialist that returns []."""
    _CODER_CHAT = "qwen2.5-coder-7b"
    _NONCODER = "qwen3-8b"
    ms = _models_service([
        _make_llm(_NONCODER, [f"{_NONCODER}@q4"]),
        _make_llm(_CODER_CHAT, [f"{_CODER_CHAT}@q4"]),
    ])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CODER_CHAT
    )
    assert result == _NONCODER


@pytest.mark.asyncio
async def test_unset_embed_chat_model_swaps_to_noncoder(engine: AsyncEngine) -> None:
    """A chat model whose key contains 'embed' is treated like a coder model —
    the resolver swaps to the first eligible non-coder LLM."""
    _EMBED_CHAT = "nomic-embed-text-v1.5"
    _NONCODER = "llama-3-8b"
    ms = _models_service([
        _make_llm(_NONCODER, [f"{_NONCODER}@q4"]),
    ])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_EMBED_CHAT
    )
    assert result == _NONCODER


@pytest.mark.asyncio
async def test_unset_coder_chat_model_no_eligible_fallback(engine: AsyncEngine) -> None:
    """When no preference is set, chat model is a coder, and no non-coder LLM
    is loaded — fail-soft: returns the coder chat model (never raises)."""
    _CODER_CHAT = "deepseek-coder-7b"
    # Only the coder model and an embedding model are loaded; nothing eligible.
    ms = _models_service([
        _make_llm(_CODER_CHAT, [f"{_CODER_CHAT}@q4"]),
        _make_embedding("nomic-embed-v1.5", ["nomic-embed-v1.5@q8_0"]),
    ])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_CODER_CHAT
    )
    assert result == _CODER_CHAT


@pytest.mark.asyncio
async def test_unset_noncoder_chat_model_not_swapped(engine: AsyncEngine) -> None:
    """When no preference is set and the chat model is NOT a coder/embed model,
    it is returned as-is regardless of what other models are loaded."""
    _NONCODER_CHAT = "llama-3-70b"
    _OTHER = "small-llm-3b"
    ms = _models_service([
        _make_llm(_OTHER, [f"{_OTHER}@q4"]),
        _make_llm(_NONCODER_CHAT, [f"{_NONCODER_CHAT}@q4"]),
    ])
    result = await resolve_background_model_id(
        engine=engine, models_service=ms, chat_model_id=_NONCODER_CHAT
    )
    # Non-coder chat model: return it directly, don't swap.
    assert result == _NONCODER_CHAT
