# SPDX-License-Identifier: Apache-2.0
"""Tests for LmStudioOverridesService.rewire_singletons.

Covers:
- Basic swap: all five singleton references are mutated to the new client.
- Models cache cleared after swap.
- Trailing-slash normalization applied to new_base_url before assignment.
- resolve_admin_tier_only returns admin-tier values, not user-tier.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import Final
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.config import Settings
from lmchat.db.schema import metadata
from lmchat.services.lm_studio_overrides_service import LmStudioOverridesService

_SECRET: Final[str] = "test-secret-32-bytes-of-entropy!!"


@pytest.fixture(autouse=True)
def _seed_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SECRET", _SECRET)
    get_settings.cache_clear()


def _settings(
    *,
    base_url: str = "http://env.example:1234",
    api_key: str = "env-api-key",
    default_model: str = "env-model",
) -> Settings:
    return Settings(  # type: ignore[call-arg]
        lm_studio_base_url=base_url,
        lm_studio_api_key=api_key,
        lm_studio_default_model=default_model,
        lm_chat_secret=_SECRET,
    )


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/rewire_test.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


def _make_app_state(
    base_url: str, api_key: str
) -> tuple[types.SimpleNamespace, httpx.AsyncClient]:
    """Build a minimal fake app.state with all required singleton attributes.

    Returns ``(state, old_client)`` — old_client is returned so tests can
    aclose() it in their cleanup.
    """
    old_client = httpx.AsyncClient(base_url=base_url)

    lmstudio_adapter = MagicMock()
    lmstudio_adapter._http_client = old_client
    lmstudio_adapter._base_url = base_url

    models_service = MagicMock()
    models_service._http_client = old_client
    models_service._base_url = base_url
    models_service._cache = ["stale-model"]
    models_service._cache_lock = asyncio.Lock()

    embedding_client = MagicMock()
    embedding_client._http = old_client
    embedding_client._base_url = base_url
    embedding_client._endpoint = f"{base_url}/api/v1/embeddings"

    web_search_service = MagicMock()
    web_search_service.reconfigure = MagicMock()

    state = types.SimpleNamespace(
        http_client=old_client,
        http=old_client,
        lmstudio_adapter=lmstudio_adapter,
        models_service=models_service,
        embedding_client=embedding_client,
        web_search_service=web_search_service,
        rewire_lock=asyncio.Lock(),
    )
    return state, old_client


@pytest.mark.asyncio
async def test_rewire_swaps_singletons(tmp_path: Path) -> None:
    """rewire_singletons mutates all five singleton references to the new client."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        state, old_client = _make_app_state("http://old.host:1234", "old-key")

        # Patch _delayed_close to avoid real asyncio.sleep in tests.
        with patch(
            "lmchat.services.lm_studio_overrides_service._delayed_close",
            new=AsyncMock(),
        ):
            await svc.rewire_singletons(
                state,
                new_base_url="http://new.host:5678",
                new_api_key="new-key",
            )

        # app.state.http_client replaced.
        assert state.http_client is not old_client
        new_client = state.http_client

        # Backward-compat alias also replaced.
        assert state.http is new_client

        # Adapter references updated.
        assert state.lmstudio_adapter._http_client is new_client
        assert state.lmstudio_adapter._base_url == "http://new.host:5678"

        # ModelsService references updated.
        assert state.models_service._http_client is new_client
        assert state.models_service._base_url == "http://new.host:5678"

        # EmbeddingClient references updated.
        assert state.embedding_client._http is new_client
        assert state.embedding_client._base_url == "http://new.host:5678"
        assert state.embedding_client._endpoint == (
            "http://new.host:5678/v1/embeddings"
        )

        # WebSearchService.reconfigure called with the new client.
        state.web_search_service.reconfigure.assert_called_once_with(
            http_client=new_client
        )

        await old_client.aclose()
        await new_client.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rewire_invalidates_models_cache(tmp_path: Path) -> None:
    """rewire_singletons sets ModelsService._cache to None."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        state, old_client = _make_app_state("http://old.host:1234", "old-key")

        # Pre-condition: cache is populated.
        assert state.models_service._cache is not None

        with patch(
            "lmchat.services.lm_studio_overrides_service._delayed_close",
            new=AsyncMock(),
        ):
            await svc.rewire_singletons(
                state,
                new_base_url="http://new.host:5678",
                new_api_key="new-key",
            )

        assert state.models_service._cache is None

        await old_client.aclose()
        await state.http_client.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rewire_strips_trailing_slash(tmp_path: Path) -> None:
    """Trailing slash on new_base_url is stripped before assignment."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        state, old_client = _make_app_state("http://old.host:1234", "old-key")

        with patch(
            "lmchat.services.lm_studio_overrides_service._delayed_close",
            new=AsyncMock(),
        ):
            await svc.rewire_singletons(
                state,
                new_base_url="http://new.host:5678/",  # trailing slash
                new_api_key="",
            )

        # No trailing slash on any stored attribute.
        assert state.lmstudio_adapter._base_url == "http://new.host:5678"
        assert state.models_service._base_url == "http://new.host:5678"
        assert state.embedding_client._base_url == "http://new.host:5678"
        # Endpoint uses clean base.
        assert state.embedding_client._endpoint == (
            "http://new.host:5678/v1/embeddings"
        )

        await old_client.aclose()
        await state.http_client.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rewire_resets_auth_failed_flag_and_mirror(tmp_path: Path) -> None:
    """LM Studio key-save chain regression.

    Regression test for the "added the key, says
    connected, no models populate" symptom. The chain was:

    1. LM Studio rotates the API key → BG refresh receives 401 →
       ``ModelsService._auth_failed = True``, ``_auth_failed_at = now``.
    2. Admin saves new key via PATCH /api/admin/lmstudio/default →
       rewire_singletons swaps a fresh httpx.AsyncClient (correct) but
       leaves ``_auth_failed = True`` untouched (BUG).
    3. FE auto-polls /api/models → cache-miss → refresh() returns
       early due to the auth_failed backoff gate → cache stays empty.
    4. Up to 60s of "no models populate" after a correct save.

    Plus the FE banner mirror at ``app.state.lm_studio_auth_failed``
    is only written by the 30-min periodic refresh loop — without
    this reset the banner says "auth_failed" for up to 30 minutes
    after a correct save.

    Fix locks down: BOTH flags must be False AFTER rewire completes,
    AND the reset MUST happen after the inner ``_cache_lock`` await
    point so an interleaving in-flight refresh on the old client
    cannot re-set the flag mid-rewire.
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        state, old_client = _make_app_state("http://old.host:1234", "old-key")

        # Exact pre-condition: prior 401 set the auth_failed
        # flag, FE banner mirror echoes it. The rewire must clear BOTH.
        state.models_service._auth_failed = True
        state.models_service._auth_failed_at = 1234.5
        state.lm_studio_auth_failed = True

        # Warm-up needs an awaitable refresh and a readable auth_failed
        # property on the models_service mock. Configure_mock works
        # around MagicMock's default property handling.
        state.models_service.refresh = AsyncMock(return_value=None)
        state.models_service.auth_failed = False

        with patch(
            "lmchat.services.lm_studio_overrides_service._delayed_close",
            new=AsyncMock(),
        ):
            await svc.rewire_singletons(
                state,
                new_base_url="http://new.host:5678",
                new_api_key="new-working-key",
            )

        # Both flags must be False post-rewire (placement requirement:
        # AFTER the _cache_lock await point).
        assert state.models_service._auth_failed is False
        assert state.models_service._auth_failed_at == 0.0
        assert state.lm_studio_auth_failed is False

        await old_client.aclose()
        await state.http_client.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rewire_schedules_warmup_refresh_and_syncs_mirror(
    tmp_path: Path,
) -> None:
    """Warm-up refresh scheduling + banner-mirror sync after rewire.

    The warm-up refresh after rewire is the difference between "model
    dropdown populates in <1s after Save" and "admin waits up to
    25s for the next FE poll cycle to refill the cache". It must:

    1. Fire after rewire completes (so the next /api/models call
       returns real data, not an empty list from a cold cache-miss).
    2. Sync ``app.state.lm_studio_auth_failed`` from
       ``models_service.auth_failed`` AFTER the refresh runs — without
       this, a failing warm-up leaves the FE banner saying
       "connected" while the model dropdown stays empty for up to
       ``models_cache_refresh_interval_seconds`` (default 30 min).
    3. Be a tracked named task (held on app.state) so the event loop
       cannot GC it mid-flight under memory pressure and so its
       exception (if any) is debuggable.
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        state, old_client = _make_app_state("http://old.host:1234", "old-key")

        # Mock refresh as an AsyncMock so we can verify it was awaited.
        state.models_service.refresh = AsyncMock(return_value=None)
        # auth_failed property returns False AFTER a successful refresh.
        state.models_service.auth_failed = False
        state.lm_studio_auth_failed = True  # pre-existing stale state

        with patch(
            "lmchat.services.lm_studio_overrides_service._delayed_close",
            new=AsyncMock(),
        ):
            await svc.rewire_singletons(
                state,
                new_base_url="http://new.host:5678",
                new_api_key="new-working-key",
            )

        # Warm-up task was scheduled and is reachable from app.state
        # (held reference prevents GC mid-flight).
        assert hasattr(state, "lmstudio_warmup_refresh_task")
        warmup = state.lmstudio_warmup_refresh_task
        assert isinstance(warmup, asyncio.Task)
        assert warmup.get_name() == "lmstudio_rewire_warmup_refresh"

        # Drain the task — it should complete cleanly because the
        # mocked refresh returns None.
        await warmup

        # The wrapper must have awaited refresh() AND synced the
        # mirror flag from models_service.auth_failed.
        state.models_service.refresh.assert_awaited_once()
        assert state.lm_studio_auth_failed is False

        await old_client.aclose()
        await state.http_client.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rewire_warmup_syncs_mirror_to_true_when_refresh_fails(
    tmp_path: Path,
) -> None:
    """If the warm-up refresh hits a 401
    (e.g. key rotated again between admin probe and rewire), the
    refresh sets ``_auth_failed=True`` internally; the warm-up wrapper
    must then sync the mirror to True so the FE banner reflects truth.

    Without this sync, the FE shows "Connected" while the dropdown
    stays empty until the next 30-min periodic loop reads the
    auth_failed property — exactly the reported symptom in
    its worst form.
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        state, old_client = _make_app_state("http://old.host:1234", "old-key")

        # The wrapper's `finally` block syncs the mirror from the
        # service's auth_failed property. Simulate a refresh that
        # sets the service flag back to True (i.e. probe got 401).
        async def _refresh_that_re_sets_auth_failed() -> None:
            state.models_service.auth_failed = True

        state.models_service.refresh = AsyncMock(
            side_effect=_refresh_that_re_sets_auth_failed
        )
        state.models_service.auth_failed = False  # initially clear
        state.lm_studio_auth_failed = False  # mirror initially clear

        with patch(
            "lmchat.services.lm_studio_overrides_service._delayed_close",
            new=AsyncMock(),
        ):
            await svc.rewire_singletons(
                state,
                new_base_url="http://new.host:5678",
                new_api_key="key-rotated-again",
            )

        # Drain the warm-up.
        await state.lmstudio_warmup_refresh_task

        # Mirror MUST reflect truth — refresh re-set the service flag
        # to True so the FE banner must say "auth failed" now, not
        # "connected".
        assert state.lm_studio_auth_failed is True

        await old_client.aclose()
        await state.http_client.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_admin_tier_only_returns_unset_when_no_admin_row(
    tmp_path: Path,
) -> None:
    """resolve_admin_tier_only returns 'unset' when no admin row exists.

    The env fallback was removed from resolve_admin_tier_only(): when no
    server_lm_studio_default row exists, every field returns '' with
    source 'unset'.
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(
            engine=engine,
            settings=_settings(
                base_url="http://env.host:1234",
                api_key="env-key",
                default_model="env-model",
            ),
        )
        cfg = await svc.resolve_admin_tier_only()
        assert cfg.base_url == ""
        assert cfg.api_key == ""
        assert cfg.default_model == ""
        assert cfg.source_base_url == "unset"
        assert cfg.source_api_key == "unset"
        assert cfg.source_default_model == "unset"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_admin_tier_only_returns_admin_row(tmp_path: Path) -> None:
    """resolve_admin_tier_only returns saved admin values, not user-tier."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(
            engine=engine,
            settings=_settings(
                base_url="http://env.host:1234",
                api_key="env-key",
                default_model="env-model",
            ),
        )
        # Write admin default.
        await svc.set_admin_default(
            base_url="http://admin.host:9999",
            api_key="admin-key",
            default_model="admin-model",
            clear=None,
        )
        cfg = await svc.resolve_admin_tier_only()
        assert cfg.base_url == "http://admin.host:9999"
        assert cfg.api_key == "admin-key"
        assert cfg.default_model == "admin-model"
        assert cfg.source_base_url == "server_admin"
        assert cfg.source_api_key == "server_admin"
        assert cfg.source_default_model == "server_admin"
    finally:
        await engine.dispose()
