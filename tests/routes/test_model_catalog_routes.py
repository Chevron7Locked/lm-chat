# SPDX-License-Identifier: Apache-2.0
"""Route-level tests for the catalog merge + chat provider param (W1 + W2-BE).

Covers:
- GET /api/models: returns LM Studio models when no catalog on app.state
  (legacy/test path).
- GET /api/models: uses catalog when app.state.model_catalog present.
- GET /api/models: includes cloud provider models; lmstudio-only unchanged
  when no cloud providers (regression test).
- GET /api/providers/status: returns empty list when no catalog; returns
  per-provider status when catalog present.
- PATCH /api/chats/{id}: accepts provider field; persists settings.provider.
- PATCH /api/chats/{id}: unknown provider → 400.
- PATCH /api/chats/{id}: empty provider treated as "lmstudio".
- PATCH /api/chats/{id}: no registry on app.state → no validation (safe
  fallback in test environments).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    get_default_session_store_dep,
    get_engine_dep,
    get_models_service_dep,
)
from lmchat.routes.chats import _get_chat_service, _get_message_service
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.chat_service import ChatService
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.services.model_catalog import ModelCatalogService
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService
from lmchat.session.sqlite_store import SQLiteSessionStore

# ---------------------------------------------------------------------------
# Env fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()


# ---------------------------------------------------------------------------
# DB / schema fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    db_path = tmp_path / "test_catalog_routes.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # FTS5 virtual table needed by ChatService
        await conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='id',
                tokenize='porter unicode61'
            )
        """))
        for ddl in [
            """CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
               END""",
            """CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF content ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                    VALUES('delete', old.id, old.content);
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
               END""",
            """CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                    VALUES('delete', old.id, old.content);
               END""",
        ]:
            await conn.execute(text(ddl))
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Service fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_models_service() -> MagicMock:
    svc = MagicMock(spec=ModelsService)
    svc.list_loaded = AsyncMock(
        return_value=[
            ModelInfo(
                key="local-model",
                capabilities=Capabilities(vision=False, trained_for_tool_use=False),
                provider="lmstudio",
            )
        ]
    )
    svc.refresh = AsyncMock(return_value=None)
    svc.get_capabilities = AsyncMock(
        return_value=Capabilities(vision=False, trained_for_tool_use=False)
    )
    return svc


@pytest.fixture()
def mock_memory_service() -> MagicMock:
    svc = MagicMock(spec=MemoryService)
    svc.handle_message_deleted = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def chat_locks() -> dict[int, asyncio.Lock]:
    return {}


@pytest.fixture()
def chat_svc(
    db_engine: AsyncEngine,
    mock_memory_service: MagicMock,
    mock_models_service: MagicMock,
    chat_locks: dict[int, asyncio.Lock],
) -> ChatService:
    return ChatService(
        engine=db_engine,
        memory_service=mock_memory_service,
        models_service=mock_models_service,
        chat_locks=chat_locks,
    )


@pytest.fixture()
def message_svc(
    db_engine: AsyncEngine,
    mock_memory_service: MagicMock,
) -> MessageService:
    return MessageService(engine=db_engine, memory_service=mock_memory_service)


# ---------------------------------------------------------------------------
# App / client factory helpers
# ---------------------------------------------------------------------------


def _build_client(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
    *,
    catalog: ModelCatalogService | None = None,
    provider_registry: Any = None,
) -> TestClient:
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)

    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_models_service_dep] = lambda: mock_models_service
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    # Wrap to set app.state in the with block
    class _Client:
        def __init__(self) -> None:
            self._tc = TestClient(app, raise_server_exceptions=True)

        def __enter__(self) -> TestClient:
            client = self._tc.__enter__()
            client.app.state.session_store = store  # type: ignore[attr-defined]
            client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
            client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
            if catalog is not None:
                # Override the lifespan-created catalog with the test stub.
                client.app.state.model_catalog = catalog  # type: ignore[attr-defined]
            else:
                # Explicitly clear so the fallback path (svc.list_loaded) is used.
                client.app.state.model_catalog = None  # type: ignore[attr-defined]
            # Always override provider_registry (even None) to isolate from
            # the lifespan-created registry which knows only "lmstudio" and
            # would reject unknown provider names.
            client.app.state.provider_registry = provider_registry  # type: ignore[attr-defined]
            return client

        def __exit__(self, *args: Any) -> None:
            self._tc.__exit__(*args)

    return _Client()  # type: ignore[return-value]


def _register_and_login(client: TestClient, username: str = "alice") -> None:
    password = "correct-horse-battery"
    client.post("/api/auth/register", data={"username": username, "password": password})
    resp = client.post("/api/auth/login", data={"username": username, "password": password})
    assert resp.status_code == 200, f"login failed: {resp.text}"


def _create_chat(client: TestClient) -> int:
    resp = client.post("/api/chats", data={"title": "test chat"})
    assert resp.status_code == 201, f"create chat failed: {resp.text}"
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# GET /api/models — catalog merge
# ---------------------------------------------------------------------------


def test_get_models_without_catalog_falls_back_to_list_loaded(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """GET /api/models without app.state.model_catalog falls back to list_loaded."""
    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc, catalog=None
    ) as client:
        _register_and_login(client)
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["key"] == "local-model"
        assert body[0]["provider"] == "lmstudio"


def test_get_models_with_catalog_includes_cloud_models(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """GET /api/models with catalog returns merged LM Studio + cloud models."""
    # Build a catalog that returns LM Studio + one cloud model.
    cloud_stub = MagicMock()
    cloud_stub.list_models_detailed = AsyncMock(
        return_value=(
            [{"id": "openai/gpt-4o", "context_length": 128000}],
            200,
            None,
        )
    )
    registry = MagicMock()
    registry.names = MagicMock(return_value=["lmstudio", "openrouter"])
    registry.get = MagicMock(return_value=cloud_stub)

    catalog = ModelCatalogService(
        models_svc=mock_models_service,
        registry=registry,
    )

    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc, catalog=catalog
    ) as client:
        _register_and_login(client)
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        keys = {m["key"] for m in body}
        assert "local-model" in keys
        assert "openai/gpt-4o" in keys
        providers = {m["provider"] for m in body}
        assert "lmstudio" in providers
        assert "openrouter" in providers


def test_get_models_lmstudio_only_unchanged(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """REGRESSION: when catalog has only lmstudio, result is identical to list_loaded.

    The output list must be exactly the LM Studio models with no extra items
    and no change to the lmstudio provider field.
    """
    # Registry with only lmstudio.
    registry = MagicMock()
    registry.names = MagicMock(return_value=["lmstudio"])

    catalog = ModelCatalogService(
        models_svc=mock_models_service,
        registry=registry,
    )

    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc, catalog=catalog
    ) as client:
        _register_and_login(client)
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["key"] == "local-model"
        assert body[0]["provider"] == "lmstudio"
        # No cloud models leaked in.
        assert all(m["provider"] == "lmstudio" for m in body)


# ---------------------------------------------------------------------------
# GET /api/providers/status
# ---------------------------------------------------------------------------


def test_provider_status_without_catalog_returns_empty(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """GET /api/providers/status with no catalog → [] (200)."""
    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc, catalog=None
    ) as client:
        _register_and_login(client)
        resp = client.get("/api/providers/status")
        assert resp.status_code == 200
        assert resp.json() == []


def test_provider_status_with_unreachable_provider(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """GET /api/providers/status reports reachable=False for a failed provider."""
    slow_prov = MagicMock()
    slow_prov.list_models_detailed = AsyncMock(
        return_value=([], None, "Connection refused")
    )
    registry = MagicMock()
    registry.names = MagicMock(return_value=["lmstudio", "openrouter"])
    registry.get = MagicMock(return_value=slow_prov)

    catalog = ModelCatalogService(
        models_svc=mock_models_service,
        registry=registry,
    )

    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc, catalog=catalog
    ) as client:
        _register_and_login(client)
        # Trigger a fetch first.
        client.get("/api/models")
        resp = client.get("/api/providers/status")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["provider"] == "openrouter"
        assert body[0]["reachable"] is False


def test_provider_status_requires_auth(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """GET /api/providers/status without auth → 401."""
    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc
    ) as client:
        resp = client.get("/api/providers/status")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PATCH /api/chats/{id} — provider field
# ---------------------------------------------------------------------------


def _make_registry_with(known_providers: list[str]) -> MagicMock:
    """Build a mock ProviderRegistry that knows the given provider names."""
    reg = MagicMock()
    registry_map = {n: MagicMock() for n in known_providers}

    def _get(name: str) -> Any:
        return registry_map.get(name)

    reg.get = MagicMock(side_effect=_get)
    return reg


def test_patch_chat_provider_persists_to_settings(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """PATCH /api/chats/{id} with provider=openrouter writes settings.provider."""
    registry = _make_registry_with(["lmstudio", "openrouter"])

    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc,
        provider_registry=registry,
    ) as client:
        _register_and_login(client)
        chat_id = _create_chat(client)

        resp = client.patch(
            f"/api/chats/{chat_id}",
            data={"provider": "openrouter"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["settings"]["provider"] == "openrouter"


def test_patch_chat_provider_unknown_returns_400(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """PATCH /api/chats/{id} with an unknown provider slug → 400."""
    registry = _make_registry_with(["lmstudio"])  # only lmstudio known

    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc,
        provider_registry=registry,
    ) as client:
        _register_and_login(client)
        chat_id = _create_chat(client)

        resp = client.patch(
            f"/api/chats/{chat_id}",
            data={"provider": "unknown-provider"},
        )
        assert resp.status_code == 400
        assert "unknown-provider" in resp.json()["detail"]


def test_patch_chat_provider_switch_to_lmstudio(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """PATCH provider='lmstudio' switches back from a cloud provider.

    Note: FastAPI coerces empty string form fields to None (same as "omit").
    To explicitly select lmstudio, pass provider='lmstudio' (the slug).
    An empty string value is treated as "no change" by FastAPI's Form handling.
    """
    registry = _make_registry_with(["lmstudio", "openrouter"])

    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc,
        provider_registry=registry,
    ) as client:
        _register_and_login(client)
        chat_id = _create_chat(client)

        # First set to openrouter.
        resp = client.patch(f"/api/chats/{chat_id}", data={"provider": "openrouter"})
        assert resp.status_code == 200
        assert resp.json()["settings"]["provider"] == "openrouter"

        # Now switch back to lmstudio using the explicit slug.
        resp = client.patch(f"/api/chats/{chat_id}", data={"provider": "lmstudio"})
        assert resp.status_code == 200
        assert resp.json()["settings"]["provider"] == "lmstudio"


def test_patch_chat_provider_without_registry_does_not_validate(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """When no registry on app.state, provider writes succeed without validation.

    This is the test-environment safe fallback (registry not wired in the
    lightweight test fixture).
    """
    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc,
        provider_registry=None,  # no registry
    ) as client:
        _register_and_login(client)
        chat_id = _create_chat(client)

        resp = client.patch(
            f"/api/chats/{chat_id}",
            data={"provider": "any-value"},
        )
        assert resp.status_code == 200
        assert resp.json()["settings"]["provider"] == "any-value"


def test_patch_chat_provider_independent_of_model_id(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """provider and model_id can be set independently in the same PATCH."""
    registry = _make_registry_with(["lmstudio", "openrouter"])

    with _build_client(
        db_engine, mock_models_service, chat_svc, message_svc,
        provider_registry=registry,
    ) as client:
        _register_and_login(client)
        chat_id = _create_chat(client)

        resp = client.patch(
            f"/api/chats/{chat_id}",
            data={"provider": "openrouter", "model_id": "meta-llama/llama-3.3-70b"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["settings"]["provider"] == "openrouter"
        assert body["model_id"] == "meta-llama/llama-3.3-70b"
