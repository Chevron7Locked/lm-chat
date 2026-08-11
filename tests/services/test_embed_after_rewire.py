# SPDX-License-Identifier: Apache-2.0
"""Tests that EmbeddingClient routes to the rewired URL after a swap (R-6).

R-6 in the v1 risk register was flagged HIGH/CERTAIN but had no test coverage.
This file closes that gap.

Covers:
- After rewire_singletons, EmbeddingClient._endpoint is updated to the new URL.
- embed_one() calls the new URL (verified via mock on the new http_client).
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lmchat.embedding.client import EmbeddingClient
from lmchat.services.lm_studio_overrides_service import (
    EMBEDDINGS_PATH_CONST,
    LmStudioOverridesService,
)


@pytest.fixture(autouse=True)
def _seed_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_embed_routes_to_rewired_url(tmp_path) -> None:
    """After rewire, EmbeddingClient._endpoint points at the new URL.

    Also verifies embed_one() calls are routed to the new endpoint.
    """
    from pathlib import Path

    from sqlalchemy.ext.asyncio import create_async_engine

    from lmchat.config import Settings
    from lmchat.db.schema import metadata

    db_path = Path(tmp_path) / "embed_rewire.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
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
        new_base = "http://new.host:5678"

        # Build a real EmbeddingClient pointing at the old base.
        embed_client = EmbeddingClient(
            http_client=old_http,
            base_url="http://old.host:1234",
        )
        assert embed_client._endpoint == "http://old.host:1234/v1/embeddings"

        models_svc = MagicMock()
        models_svc._http_client = old_http
        models_svc._base_url = "http://old.host:1234"
        models_svc._cache = None
        models_svc._cache_lock = asyncio.Lock()

        adapter = MagicMock()
        adapter._http_client = old_http
        adapter._base_url = "http://old.host:1234"

        web_svc = MagicMock()
        web_svc.reconfigure = MagicMock()

        state = types.SimpleNamespace(
            http_client=old_http,
            http=old_http,
            lmstudio_adapter=adapter,
            models_service=models_svc,
            embedding_client=embed_client,
            web_search_service=web_svc,
            rewire_lock=asyncio.Lock(),
        )

        with patch(
            "lmchat.services.lm_studio_overrides_service._delayed_close",
            new=AsyncMock(),
        ):
            await svc.rewire_singletons(
                state,
                new_base_url=new_base,
                new_api_key="new-key",
            )

        # EmbeddingClient attributes all updated.
        assert embed_client._base_url == new_base
        assert embed_client._endpoint == f"{new_base}{EMBEDDINGS_PATH_CONST}"
        assert embed_client._endpoint == f"{new_base}/v1/embeddings"
        assert embed_client._http is state.http_client

        # Verify embed_one routes to the new endpoint by mocking the http client.
        new_http = state.http_client
        embedding_resp_data = {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1] * 4}],
            "model": "embed-model",
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value=embedding_resp_data)
        mock_response.text = str(embedding_resp_data)
        new_http.post = AsyncMock(return_value=mock_response)

        result = await embed_client.embed_one(text="hello world", model_id="embed-model")
        assert result is not None
        assert len(result) == 4

        # Verify the POST was to the new endpoint URL.
        new_http.post.assert_called_once()
        call_args = new_http.post.call_args
        called_url = call_args[0][0] if call_args[0] else call_args[1].get("url")
        assert called_url is not None
        assert "new.host" in called_url or new_base in called_url, (
            f"embed_one called {called_url!r} instead of the new URL {new_base!r}"
        )

        await old_http.aclose()
        await state.http_client.aclose()
    finally:
        await engine.dispose()
