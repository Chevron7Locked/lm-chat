# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the search route.

Tests for the /api/search routes.

Route tested:
- GET /api/search?q=...&chat_id=...&limit=...

The search backend is FTS5 on SQLite.  Tests verify:
- Matching messages are returned.
- ``chat_id`` filter scopes results to a single chat.
- ``limit`` is respected.
- Unauthenticated requests → 401.
- Messages from another user are never returned.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
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
)
from lmchat.routes.chats import (
    _get_chat_service,
)
from lmchat.routes.chats import (
    _get_message_service as _get_message_service_chats,
)
from lmchat.routes.search import _get_message_service
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.chat_service import ChatService
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.services.models_service import Capabilities, ModelsService
from lmchat.session.sqlite_store import SQLiteSessionStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Required env vars + settings cache clearing."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Per-test SQLite engine with full schema + FTS5 virtual table."""
    db_path = tmp_path / "test_search.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # FTS5 virtual table + sync triggers (mirrors migration 0002).
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


@pytest.fixture()
def mock_memory_service() -> MagicMock:
    """MemoryService with mocked handle_message_deleted."""
    svc = MagicMock(spec=MemoryService)
    svc.handle_message_deleted = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def mock_models_service() -> MagicMock:
    """ModelsService with mocked get_capabilities."""
    svc = MagicMock(spec=ModelsService)
    svc.get_capabilities = AsyncMock(
        return_value=Capabilities(vision=False, trained_for_tool_use=False)
    )
    svc.list_loaded = AsyncMock(return_value=[])
    svc.refresh = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def chat_locks() -> dict[int, asyncio.Lock]:
    """Fresh per-chat lock dict."""
    return {}


@pytest.fixture()
def chat_svc(
    db_engine: AsyncEngine,
    mock_memory_service: MagicMock,
    mock_models_service: MagicMock,
    chat_locks: dict[int, asyncio.Lock],
) -> ChatService:
    """Real ChatService backed by test DB."""
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
    """Real MessageService backed by test DB."""
    return MessageService(
        engine=db_engine,
        memory_service=mock_memory_service,
    )


@pytest.fixture()
def test_client(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> Generator[TestClient]:
    """TestClient wired to per-test engine + real services."""
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)

    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service_chats] = lambda: message_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LOW_N: int = 2**10


async def _insert_user_async(engine: AsyncEngine, username: str, password: str) -> None:
    """Bypass the single-admin gate by inserting a user directly (async)."""
    from sqlalchemy import func, select

    from lmchat.db.schema import users as users_table
    from lmchat.utils.hashing import hash_password

    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    async with engine.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users_table.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": int(next_id or 1), "u": username, "ph": pw_hash},
        )


def _register_and_login(
    client: TestClient,
    username: str = "alice",
    password: str = "correct-horse-battery",
) -> None:
    """Register and log in a user (sync — first user only, no gate)."""
    client.post("/api/auth/register", data={"username": username, "password": password})
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"


def _create_chat_and_messages(
    client: TestClient,
    messages: list[str],
    title: str = "test chat",
) -> dict:  # type: ignore[type-arg]
    """Create a chat and append multiple messages, return the chat dict."""
    chat_resp = client.post("/api/chats", data={"title": title})
    assert chat_resp.status_code == 201
    chat = chat_resp.json()
    for content in messages:
        resp = client.post(
            f"/api/chats/{chat['id']}/messages",
            data={"role": "user", "content": content},
        )
        assert resp.status_code == 201
    return chat


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------


def test_search_returns_matching_messages(test_client: TestClient) -> None:
    """GET /api/search?q=... returns messages matching the query."""
    _register_and_login(test_client)
    _create_chat_and_messages(
        test_client,
        ["the quick brown fox", "another message entirely"],
    )

    resp = test_client.get("/api/search?q=quick+brown")
    assert resp.status_code == 200
    results = resp.json()
    contents = [m["content"] for m in results]
    assert any("quick brown" in c for c in contents)


def test_search_with_chat_id_filter(test_client: TestClient) -> None:
    """GET /api/search?q=...&chat_id=N scopes results to that chat."""
    _register_and_login(test_client)
    chat_a = _create_chat_and_messages(
        test_client, ["unique keyword in A"], title="chat A"
    )
    _create_chat_and_messages(
        test_client, ["unique keyword in B"], title="chat B"
    )

    # Search scoped to chat_a — should only return messages from chat_a.
    resp = test_client.get(f"/api/search?q=unique+keyword&chat_id={chat_a['id']}")
    assert resp.status_code == 200
    results = resp.json()
    assert all(m["chat_id"] == chat_a["id"] for m in results), (
        f"Got messages from unexpected chats: {[m['chat_id'] for m in results]}"
    )


def test_search_respects_limit(test_client: TestClient) -> None:
    """GET /api/search?q=...&limit=2 returns at most 2 results."""
    _register_and_login(test_client)
    _create_chat_and_messages(
        test_client,
        [f"search term number {i}" for i in range(5)],
    )

    resp = test_client.get("/api/search?q=search+term&limit=2")
    assert resp.status_code == 200
    assert len(resp.json()) <= 2


def test_search_requires_auth(test_client: TestClient) -> None:
    """GET /api/search without session → 401."""
    resp = test_client.get("/api/search?q=anything")
    assert resp.status_code == 401


@pytest.mark.asyncio()
async def test_search_other_user_messages_not_returned(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search never returns messages from another user."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service_chats] = lambda: message_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        # Alice creates a message with a unique keyword.
        await _insert_user_async(db_engine, "alice_srch", "correct-horse-battery")
        client.post("/api/auth/login", data={"username": "alice_srch", "password": "correct-horse-battery"})
        _create_chat_and_messages(client, ["uniqueword only alice has"])

        # Bob logs in and searches for the same keyword.
        await _insert_user_async(db_engine, "bob_srch", "correct-horse-battery")
        client.post("/api/auth/login", data={"username": "bob_srch", "password": "correct-horse-battery"})
        resp = client.get("/api/search?q=uniqueword")
        assert resp.status_code == 200
        assert len(resp.json()) == 0, "Bob should not see Alice's messages"

    get_settings.cache_clear()
