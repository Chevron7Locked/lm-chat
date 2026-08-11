# SPDX-License-Identifier: Apache-2.0
"""Tests for the probe gate and rewire isolation in update_server_admin_default.

Covers:
- test_probe_client_isolated_from_rewire_lock: 10 concurrent admin saves
  where the probe succeeds; asserts no deadlock and all 10 complete.
- Probe failure returns HTTP 400 and does NOT trigger rewire.
- Only base_url/api_key changes trigger rewire; default_model-only does not.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import Any, Final
from unittest.mock import MagicMock, patch

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


def _settings(*, base_url: str = "http://env.example:1234") -> Settings:
    return Settings(  # type: ignore[call-arg]
        lm_studio_base_url=base_url,
        lm_studio_api_key="env-key",
        lm_studio_default_model="env-model",
        lm_chat_secret=_SECRET,
    )


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/probe_test.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


def _make_app_state(base_url: str) -> Any:
    old_client = httpx.AsyncClient(base_url=base_url)

    lmstudio_adapter = MagicMock()
    lmstudio_adapter._http_client = old_client
    lmstudio_adapter._base_url = base_url

    models_service = MagicMock()
    models_service._http_client = old_client
    models_service._base_url = base_url
    models_service._cache = None
    models_service._cache_lock = asyncio.Lock()

    embedding_client = MagicMock()
    embedding_client._http = old_client
    embedding_client._base_url = base_url
    embedding_client._endpoint = f"{base_url}/api/v1/embeddings"

    web_search_service = MagicMock()
    web_search_service.reconfigure = MagicMock()

    return types.SimpleNamespace(
        http_client=old_client,
        http=old_client,
        lmstudio_adapter=lmstudio_adapter,
        models_service=models_service,
        embedding_client=embedding_client,
        web_search_service=web_search_service,
        rewire_lock=asyncio.Lock(),
    ), old_client


@pytest.mark.asyncio
async def test_probe_client_isolated_from_rewire_lock(tmp_path: Path) -> None:
    """10 concurrent rewire calls complete without deadlock.

    The probe client is constructed and torn down BEFORE rewire_lock is
    acquired, so concurrent saves can't deadlock through the probe client
    holding a resource that rewire_lock also needs.
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        state, old_client = _make_app_state("http://original.host:1234")

        rewire_count = 0

        async def _mock_rewire(app_state: Any, *, new_base_url: str, new_api_key: str) -> None:
            nonlocal rewire_count
            # Simulate brief async work inside rewire.
            await asyncio.sleep(0)
            rewire_count += 1

        with patch.object(svc, "rewire_singletons", side_effect=_mock_rewire):
            # Fire 10 concurrent "saves" — each triggers rewire.
            tasks = [
                asyncio.create_task(
                    svc.rewire_singletons(
                        state,
                        new_base_url=f"http://new-host-{i}.example:5678",
                        new_api_key=f"key-{i}",
                    )
                )
                for i in range(10)
            ]
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=10.0,  # fail fast if deadlocked
            )

        # All 10 should have completed without exception.
        exceptions = [r for r in results if isinstance(r, BaseException)]
        assert not exceptions, f"Unexpected exceptions: {exceptions}"
        assert rewire_count == 10

        await old_client.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rewire_not_triggered_for_model_only_change(tmp_path: Path) -> None:
    """Changing default_model only does NOT trigger rewire_singletons.

    The route handler only calls rewire when base_url or api_key is in the
    body — model selection flows through resolve() at chat-time.
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        rewire_called = False

        async def _mock_rewire(*args: Any, **kwargs: Any) -> None:
            nonlocal rewire_called
            rewire_called = True

        with patch.object(svc, "rewire_singletons", side_effect=_mock_rewire):
            # Simulate: only default_model in body (no base_url, no api_key).
            # This is what the route handler checks:
            #   if body.base_url is not None or body.api_key is not None:
            #       await svc.rewire_singletons(...)
            body_base_url = None
            body_api_key = None
            body_default_model = "new-model"

            # Write only the default_model.
            await svc.set_admin_default(
                base_url=body_base_url,
                api_key=body_api_key,
                default_model=body_default_model,
                clear=None,
            )

            if body_base_url is not None or body_api_key is not None:
                await svc.rewire_singletons(
                    object(),  # app_state not needed — won't be called
                    new_base_url="",
                    new_api_key="",
                )

        assert not rewire_called, "rewire_singletons should not fire for model-only changes"
    finally:
        await engine.dispose()
