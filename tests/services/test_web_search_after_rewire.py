# SPDX-License-Identifier: Apache-2.0
"""Tests for WebSearchService.reconfigure() (C-1 follow-up).

Without reconfigure(), WebSearchService._http continues to hold a reference
to the old (eventually closed) httpx.AsyncClient.  After OLD_CLIENT_GRACE_SECONDS
the old client's aclose() fires and every SearXNG probe raises RuntimeError.

reconfigure() rebinds _http to the new client when _owns_http is False (i.e.
the client was injected at construction time from the shared http_client).

Covers:
- reconfigure rebinds _http to the new client when _owns_http is False.
- reconfigure is a no-op when _owns_http is True (service owns its client).
- After reconfigure, probe() uses the new client (verify via mock).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from lmchat.services.web_search_service import WebSearchService


@pytest.mark.asyncio
async def test_web_search_works_after_rewire_grace_period() -> None:
    """After rebinding via reconfigure(), SearXNG probe uses the new client.

    Simulates what happens after OLD_CLIENT_GRACE_SECONDS when the old client
    is closed: reconfigure() must have been called so _http points at the new
    client, not the closed one.
    """
    # Construct WebSearchService with an injected client (_owns_http=False).
    # Use a public-looking URL to pass the SSRF guard (no network call made;
    # probe() is mocked below).
    old_client = httpx.AsyncClient(timeout=5.0, follow_redirects=False)
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://searxng.example.com",  # fake public URL; probe is mocked
        http_client=old_client,
    )
    assert svc._owns_http is False
    assert svc._http is old_client

    # Build a new client (simulating what rewire_singletons creates).
    new_client = httpx.AsyncClient(timeout=5.0, follow_redirects=False)

    # Rebind.
    svc.reconfigure(http_client=new_client)
    assert svc._http is new_client, (
        "reconfigure() did not rebind _http to the new client"
    )
    assert svc._http is not old_client, (
        "_http still points at old client after reconfigure"
    )

    # Verify that probe() uses the new client (mock the GET call on new_client).
    probe_resp = MagicMock()
    probe_resp.status_code = 200
    probe_resp.json = MagicMock(return_value={"results": []})
    new_client.get = AsyncMock(return_value=probe_resp)

    result = await svc.probe()
    assert result is True
    new_client.get.assert_called_once()

    await old_client.aclose()
    await new_client.aclose()


@pytest.mark.asyncio
async def test_reconfigure_noop_when_owns_http() -> None:
    """When _owns_http=True, reconfigure() is a no-op (service manages lifecycle)."""
    # No http_client passed → service creates its own → _owns_http=True.
    svc = WebSearchService(
        provider="ddg",  # avoid searxng URL validation on construction
    )
    assert svc._owns_http is True
    original_client = svc._http

    new_client = httpx.AsyncClient(timeout=5.0, follow_redirects=False)
    svc.reconfigure(http_client=new_client)

    # Should not have been rebound.
    assert svc._http is original_client, (
        "reconfigure() should not rebind when _owns_http=True"
    )

    await new_client.aclose()
    await svc.aclose()


@pytest.mark.asyncio
async def test_reconfigure_called_by_rewire_singletons() -> None:
    """rewire_singletons calls web_search_service.reconfigure(http_client=new_client).

    This is a contract test verifying the plumbing: the mock web_search_service
    attached to app.state has reconfigure() called with the new httpx.AsyncClient
    after rewire_singletons completes.
    """
    import asyncio
    import tempfile
    import types
    from unittest.mock import patch

    from sqlalchemy.ext.asyncio import create_async_engine

    from lmchat.config import Settings
    from lmchat.db.schema import metadata
    from lmchat.services.lm_studio_overrides_service import LmStudioOverridesService

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp}/ws_contract.db",
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
                await svc.rewire_singletons(
                    state,
                    new_base_url="http://new.host:5678",
                    new_api_key="new-key",
                )

            # The key assertion: reconfigure was called with the new client.
            new_http_client = state.http_client
            web_svc_mock.reconfigure.assert_called_once_with(http_client=new_http_client)

            await old_http.aclose()
            await new_http_client.aclose()
        finally:
            await engine.dispose()
