# SPDX-License-Identifier: Apache-2.0
"""Route tests for P13l.1 — Edit message + Regenerate (audit O-12)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.app import create_app
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    get_default_session_store_dep,
    get_engine_dep,
)
from lmchat.routes.chats import _get_chat_service, _get_message_service
from lmchat.services.chat_service import ChatService
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


@pytest.fixture(autouse=True)
def _set_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None]:
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv(
        "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/p13l1.db"
    )
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()


@pytest_asyncio.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    from sqlalchemy.ext.asyncio import create_async_engine

    # The lifespan-driven ``ensure_schema_ready`` migration runs against
    # this URL on TestClient startup; we just need an engine handle for
    # the dependency override here.
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/p13l1.db", pool_pre_ping=True
    )
    yield eng
    await eng.dispose()


@pytest.fixture()
def services(db_engine: AsyncEngine):

    memory = MemoryService(
        engine=db_engine,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )
    msg = MessageService(engine=db_engine, memory_service=memory)
    chat = ChatService(
        engine=db_engine,
        memory_service=memory,
        models_service=AsyncMock(),
        chat_locks={},
    )
    return chat, msg


@pytest.fixture()
def test_client(
    db_engine: AsyncEngine,
    services,
) -> Generator[TestClient]:
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    chat_svc, msg_svc = services

    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: msg_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


def _insert_user_direct(engine: AsyncEngine, username: str, password: str) -> None:
    """Bypass the single-admin gate by inserting the user directly."""
    from lmchat.db.schema import users as users_table

    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)

    async def _do() -> None:
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

    asyncio.run(_do())


def _login(
    client: TestClient,
    user: str = "alice",
    pw: str = "correct-horse-battery",
    *,
    engine: AsyncEngine | None = None,
) -> None:
    if engine is not None:
        _insert_user_direct(engine, user, pw)
    else:
        client.post("/api/auth/register", data={"username": user, "password": pw})
    client.post("/api/auth/login", data={"username": user, "password": pw})


def _new_chat(client: TestClient, title: str = "T") -> int:
    resp = client.post("/api/chats", data={"title": title})
    assert resp.status_code == 201
    return int(resp.json()["id"])


def _post_message(client: TestClient, chat_id: int, role: str, content: str) -> int:
    resp = client.post(
        f"/api/chats/{chat_id}/messages",
        data={"role": role, "content": content},
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


# ---------------------------------------------------------------------------
# PATCH /api/messages/{id}
# ---------------------------------------------------------------------------


def test_edit_user_message_updates_content(test_client: TestClient) -> None:
    _login(test_client)
    chat_id = _new_chat(test_client)
    mid = _post_message(test_client, chat_id, "user", "hello")
    resp = test_client.patch(
        f"/api/messages/{mid}", data={"content": "edited"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "edited"


def test_edit_assistant_message_400(test_client: TestClient) -> None:
    _login(test_client)
    chat_id = _new_chat(test_client)
    mid = _post_message(test_client, chat_id, "assistant", "hi")
    resp = test_client.patch(
        f"/api/messages/{mid}", data={"content": "edited"}
    )
    assert resp.status_code == 400
    assert "only user messages are editable" in resp.json()["detail"]


def test_edit_message_404_for_missing(test_client: TestClient) -> None:
    _login(test_client)
    resp = test_client.patch(
        "/api/messages/999999", data={"content": "edited"}
    )
    assert resp.status_code == 404


def test_edit_message_403_for_other_user(test_client: TestClient, db_engine: AsyncEngine) -> None:
    _login(test_client, "alice", "correct-horse-battery-1", engine=db_engine)
    chat_id = _new_chat(test_client)
    mid = _post_message(test_client, chat_id, "user", "alice's")

    test_client.post("/api/auth/logout")
    _login(test_client, "bob", "correct-horse-battery-2", engine=db_engine)
    resp = test_client.patch(
        f"/api/messages/{mid}", data={"content": "tampered"}
    )
    assert resp.status_code == 403


def test_edit_message_works_on_incognito_chat(
    test_client: TestClient,
) -> None:
    """Editing in an incognito chat still works (R-15 unchanged)."""
    _login(test_client)
    resp = test_client.post(
        "/api/chats", data={"title": "X", "incognito": "true"}
    )
    assert resp.status_code == 201
    chat_id = int(resp.json()["id"])
    mid = _post_message(test_client, chat_id, "user", "hello")
    edit = test_client.patch(
        f"/api/messages/{mid}", data={"content": "edited"}
    )
    assert edit.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/messages/{message_id}/regenerate
# ---------------------------------------------------------------------------


def test_regenerate_without_confirm_412(test_client: TestClient) -> None:
    _login(test_client)
    chat_id = _new_chat(test_client)
    _post_message(test_client, chat_id, "user", "q")
    a_id = _post_message(test_client, chat_id, "assistant", "a")
    _post_message(test_client, chat_id, "user", "follow-up")

    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{a_id}/regenerate"
    )
    assert resp.status_code == 412, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "confirm_required"
    assert detail["subsequent_count"] >= 2
    assert detail["chat_id"] == chat_id
    assert detail["message_id"] == a_id


def test_regenerate_with_confirm_deletes(test_client: TestClient) -> None:
    _login(test_client)
    chat_id = _new_chat(test_client)
    _post_message(test_client, chat_id, "user", "q")
    a_id = _post_message(test_client, chat_id, "assistant", "a")
    _post_message(test_client, chat_id, "user", "follow-up")

    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{a_id}/regenerate?confirm=true"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] >= 2

    # Subsequent + boundary are gone.
    chat_resp = test_client.get(f"/api/chats/{chat_id}")
    assert chat_resp.status_code == 200
    ids = [m["id"] for m in chat_resp.json()["messages"]]
    assert a_id not in ids


def test_regenerate_user_message_now_resends(test_client: TestClient) -> None:
    """Passing a user-role message now performs a Resend (auto-confirm when last).

    The old behaviour (422 assistant-only guard) is superseded by the Resend
    feature: user messages are now accepted by the endpoint, and when the user
    message is already the last message in the chat the backend auto-confirms
    and returns 200. The boundary message itself is deleted (and replayed by
    the caller as a fresh turn), so deleted=1 even though nothing else in the
    chat is affected.
    """
    _login(test_client)
    chat_id = _new_chat(test_client)
    u_id = _post_message(test_client, chat_id, "user", "q")
    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{u_id}/regenerate?confirm=true"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 1
    assert body["prior_user_content"] == "q"


def test_regenerate_404_for_missing_message(test_client: TestClient) -> None:
    _login(test_client)
    chat_id = _new_chat(test_client)
    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/999999/regenerate?confirm=true"
    )
    assert resp.status_code == 404


def test_regenerate_403_for_other_user(test_client: TestClient, db_engine: AsyncEngine) -> None:
    _login(test_client, "alice", "correct-horse-battery-1", engine=db_engine)
    chat_id = _new_chat(test_client)
    _post_message(test_client, chat_id, "user", "q")
    a_id = _post_message(test_client, chat_id, "assistant", "a")

    test_client.post("/api/auth/logout")
    _login(test_client, "bob", "correct-horse-battery-2", engine=db_engine)
    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{a_id}/regenerate?confirm=true"
    )
    # ChatService.get raises ChatNotFoundError without distinguishing
    # ownership; the route's existence probe then issues 403 because the
    # chat row exists but belongs to alice.
    assert resp.status_code == 403
