# SPDX-License-Identifier: Apache-2.0
"""Route tests for Resend — regenerate endpoint extended to user-role messages.

Covers:
  - resend (user-role) without confirm → 412 with subsequent_count
  - resend (user-role) with confirm → deletes U AND everything after it
  - resend when user message is already last → auto-confirms, deleted=1
  - ownership parity: 403 for cross-user
  - 404 for missing message
  - assistant path still returns 422 when passed a user msg the old way
    (regression guard — the old 400 path is gone; role probe now handles it
    gracefully, so we just verify the assistant path still works correctly)
"""
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
        "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/resend.db"
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

    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/resend.db", pool_pre_ping=True
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


def _message_ids(client: TestClient, chat_id: int) -> list[int]:
    resp = client.get(f"/api/chats/{chat_id}")
    assert resp.status_code == 200
    return [m["id"] for m in resp.json()["messages"]]


# ---------------------------------------------------------------------------
# Resend (user-role) — confirm gate
# ---------------------------------------------------------------------------


def test_resend_user_without_confirm_412(test_client: TestClient) -> None:
    """Without ?confirm=true the endpoint returns 412 with subsequent_count."""
    _login(test_client)
    chat_id = _new_chat(test_client)
    u_id = _post_message(test_client, chat_id, "user", "hello")
    _post_message(test_client, chat_id, "assistant", "world")
    _post_message(test_client, chat_id, "user", "follow-up")

    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{u_id}/regenerate"
    )
    assert resp.status_code == 412, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "confirm_required"
    # Two messages after u_id (the assistant reply + the follow-up user msg)
    assert detail["subsequent_count"] == 2
    assert detail["chat_id"] == chat_id
    assert detail["message_id"] == u_id


# ---------------------------------------------------------------------------
# Resend (user-role) — confirm deletes U and everything after it
# ---------------------------------------------------------------------------


def test_resend_user_with_confirm_deletes_u_and_after(test_client: TestClient) -> None:
    """confirm=true deletes U plus everything after it, and returns U's content."""
    _login(test_client)
    chat_id = _new_chat(test_client)
    u_id = _post_message(test_client, chat_id, "user", "my prompt")
    a_id = _post_message(test_client, chat_id, "assistant", "assistant reply")
    u2_id = _post_message(test_client, chat_id, "user", "follow-up")

    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{u_id}/regenerate?confirm=true"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 3
    assert body["chat_id"] == chat_id
    # prior_user_content is the deleted user message's own content, captured
    # before the delete — the caller resubmits it as a fresh turn.
    assert body["prior_user_content"] == "my prompt"

    # U, the assistant reply, and the follow-up are ALL gone — the caller
    # replays U's content as a new message instead of it surviving in place
    # (this is what stops the resend duplication: the old row is gone by the
    # time the fresh turn is posted).
    ids = _message_ids(test_client, chat_id)
    assert u_id not in ids
    assert a_id not in ids
    assert u2_id not in ids


# ---------------------------------------------------------------------------
# Resend (user-role) — already the last message → auto-confirm, U replayed
# ---------------------------------------------------------------------------


def test_resend_user_already_last_zero_delete(test_client: TestClient) -> None:
    """When U is the last message, auto-confirm: no 412, U deleted+replayed."""
    _login(test_client)
    chat_id = _new_chat(test_client)
    _post_message(test_client, chat_id, "assistant", "preamble")
    u_id = _post_message(test_client, chat_id, "user", "the last prompt")

    # No ?confirm=true needed — backend should auto-confirm because there is
    # nothing AFTER U (count_messages_after == 0). The delete is still
    # inclusive of U itself, so deleted == 1, not 0.
    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{u_id}/regenerate"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 1
    assert body["prior_user_content"] == "the last prompt"

    # U is gone — the caller replays its content as a fresh message instead.
    ids = _message_ids(test_client, chat_id)
    assert u_id not in ids


# ---------------------------------------------------------------------------
# 404 — missing message
# ---------------------------------------------------------------------------


def test_resend_user_404_for_missing_message(test_client: TestClient) -> None:
    _login(test_client)
    chat_id = _new_chat(test_client)
    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/999999/regenerate?confirm=true"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 403 — cross-user ownership
# ---------------------------------------------------------------------------


def test_resend_user_403_for_other_user(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    _login(test_client, "alice", "correct-horse-battery-1", engine=db_engine)
    chat_id = _new_chat(test_client)
    u_id = _post_message(test_client, chat_id, "user", "alice's message")
    _post_message(test_client, chat_id, "assistant", "reply")

    test_client.post("/api/auth/logout")
    _login(test_client, "bob", "correct-horse-battery-2", engine=db_engine)
    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{u_id}/regenerate?confirm=true"
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Regression guard — assistant path still works after the refactor
# ---------------------------------------------------------------------------


def test_regenerate_assistant_still_works(test_client: TestClient) -> None:
    """The original assistant-regenerate path is unbroken by the resend extension."""
    _login(test_client)
    chat_id = _new_chat(test_client)
    u_id = _post_message(test_client, chat_id, "user", "q")
    a_id = _post_message(test_client, chat_id, "assistant", "a")
    _post_message(test_client, chat_id, "user", "follow-up")

    resp = test_client.post(
        f"/api/chats/{chat_id}/messages/{a_id}/regenerate?confirm=true"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Assistant path deletes the assistant msg + the prior user msg + anything after.
    assert body["deleted"] >= 2
    assert body["prior_user_content"] == "q"

    ids = _message_ids(test_client, chat_id)
    # Both the user prompt and assistant reply are deleted; the follow-up too.
    assert u_id not in ids
    assert a_id not in ids
