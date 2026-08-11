# SPDX-License-Identifier: Apache-2.0
"""Regression guard: chat-title generation must use the live base URL.

After a rewire, generate_chat_title must use the adapter's LIVE _base_url,
not the boot-frozen settings.lm_studio_base_url.

The original bug: routes/chats.py read ``request.app.state.settings.lm_studio_base_url``
which is set once at startup and never changes.  After an admin saves a new URL,
all title-gen calls still landed on the old env URL.

The fix: read ``request.app.state.lmstudio_adapter._base_url`` which is mutated
by rewire_singletons.

This test drives generate_chat_title via the FastAPI TestClient with a mocked
ChatService.generate_title that captures the base_url argument passed to it.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI as _FastAPI
from fastapi.testclient import TestClient

from lmchat.app import create_app
from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_default_session_store_dep, get_engine_dep
from lmchat.routes.chats import _get_chat_service, _get_message_service
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.chat_service import ChatService
from lmchat.session.sqlite_store import SQLiteSessionStore

_ENV_URL = "http://env_original_host_1234"
_REWIRED_URL = "http://rewired_admin_host_5678"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_STUDIO_BASE_URL", _ENV_URL)
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()


@pytest.mark.asyncio
async def test_chat_title_generation_uses_rewired_base_url(tmp_path: Path) -> None:
    """generate_chat_title reads adapter._base_url, not settings.lm_studio_base_url.

    Steps:
    1. Build app with env URL = _ENV_URL.
    2. Manually set app.state.lmstudio_adapter._base_url = _REWIRED_URL
       (simulating what rewire_singletons does after an admin saves a new URL).
    3. Call POST /api/chats/{id}/generate-title.
    4. Assert ChatService.generate_title was called with base_url=_REWIRED_URL,
       NOT base_url=_ENV_URL.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    db_path = tmp_path / "title_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    try:
        # Track what base_url is passed to generate_title.
        captured_base_url: list[str] = []

        async def _mock_generate_title(
            chat_id: int,
            *,
            user_id: int,
            http_client: Any,
            base_url: str,
            fallback_model_id: Any,
        ) -> str:
            captured_base_url.append(base_url)
            return "Generated Title"

        mock_chat_svc = MagicMock(spec=ChatService)
        mock_chat_svc.generate_title = _mock_generate_title

        stub_msg_svc = AsyncMock()

        app = create_app()
        store = SQLiteSessionStore(engine=engine)

        app.dependency_overrides[get_engine_dep] = lambda: engine
        app.dependency_overrides[get_default_session_store_dep] = lambda: store
        app.dependency_overrides[_get_chat_service] = lambda: mock_chat_svc
        app.dependency_overrides[_get_message_service] = lambda: stub_msg_svc

        with TestClient(app, raise_server_exceptions=True) as client:
            # Simulate post-rewire state: adapter._base_url is now the rewired URL.
            _app = cast(_FastAPI, client.app)
            _app.state.lmstudio_adapter._base_url = _REWIRED_URL
            _app.state.session_store = store
            _app.state.admin_buckets = InMemoryBucketStore()
            _app.state.stream_buckets = InMemoryBucketStore()
            _app.state.models_service.list_loaded = AsyncMock(return_value=[])

            # Register and login a user (username: letters + digits + underscores only).
            reg = client.post(
                "/api/auth/register",
                data={"username": "title_test_user", "password": "password123"},
            )
            assert reg.status_code == 201, reg.text

            login = client.post(
                "/api/auth/login",
                data={"username": "title_test_user", "password": "password123"},
            )
            assert login.status_code == 200, login.text

            # Call generate-title. generate_title mock ignores chat_id / DB.
            resp = client.post("/api/chats/999/generate-title")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["title"] == "Generated Title"

        # THE KEY ASSERTION: rewired URL was passed, not the env URL.
        assert len(captured_base_url) == 1, (
            "generate_title should have been called once"
        )
        actual_url = captured_base_url[0]
        assert actual_url == _REWIRED_URL, (
            f"generate_title received base_url={actual_url!r} "
            f"but expected the rewired URL {_REWIRED_URL!r}. "
            f"This means the route is still reading from settings (env URL = {_ENV_URL!r}) "
            f"instead of from lmstudio_adapter._base_url."
        )
        assert actual_url != _ENV_URL, (
            f"generate_title received the env URL {_ENV_URL!r} — "
            f"C-1 secondary bug is NOT fixed."
        )
    finally:
        await engine.dispose()
