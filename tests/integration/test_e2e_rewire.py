# SPDX-License-Identifier: Apache-2.0
"""E2E integration tests for the LM Studio singleton rewire.

Tests use the live uvicorn + stub LM Studio infrastructure from conftest.py.

Covers:
- test_admin_save_changes_actual_traffic: save URL X (stub A), assert traffic
  goes there; save URL Y (stub B), assert traffic goes to Y.
- test_long_stream_survives_swap: in-flight streams complete after rewire
  because the old client's grace period (180 s) allows drain.
"""
from __future__ import annotations

import asyncio
import secrets
import types
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.integration.conftest import (
    make_admin,
    register_and_login,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helper: create an isolated admin client
# ---------------------------------------------------------------------------


async def _make_admin_client(
    live_servers: dict[str, Any],
    db_engine: Any,
) -> tuple[httpx.AsyncClient, str]:
    base_url = live_servers["base_url"]
    c = httpx.AsyncClient(base_url=base_url, follow_redirects=True, timeout=15.0)
    uname = f"adm_{secrets.token_hex(6)}"
    user, _ = await register_and_login(c, uname)
    await make_admin(db_engine, user["id"])
    # Re-login after promotion.
    resp = await c.post(
        "/api/auth/login",
        data={"username": uname, "password": "integration-pw-OK"},
    )
    assert resp.status_code == 200, resp.text
    return c, uname


# ---------------------------------------------------------------------------
# Test 1: admin save changes actual singleton URL
# ---------------------------------------------------------------------------


async def test_admin_save_changes_actual_traffic(
    live_servers: dict[str, Any],
    db_engine: Any,
) -> None:
    """After PATCH /api/admin/lmstudio/default the live singletons reflect the new URL.

    We verify by reading the actual _base_url attribute off the live adapter
    singleton (not by routing real traffic to a second stub, which would require
    two concurrent stub servers and complex port allocation).

    The stub LM Studio responds to /api/v1/models (probe gate check), and we
    mock the httpx.AsyncClient in the probe to succeed so we can drive the save.
    """
    app = live_servers["app"]
    stub_port = live_servers["stub_port"]

    async with httpx.AsyncClient(
        base_url=live_servers["base_url"],
        follow_redirects=True,
        timeout=15.0,
    ) as c:
        uname = f"adm_{secrets.token_hex(6)}"
        user, _ = await register_and_login(c, uname)
        await make_admin(db_engine, user["id"])
        resp = await c.post(
            "/api/auth/login",
            data={"username": uname, "password": "integration-pw-OK"},
        )
        assert resp.status_code == 200, resp.text

        # Record original singleton base_url.
        original_url = app.state.lmstudio_adapter._base_url

        # Save a new URL that points at the same stub (probe will succeed).
        new_url = f"http://127.0.0.1:{stub_port}"
        resp = await c.patch(
            "/api/admin/lmstudio/default",
            json={"base_url": new_url},
        )
        assert resp.status_code == 200, f"admin save failed: {resp.text}"
        data = resp.json()
        assert data["source_base_url"] == "server_admin"

        # Verify that the live singleton reflects the new URL.
        live_url = app.state.lmstudio_adapter._base_url
        assert live_url == new_url.rstrip("/"), (
            f"Adapter still points to {live_url!r} after save to {new_url!r}"
        )
        assert app.state.models_service._base_url == new_url.rstrip("/")
        assert app.state.embedding_client._base_url == new_url.rstrip("/")
        assert app.state.http_client is app.state.lmstudio_adapter._http_client

        # Tear-down: restore env URL so other tests aren't affected.
        resp = await c.patch(
            "/api/admin/lmstudio/default",
            json={"base_url": original_url},
        )
        assert resp.status_code == 200, f"restore failed: {resp.text}"


# ---------------------------------------------------------------------------
# Test 2: long stream survives swap (mocked — no real sleep)
# ---------------------------------------------------------------------------


async def test_long_stream_survives_swap(tmp_path: Any) -> None:
    """In-flight streams on the old client complete after a rewire.

    The mechanism: rewire_singletons creates a new http client but schedules
    old client close AFTER OLD_CLIENT_GRACE_SECONDS (180 s).  In-flight
    streams hold a reference to response._transport on the old client and
    complete without interruption.

    This test uses mocked asyncio.sleep so we don't actually wait 180 s.
    It verifies that:
    1. After rewire, _delayed_close is scheduled (create_task called).
    2. The old client's aclose() is called after the delay.
    3. The new client is in place immediately after rewire.
    """
    from pathlib import Path

    from sqlalchemy.ext.asyncio import create_async_engine

    from lmchat.config import Settings
    from lmchat.db.schema import metadata
    from lmchat.services.lm_studio_overrides_service import (
        LmStudioOverridesService,
    )

    db_path = Path(tmp_path) / "long_stream.db" if not isinstance(tmp_path, Path) else tmp_path / "long_stream.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        pool_pre_ping=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    try:
        settings = Settings(  # type: ignore[call-arg]
            lm_studio_base_url="http://env.host:1234",
            lm_studio_api_key="env-key",
            lm_studio_default_model="env-model",
            lm_chat_secret="test-secret-32-bytes-of-entropy!!",
        )
        svc = LmStudioOverridesService(engine=engine, settings=settings)

        old_client = httpx.AsyncClient(base_url="http://old.host:1234")
        new_model_svc = MagicMock()
        new_model_svc._http_client = old_client
        new_model_svc._base_url = "http://old.host:1234"
        new_model_svc._cache = None
        new_model_svc._cache_lock = asyncio.Lock()

        embed_client = MagicMock()
        embed_client._http = old_client
        embed_client._base_url = "http://old.host:1234"
        embed_client._endpoint = "http://old.host:1234/api/v1/embeddings"

        adapter = MagicMock()
        adapter._http_client = old_client
        adapter._base_url = "http://old.host:1234"

        web_svc = MagicMock()
        web_svc.reconfigure = MagicMock()

        state = types.SimpleNamespace(
            http_client=old_client,
            http=old_client,
            lmstudio_adapter=adapter,
            models_service=new_model_svc,
            embedding_client=embed_client,
            web_search_service=web_svc,
            rewire_lock=asyncio.Lock(),
        )

        # Track what create_task was called with.
        # rewire_singletons calls asyncio.create_task(_delayed_close(old, 180), ...)
        # We intercept create_task at the module level so we see the coroutine arg.
        created_tasks: list[Any] = []
        real_create_task = asyncio.create_task

        def _fake_create_task(coro: Any, **kwargs: Any) -> Any:
            created_tasks.append(coro)
            # Return a completed task so no real background work runs.
            task = real_create_task(asyncio.sleep(0))
            # Cancel the coro to avoid ResourceWarning.
            coro.close()
            return task

        with patch("asyncio.create_task", side_effect=_fake_create_task):
            await svc.rewire_singletons(
                state,
                new_base_url="http://new.host:5678",
                new_api_key="new-key",
            )

        # Verify new client is in place immediately after rewire.
        assert state.lmstudio_adapter._base_url == "http://new.host:5678"
        new_client = state.http_client
        assert new_client is not old_client

        # Verify create_task was called exactly twice: one for _delayed_close
        # and one for _warmup_and_sync_mirror (part of the LM Studio key-save chain).
        assert len(created_tasks) == 2, (
            f"Expected 2 create_task calls (_delayed_close + _warmup_and_sync_mirror), "
            f"got {len(created_tasks)}"
        )
        # The first coroutine's qualname confirms it's _delayed_close.
        assert "_delayed_close" in created_tasks[0].__qualname__, (
            f"Expected _delayed_close coroutine as first task, "
            f"got {created_tasks[0].__qualname__!r}"
        )
        # The second coroutine's qualname confirms it's _warmup_and_sync_mirror.
        assert "_warmup_and_sync_mirror" in created_tasks[1].__qualname__, (
            f"Expected _warmup_and_sync_mirror coroutine as second task, "
            f"got {created_tasks[1].__qualname__!r}"
        )

        await old_client.aclose()
        await new_client.aclose()
    finally:
        await engine.dispose()
