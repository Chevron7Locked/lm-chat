# SPDX-License-Identifier: Apache-2.0
"""Integration tests for P13j chat-share endpoints (audit O-05).

Covers:
- POST /api/chats/{id}/share — mints a token, idempotent.
- GET  /api/chats/{id}/share — returns current share or null.
- DELETE /api/chats/{id}/share — revokes the share.
- GET  /api/share/{token} — public read-only view (no auth).
- R-15 PRIVACY INVARIANT: incognito chats are unshareable (POST returns
  403; GET /api/share/{token} double-checks at read time).

The fixtures piggyback on the same per-test SQLite engine used by
``test_chats.py``; share endpoints are mounted by ``create_app()`` so
the existing test_client fixture exercises them too.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    get_default_session_store_dep,
    get_engine_dep,
)
from lmchat.routes.chats import _get_chat_service, _get_message_service
from lmchat.routes.share import (
    _get_chat_service as _share_get_chat_service,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.chat_service import ChatService
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.services.models_service import Capabilities, ModelsService
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/routes/test_chats.py — kept local to keep this
# spec runnable in isolation).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[None]:
    """Required env vars + settings cache clearing.

    ``LM_CHAT_DATABASE_URL`` points the lifespan's ``ensure_schema_ready``
    at a per-test path so it doesn't try to upgrade the dev ``./lmchat.db``
    (which is held open by the local backend in interactive sessions).
    """
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://localhost:1234")
    monkeypatch.setenv("LM_STUDIO_API_KEY", "test-key")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'lifespan_seed.db'}",
    )
    from lmchat.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Per-test SQLite engine with full schema."""
    db_path = tmp_path / "test_chats_share.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
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
def mock_models_service() -> MagicMock:
    svc = MagicMock(spec=ModelsService)
    svc.get_capabilities = AsyncMock(
        return_value=Capabilities(vision=False, trained_for_tool_use=False)
    )
    svc.list_loaded = AsyncMock(return_value=[])
    svc.refresh = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def mock_memory_service() -> MagicMock:
    svc = MagicMock(spec=MemoryService)
    svc.handle_message_deleted = AsyncMock(return_value=None)
    svc.index_message = AsyncMock(return_value=None)
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
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc
    # share router has its own module-local dep helpers (deliberate isolation
    # from the chats router so circular-import concerns at module load stay
    # contained); override those too so the public share-view endpoint sees
    # the SAME engine / chat_svc as the authenticated endpoints.
    app.dependency_overrides[_share_get_chat_service] = lambda: chat_svc
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_user_direct(
    engine: AsyncEngine,
    username: str,
    password: str = "correct-horse-battery",
) -> None:
    """Bypass the single-admin registration gate with a direct DB insert."""
    from lmchat.db.schema import users as users_table

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
    client.post("/api/auth/register", data={"username": username, "password": password})
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text


def _create_chat(
    client: TestClient,
    title: str = "share-me",
    incognito: bool = False,
) -> int:
    resp = client.post(
        "/api/chats",
        data={"title": title, "incognito": "true" if incognito else "false"},
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def _append(client: TestClient, chat_id: int, role: str, content: str) -> None:
    resp = client.post(
        f"/api/chats/{chat_id}/messages",
        data={"role": role, "content": content},
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# POST /api/chats/{id}/share
# ---------------------------------------------------------------------------


def test_create_share_mints_token_and_returns_url(test_client: TestClient) -> None:
    _register_and_login(test_client)
    cid = _create_chat(test_client)
    resp = test_client.post(f"/api/chats/{cid}/share")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert isinstance(body["token"], str)
    assert len(body["token"]) >= 20  # secrets.token_urlsafe(24) → 32 chars
    assert body["url"] == f"/share/{body['token']}"
    assert body["chat_id"] == cid


def test_create_share_is_idempotent(test_client: TestClient) -> None:
    """Re-POSTing returns the SAME token — no churn for double-clicks."""
    _register_and_login(test_client)
    cid = _create_chat(test_client)
    first = test_client.post(f"/api/chats/{cid}/share").json()
    second = test_client.post(f"/api/chats/{cid}/share").json()
    assert first["token"] == second["token"]


def test_create_share_404_for_unknown_chat(test_client: TestClient) -> None:
    _register_and_login(test_client)
    resp = test_client.post("/api/chats/9999/share")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_share_404_for_cross_user_chat(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Existence-leak prevention: cross-user share request returns 404."""
    _register_and_login(test_client, "alice")
    cid = _create_chat(test_client, "alice's chat")
    test_client.post("/api/auth/logout")
    # Insert bob directly to bypass the single-admin registration gate.
    await _insert_user_direct(db_engine, "bob")
    resp_login = test_client.post(
        "/api/auth/login", data={"username": "bob", "password": "correct-horse-battery"}
    )
    assert resp_login.status_code == 200, resp_login.text
    resp = test_client.post(f"/api/chats/{cid}/share")
    assert resp.status_code == 404


def test_create_share_403_for_incognito_chat(test_client: TestClient) -> None:
    """R-15 INVARIANT: incognito chats cannot be shared (returns 403)."""
    _register_and_login(test_client)
    cid = _create_chat(test_client, "secret", incognito=True)
    resp = test_client.post(f"/api/chats/{cid}/share")
    assert resp.status_code == 403
    assert "incognito" in resp.json()["detail"].lower()


def test_non_incognito_chat_can_be_shared(test_client: TestClient) -> None:
    """Companion to the invariant test: normal chats DO get 200/201."""
    _register_and_login(test_client)
    cid = _create_chat(test_client, "public-ok", incognito=False)
    resp = test_client.post(f"/api/chats/{cid}/share")
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# GET /api/chats/{id}/share
# ---------------------------------------------------------------------------


def test_get_share_returns_null_when_not_shared(test_client: TestClient) -> None:
    _register_and_login(test_client)
    cid = _create_chat(test_client)
    resp = test_client.get(f"/api/chats/{cid}/share")
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_share_returns_existing_token(test_client: TestClient) -> None:
    _register_and_login(test_client)
    cid = _create_chat(test_client)
    created = test_client.post(f"/api/chats/{cid}/share").json()
    fetched = test_client.get(f"/api/chats/{cid}/share").json()
    assert fetched["token"] == created["token"]


# ---------------------------------------------------------------------------
# DELETE /api/chats/{id}/share
# ---------------------------------------------------------------------------


def test_delete_share_revokes_token(test_client: TestClient) -> None:
    _register_and_login(test_client)
    cid = _create_chat(test_client)
    token = test_client.post(f"/api/chats/{cid}/share").json()["token"]
    # Public read works first…
    assert test_client.get(f"/api/share/{token}").status_code == 200
    # …then DELETE…
    assert test_client.delete(f"/api/chats/{cid}/share").status_code == 204
    # …and the public read 404s.
    assert test_client.get(f"/api/share/{token}").status_code == 404


def test_delete_share_idempotent(test_client: TestClient) -> None:
    """Deleting twice is fine (caller wants the chat un-shared)."""
    _register_and_login(test_client)
    cid = _create_chat(test_client)
    test_client.post(f"/api/chats/{cid}/share")
    assert test_client.delete(f"/api/chats/{cid}/share").status_code == 204
    assert test_client.delete(f"/api/chats/{cid}/share").status_code == 204


# ---------------------------------------------------------------------------
# GET /api/share/{token} — UNAUTHENTICATED public view
# ---------------------------------------------------------------------------


def test_public_share_renders_messages_without_auth(test_client: TestClient) -> None:
    _register_and_login(test_client)
    cid = _create_chat(test_client, "Hello world")
    _append(test_client, cid, "user", "ask")
    _append(test_client, cid, "assistant", "answer with [doc:abc]")
    token = test_client.post(f"/api/chats/{cid}/share").json()["token"]

    # Logout so the next request has no session cookie.
    test_client.post("/api/auth/logout")
    test_client.cookies.clear()
    resp = test_client.get(f"/api/share/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Hello world"
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][1]["content"] == "answer with [doc:abc]"
    # Ownership / private columns are NOT leaked in the public envelope.
    assert "user_id" not in body
    assert "settings" not in body


def test_public_share_404_for_unknown_token(test_client: TestClient) -> None:
    test_client.cookies.clear()
    resp = test_client.get("/api/share/this-token-does-not-exist")
    assert resp.status_code == 404


def test_public_share_404_after_revocation(test_client: TestClient) -> None:
    _register_and_login(test_client)
    cid = _create_chat(test_client)
    token = test_client.post(f"/api/chats/{cid}/share").json()["token"]
    test_client.delete(f"/api/chats/{cid}/share")
    test_client.cookies.clear()
    resp = test_client.get(f"/api/share/{token}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# R-15 invariant: incognito chats are NEVER shareable, end-to-end
# ---------------------------------------------------------------------------


def test_r15_invariant_incognito_cannot_share_then_normal_can(
    test_client: TestClient,
) -> None:
    """Pinned R-15: paired creation — incognito → 403, normal → 201.

    This is the explicit "invariant test" required by the P13j scope:
    create an incognito chat, attempt share, assert 403; create a
    non-incognito chat, share, assert 200/201.
    """
    _register_and_login(test_client)

    incognito_id = _create_chat(test_client, "private", incognito=True)
    incognito_resp = test_client.post(f"/api/chats/{incognito_id}/share")
    assert incognito_resp.status_code == 403

    normal_id = _create_chat(test_client, "public", incognito=False)
    normal_resp = test_client.post(f"/api/chats/{normal_id}/share")
    assert normal_resp.status_code == 201
    # Sanity: the public viewer can read the normal chat.
    token = normal_resp.json()["token"]
    test_client.cookies.clear()
    assert test_client.get(f"/api/share/{token}").status_code == 200
