# SPDX-License-Identifier: Apache-2.0
"""Tests for resolve_active_embedding_model_key — deterministic embedding model selection.

Covers the four resolution branches of the module-level resolver:

1. preferred set + loaded → returns preferred (deterministic, stable).
2. preferred set + NOT loaded → raises NoEmbeddingModelLoadedError (fail-loud).
3. preferred UNSET + multiple embedders loaded → deterministically picks the
   canonical default (nomic, DEFAULT_EMBEDDING_MODEL_KEY) when it is loaded —
   NOT lexicographic-first (which would pick the different-dimension bge-m3) —
   persists it, and returns the SAME key on repeated calls (determinism test).
3b. preferred UNSET + default (nomic) NOT loaded → raises
    NoEmbeddingModelLoadedError (fail-loud; does NOT silently pick bge).
4. preferred UNSET + NO embedder loaded → raises NoEmbeddingModelLoadedError.

Also tests MemoryService.resolve_active_embedding_model_key as a delegation wrapper.
"""
from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata, server_lm_studio_default
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import (
    MemoryService,
    NoEmbeddingModelLoadedError,
    resolve_active_embedding_model_key,
)
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema applied."""
    db_path = tmp_path / "test_embedding_resolver.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


def _make_embedding_model(key: str, instance_ids: list[str] | None = None) -> ModelInfo:
    """Return a ModelInfo stub for an embedding model."""
    return ModelInfo(
        key=key,
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        loaded_instance_ids=instance_ids or [],
    )


def _make_models_service(embedding_keys: list[str]) -> ModelsService:
    """Return a mocked ModelsService that reports *embedding_keys* as loaded."""
    mock = AsyncMock(spec=ModelsService)
    mock.list_loaded.return_value = [
        _make_embedding_model(k, instance_ids=[f"{k}@q8_0"])
        for k in embedding_keys
    ]
    return mock


async def _read_preferred(engine: AsyncEngine) -> str | None:
    """Read the persisted preferred_embedding_model_id from the DB."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(server_lm_studio_default.c.preferred_embedding_model_id)
                .where(server_lm_studio_default.c.id == 1)
            )
        ).first()
    if row is None:
        return None
    return row.preferred_embedding_model_id


# ---------------------------------------------------------------------------
# Branch 1: preferred set + loaded → returns preferred
# ---------------------------------------------------------------------------


async def test_resolver_returns_preferred_when_loaded(engine: AsyncEngine) -> None:
    """When a preference is stored and the model is loaded, return the preference."""
    # Write a preference row.
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                preferred_embedding_model_id="text-embedding-nomic-embed-text-v1.5",
            )
        )

    # Only nomic is loaded.
    models_service = _make_models_service(["text-embedding-nomic-embed-text-v1.5"])

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service,
        persist_default=False,
    )
    assert result == "text-embedding-nomic-embed-text-v1.5"


async def test_resolver_preferred_returned_even_when_other_model_also_loaded(
    engine: AsyncEngine,
) -> None:
    """Preferred key wins even when a second embedder is loaded."""
    # Store nomic as preferred; bge-m3 also loaded.
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                preferred_embedding_model_id="text-embedding-nomic-embed-text-v1.5",
            )
        )

    # Both are loaded; bge-m3 sorts BEFORE nomic lexicographically.
    models_service = _make_models_service([
        "text-embedding-bge-m3",
        "text-embedding-nomic-embed-text-v1.5",
    ])

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service,
        persist_default=False,
    )
    # Must return the STORED preference, not the lexicographic first.
    assert result == "text-embedding-nomic-embed-text-v1.5"


# ---------------------------------------------------------------------------
# Branch 2: preferred set + NOT loaded → fail loud
# ---------------------------------------------------------------------------


async def test_resolver_raises_when_preferred_not_loaded(engine: AsyncEngine) -> None:
    """When preferred is set but not loaded, raise NoEmbeddingModelLoadedError."""
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                preferred_embedding_model_id="text-embedding-bge-m3",
            )
        )

    # Only nomic is loaded; bge-m3 is NOT.
    models_service = _make_models_service(["text-embedding-nomic-embed-text-v1.5"])

    with pytest.raises(NoEmbeddingModelLoadedError, match="text-embedding-bge-m3"):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
        )


async def test_resolver_error_message_mentions_settings(engine: AsyncEngine) -> None:
    """The fail-loud error message tells the admin to check Settings."""
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                preferred_embedding_model_id="missing-model",
            )
        )

    models_service = _make_models_service(["text-embedding-nomic-embed-text-v1.5"])

    with pytest.raises(NoEmbeddingModelLoadedError, match="Settings"):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
        )


# ---------------------------------------------------------------------------
# Branch 3: preferred UNSET → deterministic pick + persist
# ---------------------------------------------------------------------------


async def test_resolver_picks_lexicographically_first_when_two_loaded(
    engine: AsyncEngine,
) -> None:
    """With no preference and two embedders loaded, pick the canonical default.

    The no-preference auto-pick is the canonical default
    (nomic) when loaded — NOT lexicographic-first. bge-m3 sorts first but is a
    different dimension (1024 vs nomic 768); silently picking it corrupts recall.
    """
    models_service = _make_models_service([
        "text-embedding-bge-m3",
        "text-embedding-nomic-embed-text-v1.5",
    ])

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service,
        persist_default=False,  # don't write for this test
    )
    # Nomic (the canonical default), NOT lexicographic-first bge-m3.
    assert result == "text-embedding-nomic-embed-text-v1.5"


async def test_resolver_deterministic_across_different_list_orders(
    engine: AsyncEngine,
) -> None:
    """Result is the same regardless of the order models_service returns them."""
    # Reverse order: nomic first, bge second.
    models_service_reversed = _make_models_service([
        "text-embedding-nomic-embed-text-v1.5",
        "text-embedding-bge-m3",
    ])

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service_reversed,
        persist_default=False,
    )
    # Must be nomic (the canonical default), regardless of list order.
    assert result == "text-embedding-nomic-embed-text-v1.5"


async def test_resolver_no_preference_prefers_nomic_over_lexicographic_first(
    engine: AsyncEngine,
) -> None:
    """No preference + bge-m3 listed FIRST + nomic loaded →
    the resolver picks nomic (the canonical default), NOT the lexicographically-
    first bge-m3. This is the core dimension-safety guard: bge-m3 is 1024-dim and
    nomic is 768-dim; silently picking bge would corrupt recall.
    """
    # bge-m3 deliberately listed first; both are genuinely loaded.
    models_service = _make_models_service([
        "text-embedding-bge-m3",
        "text-embedding-nomic-embed-text-v1.5",
    ])

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service,
        persist_default=True,
    )
    assert result == "text-embedding-nomic-embed-text-v1.5"
    # And the persisted preference is nomic, never bge-m3.
    assert await _read_preferred(engine) == "text-embedding-nomic-embed-text-v1.5"


async def test_resolver_no_preference_fails_loud_when_nomic_not_loaded(
    engine: AsyncEngine,
) -> None:
    """No preference + nomic NOT loaded (only bge-m3 loaded) →
    the resolver FAILS LOUD with a clear "load the default" message. It must NOT
    silently fall back to bge-m3 (different dimension). The admin loads nomic
    (LM Studio's first-launch default) rather than indexing under the wrong model.
    """
    # Only bge-m3 loaded; the canonical default (nomic) is absent.
    models_service = _make_models_service(["text-embedding-bge-m3"])

    with pytest.raises(
        NoEmbeddingModelLoadedError,
        match="text-embedding-nomic-embed-text-v1.5",
    ):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
            persist_default=True,
        )

    # Nothing was persisted — bge-m3 must NEVER become the auto-pick.
    assert await _read_preferred(engine) is None


async def test_resolver_persists_default_on_first_call(engine: AsyncEngine) -> None:
    """With persist_default=True, first call writes the chosen key to DB."""
    models_service = _make_models_service([
        "text-embedding-bge-m3",
        "text-embedding-nomic-embed-text-v1.5",
    ])

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service,
        persist_default=True,
    )
    # The canonical default (nomic) is chosen and persisted, not bge-m3.
    assert result == "text-embedding-nomic-embed-text-v1.5"

    # Verify the preference was written.
    stored = await _read_preferred(engine)
    assert stored == "text-embedding-nomic-embed-text-v1.5"


async def test_resolver_stable_after_persist_even_if_only_nomic_loaded(
    engine: AsyncEngine,
) -> None:
    """A pinned model that gets unloaded triggers fail-loud (no silent swap).

    With both loaded and no preference, the resolver
    auto-picks nomic (the canonical default), so bge-m3 is never auto-persisted.
    To exercise the persist-then-unload invariant we pin bge-m3 explicitly,
    then unload it: the resolver must FAIL LOUD rather than silently falling
    back to the still-loaded nomic (a different, dimensionally-incompatible
    model would corrupt recall).
    """
    from lmchat.services.memory_service import _persist_embedding_preference

    # Pin bge-m3 explicitly (admin preference).
    await _persist_embedding_preference(engine, "text-embedding-bge-m3")
    stored = await _read_preferred(engine)
    assert stored == "text-embedding-bge-m3"

    # bge-m3 idle-unloaded, only nomic available → fail loud (NOT auto-swap).
    ms_nomic_only = _make_models_service(["text-embedding-nomic-embed-text-v1.5"])
    with pytest.raises(NoEmbeddingModelLoadedError, match="text-embedding-bge-m3"):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=ms_nomic_only,
            persist_default=True,
        )


async def test_resolver_same_key_returned_on_repeated_calls(
    engine: AsyncEngine,
) -> None:
    """With two embedders loaded, repeated calls return the SAME key (determinism)."""
    models_service = _make_models_service([
        "text-embedding-bge-m3",
        "text-embedding-nomic-embed-text-v1.5",
    ])

    results = []
    for _ in range(3):
        r = await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
            persist_default=True,
        )
        results.append(r)

    # All calls must agree.
    assert len(set(results)) == 1
    # The canonical default (nomic) is chosen, not lexicographic-first bge-m3.
    assert results[0] == "text-embedding-nomic-embed-text-v1.5"


# ---------------------------------------------------------------------------
# Branch 4: no embedder loaded → raises
# ---------------------------------------------------------------------------


async def test_resolver_raises_when_no_embedding_model_loaded(
    engine: AsyncEngine,
) -> None:
    """When no embedding model is loaded and no preference, raise."""
    models_service = AsyncMock(spec=ModelsService)
    models_service.list_loaded.return_value = []  # no models

    with pytest.raises(NoEmbeddingModelLoadedError):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
        )


async def test_resolver_raises_when_only_llm_models_loaded(
    engine: AsyncEngine,
) -> None:
    """When only chat LLM models are loaded (not embedding), raise."""
    models_service = AsyncMock(spec=ModelsService)
    models_service.list_loaded.return_value = [
        ModelInfo(
            key="qwen3-8b",
            type="llm",
            capabilities=Capabilities(vision=False, trained_for_tool_use=True),
        )
    ]

    with pytest.raises(NoEmbeddingModelLoadedError):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
        )


# ---------------------------------------------------------------------------
# MemoryService.resolve_active_embedding_model_key (delegation wrapper)
# ---------------------------------------------------------------------------


async def test_memory_service_resolver_delegates_to_module_function(
    engine: AsyncEngine,
) -> None:
    """MemoryService.resolve_active_embedding_model_key delegates correctly."""
    models_service = _make_models_service([
        "text-embedding-bge-m3",
        "text-embedding-nomic-embed-text-v1.5",
    ])
    svc = MemoryService(
        engine=engine,
        embedding_client=MagicMock(spec=EmbeddingClient),
        models_service=models_service,
    )

    result = await svc.resolve_active_embedding_model_key(persist_default=True)
    assert result == "text-embedding-nomic-embed-text-v1.5"

    # Also verify persist happened.
    stored = await _read_preferred(engine)
    assert stored == "text-embedding-nomic-embed-text-v1.5"


async def test_memory_service_default_embedding_model_uses_resolver(
    engine: AsyncEngine,
) -> None:
    """MemoryService._default_embedding_model() routes through the resolver."""
    models_service = _make_models_service([
        "text-embedding-bge-m3",
        "text-embedding-nomic-embed-text-v1.5",
    ])
    svc = MemoryService(
        engine=engine,
        embedding_client=MagicMock(spec=EmbeddingClient),
        models_service=models_service,
    )

    # _default_embedding_model is the shim called by index_message/embed_texts.
    result = await svc._default_embedding_model()
    assert result == "text-embedding-nomic-embed-text-v1.5"


# ---------------------------------------------------------------------------
# Prefix/instance-id match branches (preferred stored as instance-id form)
# ---------------------------------------------------------------------------


async def test_resolver_preferred_matches_via_instance_id(engine: AsyncEngine) -> None:
    """Preferred stored as instance-id (e.g. 'nomic@q8_0') still resolves."""
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                preferred_embedding_model_id="text-embedding-nomic-embed-text-v1.5@q8_0",
            )
        )

    models_service = _make_models_service(["text-embedding-nomic-embed-text-v1.5"])

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service,
    )
    assert result == "text-embedding-nomic-embed-text-v1.5@q8_0"


async def test_resolver_preferred_matches_via_prefix(engine: AsyncEngine) -> None:
    """Preferred stored as catalog key resolves when only an @-suffixed instance is loaded."""
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                preferred_embedding_model_id="text-embedding-nomic-embed-text-v1.5",
            )
        )

    # ModelInfo has no loaded_instance_ids but we simulate a loaded instance.
    mock = AsyncMock(spec=ModelsService)
    model = ModelInfo(
        key="text-embedding-nomic-embed-text-v1.5",
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        loaded_instance_ids=["text-embedding-nomic-embed-text-v1.5@q8_0"],
    )
    mock.list_loaded.return_value = [model]

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=mock,
    )
    assert result == "text-embedding-nomic-embed-text-v1.5"


# ---------------------------------------------------------------------------
# Reverse bare-@-strip fallback: preferred pins a stale @quant that is no
# longer loaded, but the SAME family IS loaded under a different quant. The
# write/index path must degrade exactly like recall (resolve_embedding_wire_id
# has this fallback) — a stale pinned quant must NOT make memory silently stop
# SAVING while recall keeps READING. Regression for the write-vs-recall
# resolver divergence.
# ---------------------------------------------------------------------------


async def test_resolver_preferred_stale_quant_falls_back_to_same_family(
    engine: AsyncEngine,
) -> None:
    """Preferred pins nomic@q4_0 but nomic is loaded under @q8_0 → resolve, not raise.

    Before the fix the write path's matcher lacked the reverse bare-@-strip
    fallback recall has, so a stale pinned quant raised
    NoEmbeddingModelLoadedError → memory silently stopped SAVING while recall
    kept READING. The write path must now degrade the same way recall does.
    """
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                # Pinned quant that is NO LONGER loaded.
                preferred_embedding_model_id=(
                    "text-embedding-nomic-embed-text-v1.5@q4_0"
                ),
            )
        )

    # nomic is loaded, but under a DIFFERENT quant (@q8_0, per the helper).
    models_service = _make_models_service(["text-embedding-nomic-embed-text-v1.5"])

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service,
        persist_default=False,
    )
    # Same-family fallback keeps the pinned key (downstream wire resolution
    # maps it to the loaded @q8_0 instance) rather than failing loud.
    assert result == "text-embedding-nomic-embed-text-v1.5@q4_0"


async def test_resolver_preferred_stale_quant_still_fails_loud_when_family_absent(
    engine: AsyncEngine,
) -> None:
    """Stale @quant whose ENTIRE family is unloaded → still fail loud.

    The reverse fallback is same-family ONLY. If nomic is not loaded at all
    (only bge-m3 is), a pinned nomic@q4_0 must NOT resolve to the different-
    dimension bge-m3 — it must raise, preserving dimension safety.
    """
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                preferred_embedding_model_id=(
                    "text-embedding-nomic-embed-text-v1.5@q4_0"
                ),
            )
        )

    # Only bge-m3 loaded; the nomic family is entirely absent.
    models_service = _make_models_service(["text-embedding-bge-m3"])

    with pytest.raises(
        NoEmbeddingModelLoadedError,
        match="text-embedding-nomic-embed-text-v1.5@q4_0",
    ):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
            persist_default=False,
        )


# ---------------------------------------------------------------------------
# Regression: list_loaded() returns the FULL catalog — type filter is NOT a
# loaded filter. A catalog embedder with loaded_instance_ids=[] (present in
# the catalog but NOT loaded) must be excluded, even when it sorts FIRST.
# The bug: bge-m3 (in catalog, not loaded) won the lex sort, passed
# _is_loaded (because loaded_keys was built from the same unfiltered set),
# got persisted as preferred, and pinned projects to an unloaded,
# dimensionally-incompatible model.
# ---------------------------------------------------------------------------


def _make_mixed_catalog_models_service() -> ModelsService:
    """ModelsService whose list_loaded returns TWO embedding-type models:

    * ``text-embedding-bge-m3`` — IN catalog, NOT loaded
      (``loaded_instance_ids=[]``). Sorts FIRST alphabetically.
    * ``text-embedding-nomic-embed-text-v1.5`` — genuinely loaded
      (``loaded_instance_ids=["...@q8_0"]``). Sorts SECOND.

    Mirrors LM Studio's ``GET /api/v1/models`` returning the whole catalog,
    not just loaded instances.
    """
    mock = AsyncMock(spec=ModelsService)
    mock.list_loaded.return_value = [
        # In catalog but NOT loaded — empty loaded_instance_ids.
        ModelInfo(
            key="text-embedding-bge-m3",
            type="embedding",
            capabilities=Capabilities(vision=False, trained_for_tool_use=False),
            loaded_instance_ids=[],
        ),
        # Genuinely loaded.
        ModelInfo(
            key="text-embedding-nomic-embed-text-v1.5",
            type="embedding",
            capabilities=Capabilities(vision=False, trained_for_tool_use=False),
            loaded_instance_ids=["text-embedding-nomic-embed-text-v1.5@q8_0"],
        ),
    ]
    return mock


async def test_resolver_skips_catalog_unloaded_embedder_picks_loaded_one(
    engine: AsyncEngine,
) -> None:
    """(a) No preference: resolver must pick the genuinely-LOADED embedder,
    NOT the alphabetically-first one that is merely in the catalog.

    bge-m3 sorts first but has loaded_instance_ids=[] (not loaded). nomic is
    loaded. The resolver must return nomic and persist nomic — never bge-m3.
    """
    models_service = _make_mixed_catalog_models_service()

    result = await resolve_active_embedding_model_key(
        engine=engine,
        models_service=models_service,
        persist_default=True,
    )
    # MUST be the loaded model, not the lexicographically-first unloaded one.
    assert result == "text-embedding-nomic-embed-text-v1.5"

    # And the persisted preference must be the loaded model, not bge-m3.
    stored = await _read_preferred(engine)
    assert stored == "text-embedding-nomic-embed-text-v1.5"


async def test_resolver_raises_when_preferred_is_catalog_only_not_loaded(
    engine: AsyncEngine,
) -> None:
    """(b) Preference set to the catalog-only (unloaded) embedder → fail loud.

    bge-m3 is in the catalog but NOT loaded (loaded_instance_ids=[]). If the
    admin pinned it as preferred, the resolver must NOT silently accept it
    (it would route embeddings to an unloaded, dimensionally-incompatible
    model). It must raise NoEmbeddingModelLoadedError.
    """
    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1,
                preferred_embedding_model_id="text-embedding-bge-m3",
            )
        )

    models_service = _make_mixed_catalog_models_service()

    with pytest.raises(NoEmbeddingModelLoadedError, match="text-embedding-bge-m3"):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
        )


async def test_resolver_raises_when_all_embedders_are_catalog_only(
    engine: AsyncEngine,
) -> None:
    """No preference + every embedding-type model has loaded_instance_ids=[]
    (whole catalog present but nothing actually loaded) → raise.

    Guards the case where the type filter would have found models but the
    loaded filter correctly finds none.
    """
    mock = AsyncMock(spec=ModelsService)
    mock.list_loaded.return_value = [
        ModelInfo(
            key="text-embedding-bge-m3",
            type="embedding",
            capabilities=Capabilities(vision=False, trained_for_tool_use=False),
            loaded_instance_ids=[],
        ),
        ModelInfo(
            key="text-embedding-nomic-embed-text-v1.5",
            type="embedding",
            capabilities=Capabilities(vision=False, trained_for_tool_use=False),
            loaded_instance_ids=[],
        ),
    ]

    with pytest.raises(NoEmbeddingModelLoadedError):
        await resolve_active_embedding_model_key(
            engine=engine,
            models_service=mock,
        )


# ---------------------------------------------------------------------------
# embedding_status() — active_model_id alignment + loaded-only listing.
#
# The Settings/Memory visibility snapshot previously took active = loaded[0]
# over the UNFILTERED catalog (so it could report e.g. ``bge-m3`` while the
# index/recall path actually embedded under ``nomic``), and listed every
# downloaded quant variant (letting the admin pin a not-loaded one,
# breaking memory indexing). These pin the corrected behaviour.
# ---------------------------------------------------------------------------


async def test_embedding_status_active_id_matches_resolver_not_list_order(
    engine: AsyncEngine,
) -> None:
    """active_model_id must be the model the resolver picks, NOT loaded[0].

    The catalog lists an unloaded ``bge-m3`` FIRST and a loaded ``nomic``
    SECOND. The old snapshot reported ``bge-m3`` (list order); the aligned
    snapshot must report ``nomic`` — the model the index/recall path uses.
    """
    models_service = _make_mixed_catalog_models_service()
    svc = MemoryService(
        engine=engine,
        embedding_client=MagicMock(spec=EmbeddingClient),
        models_service=models_service,
    )

    snap = await svc.embedding_status()

    assert snap["active_model_id"] == "text-embedding-nomic-embed-text-v1.5"
    # And the loaded list excludes the catalog-only (unloaded) bge-m3.
    assert snap["loaded_embedding_models"] == [
        "text-embedding-nomic-embed-text-v1.5"
    ]
    assert "text-embedding-bge-m3" not in snap["loaded_embedding_models"]


async def test_embedding_status_active_id_none_when_nothing_loaded(
    engine: AsyncEngine,
) -> None:
    """When every embedding-type model is catalog-only (unloaded),
    active_model_id is None and the loaded list is empty."""
    mock = AsyncMock(spec=ModelsService)
    mock.list_loaded.return_value = [
        ModelInfo(
            key="text-embedding-bge-m3",
            type="embedding",
            capabilities=Capabilities(vision=False, trained_for_tool_use=False),
            loaded_instance_ids=[],
        ),
    ]
    svc = MemoryService(
        engine=engine,
        embedding_client=MagicMock(spec=EmbeddingClient),
        models_service=mock,
    )

    snap = await svc.embedding_status()

    assert snap["active_model_id"] is None
    assert snap["loaded_embedding_models"] == []


async def test_embedding_status_is_read_only_does_not_persist_preference(
    engine: AsyncEngine,
) -> None:
    """embedding_status() is a pure snapshot — it must NOT write a preference.

    It resolves the active model with persist_default=False so merely opening
    Settings never silently pins an auto-picked embedder.
    """
    models_service = _make_models_service([
        "text-embedding-bge-m3",
        "text-embedding-nomic-embed-text-v1.5",
    ])
    svc = MemoryService(
        engine=engine,
        embedding_client=MagicMock(spec=EmbeddingClient),
        models_service=models_service,
    )

    snap = await svc.embedding_status()
    # Auto-pick is the canonical default (nomic) among LOADED models.
    assert snap["active_model_id"] == "text-embedding-nomic-embed-text-v1.5"
    # But nothing was persisted — the snapshot is read-only.
    assert await _read_preferred(engine) is None


# ---------------------------------------------------------------------------
# embedding_status() — active_model_id is None on generic resolver error
#
# Previously the generic except branch fell back to loaded_embedding_keys[0],
# which could report a green "active" card while indexing was dead (dimension
# mismatch). It now returns None so the admin card reflects the failure.
# ---------------------------------------------------------------------------


async def test_embedding_status_active_id_none_on_generic_resolver_error(
    engine: AsyncEngine,
) -> None:
    """A generic resolver exception must return active_model_id=None, NOT the
    first loaded embedder. Returning the first embedder could show a green
    "active" status while the real resolver (and indexing) is broken."""
    from unittest.mock import patch

    models_service = _make_models_service(["text-embedding-nomic-embed-text-v1.5"])
    svc = MemoryService(
        engine=engine,
        embedding_client=MagicMock(spec=EmbeddingClient),
        models_service=models_service,
    )

    # Simulate a generic (non-NoEmbeddingModelLoadedError) resolver failure.
    with patch(
        "lmchat.services.memory_service.resolve_active_embedding_model_key",
        side_effect=RuntimeError("resolver blew up"),
    ):
        snap = await svc.embedding_status()

    # Must be None — NOT "text-embedding-nomic-embed-text-v1.5".
    assert snap["active_model_id"] is None
    # The loaded list still reflects what's genuinely loaded.
    assert "text-embedding-nomic-embed-text-v1.5" in snap["loaded_embedding_models"]


async def test_embedding_status_active_id_none_on_no_embedding_model_error(
    engine: AsyncEngine,
) -> None:
    """NoEmbeddingModelLoadedError → active_model_id=None (existing behaviour,
    preserved through the refactor)."""
    models_service = AsyncMock(spec=ModelsService)
    models_service.list_loaded.return_value = []
    svc = MemoryService(
        engine=engine,
        embedding_client=MagicMock(spec=EmbeddingClient),
        models_service=models_service,
    )

    snap = await svc.embedding_status()
    assert snap["active_model_id"] is None


# ---------------------------------------------------------------------------
# embedding_status() — last_indexed_at must interpret created_at as UTC.
#
# messages.created_at is a DateTime column; SQLAlchemy always returns it as
# a Python datetime (never a raw float or string), so the `float(last_ts)`
# fast path in embedding_status() always fails and the fromisoformat()
# fallback is the NORMAL path for real data, not a rare legacy-writer edge
# case. .timestamp() on the resulting naive datetime would misinterpret it
# as host-local time instead of UTC.
# ---------------------------------------------------------------------------


async def test_embedding_status_last_indexed_at_interprets_created_at_as_utc(
    engine: AsyncEngine,
) -> None:
    """`last_indexed_at` must be the message's `created_at` interpreted as
    UTC, not as the process's local timezone.

    Forces the process TZ to a fixed non-UTC offset (UTC+8, no DST) so the
    assertion is deterministic regardless of the CI host's real timezone —
    a UTC-hosted CI box would not otherwise exercise the skew at all.
    """
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        created_naive = datetime(2026, 1, 1, 12, 0, 0)  # naive UTC wall-clock,
        # matching what SQLite hands back for a func.now() write.
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO users (id, username, password_hash)"
                     " VALUES (1, 'user1', 'scrypt$dummy')")
            )
            await conn.execute(
                text("INSERT INTO chats (id, user_id, title)"
                     " VALUES (1, 1, 'Test chat')")
            )
            await conn.execute(
                text(
                    "INSERT INTO messages (id, chat_id, role, content, created_at)"
                    " VALUES (1, 1, 'user', 'hi', :ca)"
                ),
                {"ca": created_naive.strftime("%Y-%m-%d %H:%M:%S")},
            )
            await conn.execute(
                text(
                    "INSERT INTO message_embeddings"
                    " (message_id, embedding_model_id, embedding, text_hash)"
                    " VALUES (1, 'text-embedding-nomic-embed-text-v1.5', X'00', 'h1')"
                )
            )

        models_service = _make_models_service(["text-embedding-nomic-embed-text-v1.5"])
        svc = MemoryService(
            engine=engine,
            embedding_client=MagicMock(spec=EmbeddingClient),
            models_service=models_service,
        )

        snap = await svc.embedding_status()

        expected_epoch = created_naive.replace(tzinfo=UTC).timestamp()
        assert snap["last_indexed_at"] == pytest.approx(expected_epoch)
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()
