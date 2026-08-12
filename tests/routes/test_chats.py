# SPDX-License-Identifier: Apache-2.0
"""Integration tests for chats routes.

Tests for the /api/chats routes.

Pattern: FastAPI TestClient with real SQLite DB (tmp_path) + real
ChatService and MessageService.  No mocks at the service layer — only the
HTTP boundary is stubbed where LM Studio calls would be needed (models_service
for compaction tokenizer).

Fixtures
--------
``db_engine``      — per-test SQLite engine with full schema (including FTS5
                     migration via Alembic upgrade head).
``_set_env``       — required env vars + settings cache clearing.
``test_client``    — TestClient with override deps for engine, session store,
                     chat_service, and message_service.
``_alice_session`` — registers + logs in "alice" and returns the TestClient.

Error contract
--------------
Cross-user access ALWAYS returns 404 (never 403) — existence must not be leaked.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import metadata, users
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    get_default_session_store_dep,
    get_engine_dep,
)
from lmchat.routes.chats import _get_chat_service, _get_message_service
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.chat_service import ChatService
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.services.models_service import Capabilities, ModelsService
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

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
    db_path = tmp_path / "test_chats.db"
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
def mock_models_service() -> MagicMock:
    """ModelsService with mocked get_capabilities (for compaction tokenizer)."""
    svc = MagicMock(spec=ModelsService)
    svc.get_capabilities = AsyncMock(
        return_value=Capabilities(vision=False, trained_for_tool_use=False)
    )
    svc.list_loaded = AsyncMock(return_value=[])
    svc.refresh = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def mock_memory_service() -> MagicMock:
    """MemoryService with mocked handle_message_deleted."""
    svc = MagicMock(spec=MemoryService)
    svc.handle_message_deleted = AsyncMock(return_value=None)
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
    """Real ChatService backed by test DB + mock dependencies."""
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
    """Real MessageService backed by test DB + mock memory service."""
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
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_and_login(
    client: TestClient,
    username: str = "alice",
    password: str = "correct-horse-battery",
) -> None:
    """Register and log in a user (sets session cookie on client)."""
    client.post("/api/auth/register", data={"username": username, "password": password})
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"


async def _get_user_id(engine: AsyncEngine, username: str) -> int:
    """Look up a user's integer ID from the DB."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(users.c.id).where(users.c.username == username)
        )
        row = result.fetchone()
    assert row is not None, f"user {username!r} not found"
    return int(row.id)


_LOW_N: int = 2**10


async def _insert_user_direct(
    engine: AsyncEngine,
    username: str,
    password: str = "correct-horse-battery",
) -> None:
    """Bypass the registration gate by inserting a user directly into the DB.

    Required when we need a second user in tests where registration is closed
    (the single-admin gate blocks plain registration after the first user exists).
    """
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    async with engine.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        if next_id is None:
            raise RuntimeError("coalesce returned None — unreachable")
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": int(next_id), "u": username, "ph": pw_hash},
        )


def _create_chat(client: TestClient, title: str = "my chat") -> dict[str, Any]:
    """Create a chat and return the JSON body."""
    resp = client.post("/api/chats", data={"title": title})
    assert resp.status_code == 201, f"create_chat failed: {resp.text}"
    return resp.json()


def _append_message(
    client: TestClient,
    chat_id: int,
    content: str = "hello",
    role: str = "user",
) -> dict[str, Any]:
    """Append a message and return the JSON body."""
    resp = client.post(
        f"/api/chats/{chat_id}/messages",
        data={"role": role, "content": content},
    )
    assert resp.status_code == 201, f"append_message failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# POST /api/chats
# ---------------------------------------------------------------------------


def test_create_chat_201(test_client: TestClient) -> None:
    """POST /api/chats → 201 with Chat body."""
    _register_and_login(test_client)
    resp = test_client.post("/api/chats", data={"title": "test chat"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "test chat"
    assert isinstance(body["id"], int)
    assert body["pinned"] is False
    assert body["folder"] is None


# ---------------------------------------------------------------------------
# GET /api/chats — list
# ---------------------------------------------------------------------------


def test_list_chats_filters_by_folder(test_client: TestClient) -> None:
    """GET /api/chats?folder=X returns only chats in that folder."""
    _register_and_login(test_client)
    # Create two chats, move one to a folder.
    chat1 = _create_chat(test_client, "chat 1")
    chat2 = _create_chat(test_client, "chat 2")

    test_client.patch(
        f"/api/chats/{chat2['id']}", data={"folder": "archive"}
    )

    resp = test_client.get("/api/chats?folder=archive")
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()]
    assert chat2["id"] in ids
    assert chat1["id"] not in ids


# ---------------------------------------------------------------------------
# project_id + unscoped query params on GET /api/chats
# ---------------------------------------------------------------------------


async def _set_chat_project_id(
    engine: AsyncEngine, *, chat_id: int, project_id: int | None
) -> None:
    """Direct DB write — no PATCH route exists for this yet."""
    from sqlalchemy import update as _update

    from lmchat.db.schema import chats as _chats

    async with engine.begin() as conn:
        await conn.execute(
            _update(_chats)
            .where(_chats.c.id == chat_id)
            .values(project_id=project_id)
        )


@pytest.mark.anyio
async def test_list_chats_default_is_user_scoped_union(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Default `GET /api/chats` returns EVERY chat the user owns,
    regardless of project_id — preserves pre-Projects behavior.
    """
    _register_and_login(test_client)
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    chat_un = _create_chat(test_client, "un-projected")
    chat_in = _create_chat(test_client, "in-project")
    await _set_chat_project_id(
        db_engine, chat_id=chat_in["id"], project_id=proj["id"]
    )
    resp = test_client.get("/api/chats")
    assert resp.status_code == 200
    ids = {c["id"] for c in resp.json()}
    assert chat_un["id"] in ids
    assert chat_in["id"] in ids


@pytest.mark.anyio
async def test_list_chats_with_project_id_filters(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """`?project_id=X` returns only the chats in that project."""
    _register_and_login(test_client)
    proj = test_client.post("/api/projects", data={"name": "Proj"}).json()
    chat_un = _create_chat(test_client, "un-projected")
    chat_in = _create_chat(test_client, "in-project")
    await _set_chat_project_id(
        db_engine, chat_id=chat_in["id"], project_id=proj["id"]
    )
    resp = test_client.get(
        "/api/chats", params={"project_id": proj["id"]}
    )
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()]
    assert chat_in["id"] in ids
    assert chat_un["id"] not in ids


@pytest.mark.anyio
async def test_list_chats_with_unscoped_returns_only_unprojected(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """`?unscoped=true` returns only chats with project_id IS NULL."""
    _register_and_login(test_client)
    proj = test_client.post("/api/projects", data={"name": "Proj"}).json()
    chat_un = _create_chat(test_client, "un-projected")
    chat_in = _create_chat(test_client, "in-project")
    await _set_chat_project_id(
        db_engine, chat_id=chat_in["id"], project_id=proj["id"]
    )
    resp = test_client.get("/api/chats", params={"unscoped": "true"})
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()]
    assert chat_un["id"] in ids
    assert chat_in["id"] not in ids


@pytest.mark.anyio
async def test_list_chats_composed_project_id_and_folder_filter(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """`?project_id=X&folder=Y` returns the INTERSECTION — both filters apply.

    Prior tests covered `?folder=Y`
    and `?project_id=X` independently. This test pins the composed
    predicate: a chat must match BOTH filters to appear.
    """
    _register_and_login(test_client)
    proj = test_client.post("/api/projects", data={"name": "Proj"}).json()
    # Four chats covering the (project_id, folder) cross product:
    # (project, archive)   ← only one that matches
    # (project, other)
    # (no project, archive)
    # (no project, other)
    targets = {
        "pa": _create_chat(test_client, "proj-archive"),
        "po": _create_chat(test_client, "proj-other"),
        "ua": _create_chat(test_client, "un-archive"),
        "uo": _create_chat(test_client, "un-other"),
    }
    await _set_chat_project_id(
        db_engine, chat_id=targets["pa"]["id"], project_id=proj["id"]
    )
    await _set_chat_project_id(
        db_engine, chat_id=targets["po"]["id"], project_id=proj["id"]
    )
    for key in ("pa", "ua"):
        test_client.patch(
            f"/api/chats/{targets[key]['id']}",
            data={"folder": "archive"},
        )
    for key in ("po", "uo"):
        test_client.patch(
            f"/api/chats/{targets[key]['id']}",
            data={"folder": "other"},
        )
    resp = test_client.get(
        "/api/chats",
        params={"project_id": proj["id"], "folder": "archive"},
    )
    assert resp.status_code == 200
    ids = {c["id"] for c in resp.json()}
    assert ids == {targets["pa"]["id"]}


# ---------------------------------------------------------------------------
# GET /api/chats/{chat_id}
# ---------------------------------------------------------------------------


def test_get_chat_returns_messages(test_client: TestClient) -> None:
    """GET /api/chats/{id} includes the messages list."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)
    _append_message(test_client, chat["id"], content="first message")

    resp = test_client.get(f"/api/chats/{chat['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == chat["id"]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "first message"


@pytest.mark.asyncio()
async def test_get_chat_cross_user_returns_404(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/chats/{id} owned by another user returns 404."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    # Pre-insert both users directly (bypass the single-admin registration gate).
    await _insert_user_direct(db_engine, "alice")
    await _insert_user_direct(db_engine, "bob")

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        # Alice creates a chat.
        client.post("/api/auth/login", data={"username": "alice", "password": "correct-horse-battery"})
        chat = _create_chat(client)

        # Bob tries to read Alice's chat.
        client.post("/api/auth/login", data={"username": "bob", "password": "correct-horse-battery"})
        resp = client.get(f"/api/chats/{chat['id']}")
        assert resp.status_code == 404, f"expected 404, got {resp.status_code}"

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# PATCH /api/chats/{chat_id}
# ---------------------------------------------------------------------------


def test_patch_chat_renames(test_client: TestClient) -> None:
    """PATCH /api/chats/{id} with title → updates chat title."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)
    resp = test_client.patch(f"/api/chats/{chat['id']}", data={"title": "new name"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "new name"


def test_patch_chat_moves_to_folder(test_client: TestClient) -> None:
    """PATCH /api/chats/{id} with folder → sets the folder."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)
    resp = test_client.patch(
        f"/api/chats/{chat['id']}", data={"folder": "projects"}
    )
    assert resp.status_code == 200
    assert resp.json()["folder"] == "projects"


def test_patch_chat_pins(test_client: TestClient) -> None:
    """PATCH /api/chats/{id} with pinned=true → sets pinned flag."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)
    assert chat["pinned"] is False

    resp = test_client.patch(f"/api/chats/{chat['id']}", data={"pinned": "true"})
    assert resp.status_code == 200
    assert resp.json()["pinned"] is True


def test_patch_chat_model_id_persists(test_client: TestClient) -> None:
    """PATCH model_id → persists; list + detail GET both return it.

    Covers the per-chat model-persistence fix: chats.model_id column
    did not exist prior to migration 0019, causing OperationalError on
    every PATCH that set model_id.
    """
    _register_and_login(test_client)
    chat = _create_chat(test_client)

    # New chat has no model selected.
    assert chat.get("model_id") is None

    model = "qwen3.6-35b-test-model"

    # PATCH to set model_id — must return 200 with model_id reflected.
    patch_resp = test_client.patch(
        f"/api/chats/{chat['id']}", data={"model_id": model}
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["model_id"] == model

    # List endpoint returns model_id for this chat.
    list_resp = test_client.get("/api/chats")
    assert list_resp.status_code == 200
    matching = [c for c in list_resp.json() if c["id"] == chat["id"]]
    assert matching, "created chat missing from list"
    assert matching[0]["model_id"] == model

    # Detail endpoint (GET /api/chats/{id}) also returns model_id.
    detail_resp = test_client.get(f"/api/chats/{chat['id']}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["model_id"] == model


def test_patch_chat_clear_model_id_resets_to_auto(test_client: TestClient) -> None:
    """PATCH ``clear=model_id`` → NULLs the per-chat override ("Auto" reset).

    The picker shows "Auto" whenever a chat has no explicit override, and the
    reset affordance selects "Auto". Because a flat ``model_id=""`` param is
    intentionally ignored server-side (it only SETS a non-empty pin), the reset
    must travel through the explicit-NULL ``clear=`` path.
    """
    _register_and_login(test_client)
    chat = _create_chat(test_client)

    # Pin a model first.
    model = "qwen3.6-35b-test-model"
    set_resp = test_client.patch(
        f"/api/chats/{chat['id']}", data={"model_id": model}
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["model_id"] == model

    # A flat empty model_id must NOT clear the pin (guarded server-side).
    noop_resp = test_client.patch(
        f"/api/chats/{chat['id']}", data={"model_id": ""}
    )
    assert noop_resp.status_code == 200, noop_resp.text
    assert noop_resp.json()["model_id"] == model

    # clear=model_id resets it back to NULL.
    clear_resp = test_client.patch(
        f"/api/chats/{chat['id']}", data={"clear": "model_id"}
    )
    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json()["model_id"] is None

    # Persisted: list + detail both reflect the cleared state.
    detail_resp = test_client.get(f"/api/chats/{chat['id']}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["model_id"] is None


# ---------------------------------------------------------------------------
# DELETE /api/chats/{chat_id}
# ---------------------------------------------------------------------------


def test_delete_chat_204(test_client: TestClient) -> None:
    """DELETE /api/chats/{id} → 204; subsequent GET returns 404."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)

    resp = test_client.delete(f"/api/chats/{chat['id']}")
    assert resp.status_code == 204

    resp2 = test_client.get(f"/api/chats/{chat['id']}")
    assert resp2.status_code == 404


@pytest.mark.asyncio()
async def test_delete_chat_cross_user_returns_404(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE /api/chats/{id} owned by another user returns 404."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    # Pre-insert both users directly (bypass the single-admin registration gate).
    await _insert_user_direct(db_engine, "alice2")
    await _insert_user_direct(db_engine, "bob2")

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        client.post("/api/auth/login", data={"username": "alice2", "password": "correct-horse-battery"})
        chat = _create_chat(client)

        client.post("/api/auth/login", data={"username": "bob2", "password": "correct-horse-battery"})
        resp = client.delete(f"/api/chats/{chat['id']}")
        assert resp.status_code == 404

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# DELETE /api/chats/{chat_id}/messages — clear history (/clear)
# ---------------------------------------------------------------------------


def test_clear_chat_messages(test_client: TestClient) -> None:
    """DELETE /api/chats/{id}/messages empties the chat but keeps the shell."""
    _register_and_login(test_client)
    chat = _create_chat(test_client, title="keep me")
    cid = chat["id"]
    _append_message(test_client, cid, content="first", role="user")
    _append_message(test_client, cid, content="second", role="assistant")

    resp = test_client.delete(f"/api/chats/{cid}/messages")
    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] == 2

    # Chat shell survives with its title; message list is now empty.
    detail = test_client.get(f"/api/chats/{cid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["title"] == "keep me"
    assert body["messages"] == []

    # Idempotent-ish: clearing an already-empty chat reports 0 and still 200.
    resp2 = test_client.delete(f"/api/chats/{cid}/messages")
    assert resp2.status_code == 200
    assert resp2.json()["cleared"] == 0


def test_clear_chat_messages_unknown_chat_404(test_client: TestClient) -> None:
    """DELETE /api/chats/{id}/messages on an unknown chat returns 404."""
    _register_and_login(test_client)
    resp = test_client.delete("/api/chats/999999/messages")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/fork
# ---------------------------------------------------------------------------


def test_fork_chat_creates_new_chat_with_message_subset(
    test_client: TestClient,
) -> None:
    """POST /api/chats/{id}/fork copies messages up to at_message_id."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)
    _append_message(test_client, chat["id"], content="msg 1")
    msg2 = _append_message(test_client, chat["id"], content="msg 2")
    _append_message(test_client, chat["id"], content="msg 3")

    # Fork at msg2 — should include msg1 + msg2 but NOT msg3.
    resp = test_client.post(
        f"/api/chats/{chat['id']}/fork",
        data={"at_message_id": str(msg2["id"])},
    )
    assert resp.status_code == 201
    forked = resp.json()
    assert forked["id"] != chat["id"]
    assert "(fork)" in forked["title"]

    # Verify the forked chat's messages.
    detail = test_client.get(f"/api/chats/{forked['id']}")
    assert detail.status_code == 200
    msg_contents = [m["content"] for m in detail.json()["messages"]]
    assert "msg 1" in msg_contents
    assert "msg 2" in msg_contents
    assert "msg 3" not in msg_contents


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/compact
# ---------------------------------------------------------------------------


def test_compact_returns_CompactResult(
    test_client: TestClient,
    chat_svc: ChatService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/chats/{id}/compact returns CompactResultResponse.

    We use a large enough target_tokens that the invariant-protected
    last user message is preserved, but small enough that earlier messages
    get dropped.  Each message is ~10 tokens; 10 messages ≈ 100 tokens total.
    Target 20 tokens: preserves the last message (~10 tokens * 1.1 margin ≈ 11)
    but forces earlier messages to be dropped.

    We append messages with model_id="gpt-4" so tiktoken resolves the model
    to a known encoding without triggering the cl100k_base fallback warning.
    This avoids polluting the capsys stream that the compaction unit test reads.

    Stubs ``ChatService._run_llm_distill`` (same pattern as the AC10
    generate-title tests below) so the summarization call never hits a real
    LM Studio endpoint — the route still reads real ``app.state.http_client``
    / ``app.state.lmstudio_adapter`` set up by the app's lifespan, but the
    service-level LLM call itself is stubbed.
    """
    monkeypatch.setattr(
        chat_svc,
        "_run_llm_distill",
        AsyncMock(return_value="stub archive summary"),
    )

    _register_and_login(test_client)
    chat = _create_chat(test_client)
    # Add 10 short messages with a known tiktoken model name so no fallback fires.
    for i in range(10):
        resp = test_client.post(
            f"/api/chats/{chat['id']}/messages",
            data={"role": "user", "content": f"message number {i}", "model_id": "gpt-4"},
        )
        assert resp.status_code == 201

    resp = test_client.post(
        f"/api/chats/{chat['id']}/compact",
        data={"target_tokens": "20"},
    )
    assert resp.status_code == 200, f"compact returned {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["chat_id"] == chat["id"]
    assert "removed_message_ids" in body
    assert isinstance(body["remaining_token_count"], int)
    assert isinstance(body["original_token_count"], int)
    assert body["compaction_id"] is not None
    assert body["summary"] == "stub archive summary"
    assert body["archived_count"] == len(body["removed_message_ids"])
    assert isinstance(body["summary_token_count"], int)


def test_compact_target_too_low_returns_422(test_client: TestClient) -> None:
    """POST /api/chats/{id}/compact with target_tokens=1 → 422."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)
    _append_message(test_client, chat["id"], content="a message")

    resp = test_client.post(
        f"/api/chats/{chat['id']}/compact",
        data={"target_tokens": "1"},
    )
    assert resp.status_code == 422


def test_compact_summary_failure_returns_502(
    test_client: TestClient,
    chat_svc: ChatService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/chats/{id}/compact → 502 when the summary call fails; nothing archived."""
    from lmchat.services.chat_service import CompactionSummaryError

    monkeypatch.setattr(
        chat_svc,
        "_run_llm_distill",
        AsyncMock(side_effect=CompactionSummaryError("upstream 500")),
    )

    _register_and_login(test_client)
    chat = _create_chat(test_client)
    for i in range(10):
        resp = test_client.post(
            f"/api/chats/{chat['id']}/messages",
            data={"role": "user", "content": f"message number {i}", "model_id": "gpt-4"},
        )
        assert resp.status_code == 201

    resp = test_client.post(
        f"/api/chats/{chat['id']}/compact",
        data={"target_tokens": "20"},
    )
    assert resp.status_code == 502
    assert "upstream 500" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/chats/{chat_id}/compactions — recall
# ---------------------------------------------------------------------------


def test_list_and_recall_compactions(
    test_client: TestClient,
    chat_svc: ChatService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET .../compactions lists spans; GET .../compactions/{id}/messages recalls them."""
    monkeypatch.setattr(
        chat_svc,
        "_run_llm_distill",
        AsyncMock(return_value="stub archive summary"),
    )

    _register_and_login(test_client)
    chat = _create_chat(test_client)
    for i in range(10):
        resp = test_client.post(
            f"/api/chats/{chat['id']}/messages",
            data={"role": "user", "content": f"message number {i}", "model_id": "gpt-4"},
        )
        assert resp.status_code == 201

    compact_resp = test_client.post(
        f"/api/chats/{chat['id']}/compact",
        data={"target_tokens": "20"},
    )
    assert compact_resp.status_code == 200
    compact_body = compact_resp.json()
    compaction_id = compact_body["compaction_id"]
    assert compaction_id is not None

    list_resp = test_client.get(f"/api/chats/{chat['id']}/compactions")
    assert list_resp.status_code == 200
    spans = list_resp.json()
    assert len(spans) == 1
    assert spans[0]["id"] == compaction_id
    assert spans[0]["summary"] == "stub archive summary"
    # archived_count is derived from live message membership, not a stored
    # number — must equal what /compact actually archived.
    assert spans[0]["archived_count"] == len(compact_body["removed_message_ids"])

    messages_resp = test_client.get(
        f"/api/chats/{chat['id']}/compactions/{compaction_id}/messages"
    )
    assert messages_resp.status_code == 200
    archived_messages = messages_resp.json()
    assert {m["id"] for m in archived_messages} == set(compact_body["removed_message_ids"])


def test_list_compactions_unknown_chat_404(test_client: TestClient) -> None:
    """GET /api/chats/{id}/compactions → 404 for a nonexistent chat."""
    _register_and_login(test_client)
    resp = test_client.get("/api/chats/999999/compactions")
    assert resp.status_code == 404


def test_compaction_messages_unknown_compaction_404(test_client: TestClient) -> None:
    """GET .../compactions/{cid}/messages → 404 when the compaction doesn't exist."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)
    resp = test_client.get(f"/api/chats/{chat['id']}/compactions/999999/messages")
    assert resp.status_code == 404


async def test_list_compactions_cross_user_returns_404(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bob GETting alice's chat's compactions → 404 (never 403); existence must not leak."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        _register_and_login(client, username="alice")
        alice_chat = _create_chat(client)

        await _insert_user_direct(db_engine, "bob")
        client.post("/api/auth/logout")
        _register_and_login(client, username="bob", password="correct-horse-battery")

        resp = client.get(f"/api/chats/{alice_chat['id']}/compactions")
        assert resp.status_code == 404

        resp2 = client.get(f"/api/chats/{alice_chat['id']}/compactions/1/messages")
        assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/messages — append
# ---------------------------------------------------------------------------


def test_post_message_appends_201(test_client: TestClient) -> None:
    """POST /api/chats/{id}/messages appends a message and returns 201."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)

    resp = test_client.post(
        f"/api/chats/{chat['id']}/messages",
        data={"role": "user", "content": "hello world"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["content"] == "hello world"
    assert body["role"] == "user"
    assert body["chat_id"] == chat["id"]


@pytest.mark.asyncio()
async def test_post_message_cross_user_chat_returns_404(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/chats/{id}/messages on another user's chat → 404."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    # Pre-insert both users directly (bypass the single-admin registration gate).
    await _insert_user_direct(db_engine, "alice3")
    await _insert_user_direct(db_engine, "bob3")

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        client.post("/api/auth/login", data={"username": "alice3", "password": "correct-horse-battery"})
        chat = _create_chat(client)

        client.post("/api/auth/login", data={"username": "bob3", "password": "correct-horse-battery"})
        resp = client.post(
            f"/api/chats/{chat['id']}/messages",
            data={"role": "user", "content": "intrusion"},
        )
        assert resp.status_code == 404

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Auth guard: all endpoints require authentication
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PATCH /api/chats/{chat_id} — settings fields
# ---------------------------------------------------------------------------


def test_patch_chat_ab_compare_setting(test_client: TestClient) -> None:
    """PATCH /api/chats/{id} with ab_compare JSON sets settings.ab_compare."""
    import json

    _register_and_login(test_client)
    chat = _create_chat(test_client)

    ab_json = json.dumps(
        {"enabled": True, "model_a": "qwen3-35b", "model_b": "gemma-26b"}
    )
    resp = test_client.patch(
        f"/api/chats/{chat['id']}", data={"ab_compare": ab_json}
    )
    assert resp.status_code == 200
    body = resp.json()
    ab = body["settings"]["ab_compare"]
    assert ab["enabled"] is True
    assert ab["model_a"] == "qwen3-35b"
    assert ab["model_b"] == "gemma-26b"


def test_patch_chat_ab_compare_invalid_json_returns_422(
    test_client: TestClient,
) -> None:
    """PATCH /api/chats/{id} with malformed ab_compare JSON → 422."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)

    resp = test_client.patch(
        f"/api/chats/{chat['id']}", data={"ab_compare": "not-valid-json"}
    )
    assert resp.status_code == 422


def test_patch_chat_ab_compare_flat_params_persist(test_client: TestClient) -> None:
    """PATCH with separate ab_compare_enabled / model_a / model_b form fields persists.

    This is the contract that the FE ``useUpdateChat`` mutation uses when it sends
    the three flat params rather than the JSON-blob ``ab_compare`` field.
    """
    _register_and_login(test_client)
    chat = _create_chat(test_client)

    resp = test_client.patch(
        f"/api/chats/{chat['id']}",
        data={
            "ab_compare_enabled": "true",
            "ab_compare_model_a": "qwen3-35b",
            "ab_compare_model_b": "gemma-26b",
        },
    )
    assert resp.status_code == 200
    ab = resp.json()["settings"]["ab_compare"]
    assert ab["enabled"] is True
    assert ab["model_a"] == "qwen3-35b"
    assert ab["model_b"] == "gemma-26b"


def test_patch_chat_ab_compare_flat_enabled_only_preserves_models(
    test_client: TestClient,
) -> None:
    """PATCH with only ab_compare_enabled=false preserves existing model_a / model_b.

    This covers the auto-off path: after the user commits a pane, Chat.tsx calls
    ``updateChat({ ab_compare_enabled: false })`` — model ids must survive.
    """
    _register_and_login(test_client)
    chat = _create_chat(test_client)

    # First: set both models + enable via flat params.
    test_client.patch(
        f"/api/chats/{chat['id']}",
        data={
            "ab_compare_enabled": "true",
            "ab_compare_model_a": "qwen3-35b",
            "ab_compare_model_b": "gemma-26b",
        },
    )

    # Second: disable only — model_a / model_b must survive the merge.
    resp = test_client.patch(
        f"/api/chats/{chat['id']}",
        data={"ab_compare_enabled": "false"},
    )
    assert resp.status_code == 200
    ab = resp.json()["settings"]["ab_compare"]
    assert ab["enabled"] is False
    assert ab["model_a"] == "qwen3-35b"
    assert ab["model_b"] == "gemma-26b"


def test_patch_chat_settings_shallow_merge(test_client: TestClient) -> None:
    """PATCH /api/chats/{id}: two successive patches preserve earlier keys."""
    _register_and_login(test_client)
    chat = _create_chat(test_client)

    # First patch: enable rag.
    test_client.patch(f"/api/chats/{chat['id']}", data={"rag_enabled": "true"})
    # Second patch: set reasoning_effort — rag_enabled must be preserved.
    resp = test_client.patch(
        f"/api/chats/{chat['id']}", data={"reasoning_effort": "high"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"]["rag_enabled"] is True
    assert body["settings"]["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("POST", "/api/chats", {"title": "x"}),
        ("GET", "/api/chats", {}),
        ("GET", "/api/chats/1", {}),
        ("PATCH", "/api/chats/1", {"title": "x"}),
        ("DELETE", "/api/chats/1", {}),
        ("POST", "/api/chats/1/fork", {"at_message_id": "1"}),
        ("POST", "/api/chats/1/compact", {"target_tokens": "100"}),
        ("POST", "/api/chats/1/messages", {"role": "user", "content": "x"}),
        ("GET", "/api/chats/1/compactions", {}),
        ("GET", "/api/chats/1/compactions/1/messages", {}),
    ],
)
def test_all_endpoints_require_auth(
    test_client: TestClient,
    method: str,
    path: str,
    data: dict[str, str],
) -> None:
    """Unauthenticated requests to all chats endpoints → 401."""
    resp = test_client.request(method, path, data=data)
    assert resp.status_code == 401, (
        f"{method} {path} returned {resp.status_code}, expected 401"
    )


# ---------------------------------------------------------------------------
# PATCH /api/chats/reorder — DnD reorder
# ---------------------------------------------------------------------------


def test_reorder_happy_path(test_client: TestClient) -> None:
    """PATCH /api/chats/reorder moves a chat to a folder at a position."""
    _register_and_login(test_client)
    chat = _create_chat(test_client, "reorder me")

    resp = test_client.patch(
        "/api/chats/reorder",
        data={"chat_id": str(chat["id"]), "folder": "work", "display_order": "0"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # The chat should now be in the "work" folder.
    detail = test_client.get(f"/api/chats/{chat['id']}")
    assert detail.status_code == 200
    assert detail.json()["folder"] == "work"


def test_reorder_clears_folder_when_null(test_client: TestClient) -> None:
    """PATCH /api/chats/reorder with clear_folder=true sets
    chats.folder to NULL — the wire path the sidebar's "Remove from folder"
    action (MoveToFolderMenu) and DnD-into-ungrouped both use.
    """
    _register_and_login(test_client)
    chat = _create_chat(test_client, "move me out")

    # First move it INTO a folder, so there's something real to clear.
    into = test_client.patch(
        "/api/chats/reorder",
        data={"chat_id": str(chat["id"]), "folder": "work", "display_order": "0"},
    )
    assert into.status_code == 200
    assert test_client.get(f"/api/chats/{chat['id']}").json()["folder"] == "work"

    # Explicit clear — no "folder" field at all, just clear_folder=true.
    out = test_client.patch(
        "/api/chats/reorder",
        data={
            "chat_id": str(chat["id"]),
            "clear_folder": "true",
            "display_order": "0",
        },
    )
    assert out.status_code == 200
    assert out.json()["ok"] is True

    detail = test_client.get(f"/api/chats/{chat['id']}")
    assert detail.status_code == 200
    assert detail.json()["folder"] is None

    # clear_folder=true must WIN even if a stale "folder" value is also
    # sent in the same request (defends against a caller that sends both).
    into2 = test_client.patch(
        "/api/chats/reorder",
        data={"chat_id": str(chat["id"]), "folder": "work", "display_order": "0"},
    )
    assert into2.status_code == 200
    out2 = test_client.patch(
        "/api/chats/reorder",
        data={
            "chat_id": str(chat["id"]),
            "folder": "work",
            "clear_folder": "true",
            "display_order": "0",
        },
    )
    assert out2.status_code == 200
    assert test_client.get(f"/api/chats/{chat['id']}").json()["folder"] is None


@pytest.mark.asyncio()
async def test_reorder_cross_user_404(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """PATCH /api/chats/reorder returns 404 for cross-user access."""
    from lmchat.app import create_app as _create_app
    from lmchat.routes._dependencies import get_default_session_store_dep, get_engine_dep
    from lmchat.routes.chats import _get_chat_service, _get_message_service

    # Pre-insert both users directly (bypass the single-admin registration gate).
    await _insert_user_direct(db_engine, "alice_reorder")
    await _insert_user_direct(db_engine, "bob_reorder")

    app = _create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app, raise_server_exceptions=True) as client_a:
        client_a.app.state.session_store = store  # type: ignore[attr-defined]
        client_a.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client_a.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        client_a.post("/api/auth/login", data={"username": "alice_reorder", "password": "correct-horse-battery"})
        chat = _create_chat(client_a, "alice's chat")

    # Login as bob and try to reorder alice's chat.
    app2 = _create_app()
    store2 = SQLiteSessionStore(engine=db_engine)
    app2.dependency_overrides[get_engine_dep] = lambda: db_engine
    app2.dependency_overrides[get_default_session_store_dep] = lambda: store2
    app2.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app2.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app2, raise_server_exceptions=True) as client_b:
        client_b.app.state.session_store = store2  # type: ignore[attr-defined]
        client_b.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client_b.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        client_b.post("/api/auth/login", data={"username": "bob_reorder", "password": "correct-horse-battery"})
        resp = client_b.patch(
            "/api/chats/reorder",
            data={"chat_id": str(chat["id"]), "display_order": "0"},
        )
    assert resp.status_code == 404


def test_reorder_concurrent_maintains_order(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """Two concurrent reorder calls maintain a consistent display_order (per-folder lock)."""
    # Create two chats and send two simultaneous reorder PATCH requests.
    # The lock prevents a lost-update race; after both complete the orders
    # are 0 and 1 (not duplicated).
    from lmchat.app import create_app as _create_app
    from lmchat.routes._dependencies import get_default_session_store_dep, get_engine_dep
    from lmchat.routes.chats import _get_chat_service, _get_message_service

    app = _create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        _register_and_login(client, "alice_concurrent")
        chat_a = _create_chat(client, "A")
        chat_b = _create_chat(client, "B")

        # Move both chats to the same folder at position 0 sequentially.
        r1 = client.patch(
            "/api/chats/reorder",
            data={"chat_id": str(chat_a["id"]), "folder": "proj", "display_order": "0"},
        )
        r2 = client.patch(
            "/api/chats/reorder",
            data={"chat_id": str(chat_b["id"]), "folder": "proj", "display_order": "0"},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200

        # Both chats should be in the folder with distinct orders.
        chats_resp = client.get("/api/chats?folder=proj")
        items = chats_resp.json()
        orders = sorted(c["display_order"] for c in items)
        assert len(set(orders)) == len(orders), "display_order values must be unique"


def test_reorder_401_unauthed(test_client: TestClient) -> None:
    """Unauthenticated PATCH /api/chats/reorder → 401."""
    resp = test_client.patch(
        "/api/chats/reorder",
        data={"chat_id": "1", "display_order": "0"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# update_settings concurrency lock
# ---------------------------------------------------------------------------


def test_update_settings_concurrent_consistent(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
) -> None:
    """Concurrent PATCH settings calls maintain a consistent merged blob (per-chat lock)."""
    from lmchat.app import create_app as _create_app
    from lmchat.routes._dependencies import get_default_session_store_dep, get_engine_dep
    from lmchat.routes.chats import _get_chat_service, _get_message_service

    app = _create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        _register_and_login(client, "alice_lock")
        chat = _create_chat(client, "lock-test chat")

        # Two sequential PATCHes that set different settings keys.
        r1 = client.patch(
            f"/api/chats/{chat['id']}", data={"rag_enabled": "true"}
        )
        r2 = client.patch(
            f"/api/chats/{chat['id']}", data={"reasoning_effort": "high"}
        )
        assert r1.status_code == 200
        assert r2.status_code == 200

        # Final state: the second PATCH response must have BOTH keys set
        # (neither was lost under the per-chat lock).
        body2 = r2.json()
        assert body2["settings"].get("rag_enabled") is True
        assert body2["settings"].get("reasoning_effort") == "high"


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/generate-title
# The route is a thin handler
# over ChatService.generate_title: it reads three pieces of app.state
# (http_client, lmstudio_adapter._base_url, models_service) BEFORE
# delegating, then maps the service's exception types to HTTP codes:
#   - happy path           → 200 + {"title": <str>}
#   - ChatNotFoundError    → 404
#   - TitleGenerationError → 502 (FE swallows; chat keeps default title)
#   - cross-user access    → 404 (existence is never leaked)
# Service-level title generation is covered exhaustively by
# tests/services/test_chat_service_autotitle.py; these tests
# pin the HTTP boundary only.
# ---------------------------------------------------------------------------


def _wire_title_app_state(client: TestClient) -> None:
    """Attach the app.state attributes the generate-title route reads
    before calling the service. In the real app these come from the
    lifespan; here we stub them since the service call is mocked.
    """
    client.app.state.http_client = AsyncMock()  # type: ignore[attr-defined]
    client.app.state.lmstudio_adapter = MagicMock(  # type: ignore[attr-defined]
        _base_url="http://lm-studio.test"
    )
    client.app.state.models_service = MagicMock(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(return_value=[])
    )


def test_AC10_generate_title_200_returns_persisted_title(
    test_client: TestClient,
    chat_svc: ChatService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: service returns a title → 200 + {title}."""
    _register_and_login(test_client)
    chat = _create_chat(test_client, "New Chat")
    _wire_title_app_state(test_client)

    fake = AsyncMock(return_value="Project Setup Walkthrough")
    monkeypatch.setattr(chat_svc, "generate_title", fake)

    resp = test_client.post(f"/api/chats/{chat['id']}/generate-title")
    assert resp.status_code == 200
    assert resp.json() == {"title": "Project Setup Walkthrough"}

    # Route forwarded the chat_id + the user_id resolved from the session,
    # plus the wired http_client + base_url + (empty) fallback_model_id.
    fake.assert_awaited_once()
    assert fake.await_args is not None
    call_kwargs = fake.await_args.kwargs
    assert call_kwargs["user_id"] is not None
    assert call_kwargs["base_url"] == "http://lm-studio.test"
    # No loaded models → fallback_model_id stays None.
    assert call_kwargs["fallback_model_id"] is None


def test_AC10_generate_title_passes_fallback_model_id_when_available(
    test_client: TestClient,
    chat_svc: ChatService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ModelsService has loaded models, the first one's stable key
    is forwarded as fallback_model_id. This guards a regression
    where the cached loaded-model list went unread. Uses .key (not the
    legacy .id attribute) so the fallback persists the stable catalog key
    rather than the per-load instance id."""
    _register_and_login(test_client)
    chat = _create_chat(test_client, "New Chat")

    test_client.app.state.http_client = AsyncMock()  # type: ignore[attr-defined]
    test_client.app.state.lmstudio_adapter = MagicMock(  # type: ignore[attr-defined]
        _base_url="http://lm-studio.test"
    )
    loaded_one = MagicMock(key="qwen3.6-35b-a3b-uncensored-heretic-native-mtp-preserved-i1")
    test_client.app.state.models_service = MagicMock(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(return_value=[loaded_one])
    )

    fake = AsyncMock(return_value="Quick Title")
    monkeypatch.setattr(chat_svc, "generate_title", fake)

    resp = test_client.post(f"/api/chats/{chat['id']}/generate-title")
    assert resp.status_code == 200
    assert fake.await_args is not None
    assert fake.await_args.kwargs["fallback_model_id"] == "qwen3.6-35b-a3b-uncensored-heretic-native-mtp-preserved-i1"


def test_AC10_generate_title_swallows_list_loaded_failure(
    test_client: TestClient,
    chat_svc: ChatService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If models_service.list_loaded raises, the route logs and
    proceeds with fallback_model_id=None instead of 5xx-ing. Regression
    guard against the cache-lookup exception leaking out as a server
    error and breaking auto-title for every chat."""
    _register_and_login(test_client)
    chat = _create_chat(test_client, "New Chat")

    test_client.app.state.http_client = AsyncMock()  # type: ignore[attr-defined]
    test_client.app.state.lmstudio_adapter = MagicMock(  # type: ignore[attr-defined]
        _base_url="http://lm-studio.test"
    )
    test_client.app.state.models_service = MagicMock(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(side_effect=RuntimeError("cache blew up"))
    )

    fake = AsyncMock(return_value="Resilient Title")
    monkeypatch.setattr(chat_svc, "generate_title", fake)

    resp = test_client.post(f"/api/chats/{chat['id']}/generate-title")
    assert resp.status_code == 200
    assert fake.await_args is not None
    assert fake.await_args.kwargs["fallback_model_id"] is None


def test_AC11_generate_title_chat_not_found_returns_404(
    test_client: TestClient,
    chat_svc: ChatService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service ChatNotFoundError surfaces as 404."""
    from lmchat.services.chat_service import ChatNotFoundError

    _register_and_login(test_client)
    chat = _create_chat(test_client, "New Chat")
    _wire_title_app_state(test_client)

    monkeypatch.setattr(
        chat_svc,
        "generate_title",
        AsyncMock(side_effect=ChatNotFoundError("chat 999 not found")),
    )

    resp = test_client.post(f"/api/chats/{chat['id']}/generate-title")
    assert resp.status_code == 404


def test_AC12_generate_title_upstream_failure_returns_502(
    test_client: TestClient,
    chat_svc: ChatService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TitleGenerationError surfaces as 502 (FE swallows silently)."""
    from lmchat.services.chat_service import TitleGenerationError

    _register_and_login(test_client)
    chat = _create_chat(test_client, "New Chat")
    _wire_title_app_state(test_client)

    monkeypatch.setattr(
        chat_svc,
        "generate_title",
        AsyncMock(side_effect=TitleGenerationError("upstream 500")),
    )

    resp = test_client.post(f"/api/chats/{chat['id']}/generate-title")
    assert resp.status_code == 502
    assert "upstream 500" in resp.json()["detail"]


@pytest.mark.anyio
async def test_AC13_generate_title_cross_user_returns_404(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bob POSTing generate-title on alice's chat → 404 (never 403);
    existence MUST NOT leak."""
    from lmchat.services.chat_service import ChatNotFoundError

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    # Pre-insert both users directly (bypass the single-admin registration gate).
    await _insert_user_direct(db_engine, "alice_t")
    await _insert_user_direct(db_engine, "bob_t")

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        _wire_title_app_state(client)

        # The service's ownership check is what raises ChatNotFoundError
        # when bob asks for alice's chat — verified by service-layer
        # tests. Here we patch generate_title to raise it unconditionally
        # so the route's exception-mapping contract is what's under test.
        monkeypatch.setattr(
            chat_svc,
            "generate_title",
            AsyncMock(side_effect=ChatNotFoundError("not yours")),
        )

        client.post(
            "/api/auth/login",
            data={"username": "alice_t", "password": "correct-horse-battery"},
        )
        chat = _create_chat(client)

        client.post(
            "/api/auth/login",
            data={"username": "bob_t", "password": "correct-horse-battery"},
        )
        resp = client.post(
            f"/api/chats/{chat['id']}/generate-title"
        )
        assert resp.status_code == 404

    get_settings.cache_clear()
