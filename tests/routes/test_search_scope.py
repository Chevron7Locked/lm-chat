# SPDX-License-Identifier: Apache-2.0
"""Integration tests for GET /api/search?scope= extension.

Tests for search scope isolation.

Uses the global-engine isolation pattern (same as test_streaming.py) so
AuthMiddleware and route handlers share one DB.

Tests
-----
- test_search_scope_messages        — scope=messages returns default behavior
- test_search_scope_messages_default — omitting scope= defaults to messages
- test_search_scope_chats           — scope=chats returns matching chat titles
- test_search_scope_chats_only_owned — scope=chats results are user-scoped
- test_search_scope_memory          — scope=memory returns matching pinned insights
- test_search_scope_all_three_keys  — scope=all returns dict with three keys
- test_search_scope_cross_user_messages — user B's messages not in user A results
- test_search_unauthenticated_returns_401 — no session → 401
"""
from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.services.auth_service import _reset_dummy_hash_cache

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """Build the app with per-test DB isolation."""
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/search_scope_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    return create_app()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear settings cache around each test."""
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()


def _setup_fts5(eng: Any) -> None:
    """Add FTS5 virtual table + triggers to the given engine (sync helper)."""
    from sqlalchemy import text as sa_text

    async def _run() -> None:
        async with eng.connect() as conn:
            try:
                await conn.execute(sa_text("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        content, content='messages', content_rowid='id',
                        tokenize='porter unicode61'
                    )
                """))
                for ddl in [
                    """CREATE TRIGGER IF NOT EXISTS messages_ai
                       AFTER INSERT ON messages BEGIN
                           INSERT INTO messages_fts(rowid, content)
                           VALUES (new.id, new.content);
                       END""",
                    """CREATE TRIGGER IF NOT EXISTS messages_au
                       AFTER UPDATE OF content ON messages BEGIN
                           INSERT INTO messages_fts(messages_fts, rowid, content)
                           VALUES('delete', old.id, old.content);
                           INSERT INTO messages_fts(rowid, content)
                           VALUES (new.id, new.content);
                       END""",
                    """CREATE TRIGGER IF NOT EXISTS messages_ad
                       AFTER DELETE ON messages BEGIN
                           INSERT INTO messages_fts(messages_fts, rowid, content)
                           VALUES('delete', old.id, old.content);
                       END""",
                ]:
                    await conn.execute(sa_text(ddl))
                await conn.commit()
            except Exception:  # noqa: BLE001
                pass

    asyncio.run(_run())


@pytest.fixture()
def test_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    """TestClient with per-test DB + FTS5 schema."""
    app = _make_app(tmp_path, monkeypatch)

    with TestClient(app, raise_server_exceptions=True) as client:
        from lmchat.db.engine import get_engine

        _setup_fts5(get_engine())

        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LOW_N: int = 2**10


async def _insert_user_via_engine_async(username: str, password: str) -> None:
    """Bypass the single-admin gate via the global engine (async)."""
    from sqlalchemy import func, select
    from sqlalchemy import text as sa_text

    from lmchat.db.engine import get_engine
    from lmchat.db.schema import users as users_table
    from lmchat.utils.hashing import hash_password

    engine = get_engine()
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    async with engine.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users_table.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        await conn.execute(
            sa_text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": int(next_id or 1), "u": username, "ph": pw_hash},
        )


def _insert_user_sync(username: str, password: str) -> None:
    """Bypass the single-admin gate via the global engine (synchronous wrapper)."""
    asyncio.run(_insert_user_via_engine_async(username, password))


def _register_and_login(
    client: TestClient,
    username: str = "alice",
    password: str = "correct-horse-battery",
) -> None:
    """Insert the user directly (bypass single-admin gate) then log in."""
    _insert_user_sync(username, password)
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"


def _create_chat(client: TestClient, title: str) -> dict:  # type: ignore[type-arg]
    r = client.post("/api/chats", data={"title": title})
    assert r.status_code == 201, r.text
    return r.json()


def _append_message(client: TestClient, chat_id: int, content: str) -> None:
    r = client.post(
        f"/api/chats/{chat_id}/messages",
        data={"role": "user", "content": content},
    )
    assert r.status_code == 201, r.text


async def _pin_insight(user_id: int, insight_text: str) -> None:
    """Pin a memory insight directly via the MemoryService."""
    from unittest.mock import AsyncMock, MagicMock

    from lmchat.db.engine import get_engine
    from lmchat.embedding.client import EmbeddingClient
    from lmchat.services.memory_service import MemoryService
    from lmchat.services.models_service import ModelsService

    eng = get_engine()
    embed_client = MagicMock(spec=EmbeddingClient)
    embed_client.embed = AsyncMock(return_value=[0.1] * 384)
    stub_models = MagicMock(spec=ModelsService)
    svc = MemoryService(
        engine=eng, embedding_client=embed_client, models_service=stub_models
    )
    await svc.pin_insight(user_id=user_id, text=insight_text)


async def _get_user_id(username: str) -> int:
    """Look up a user_id by username from the global engine."""
    from sqlalchemy import select

    from lmchat.db.engine import get_engine
    from lmchat.db.schema import users as users_table

    eng = get_engine()
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(users_table.c.id).where(users_table.c.username == username)
            )
        ).fetchone()
    assert row is not None, f"user {username!r} not found"
    return int(row[0])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_search_scope_messages(test_client: TestClient) -> None:
    """scope=messages (default) returns matching messages as a flat list."""
    _register_and_login(test_client, "scope_msg_user", "correct-horse-battery")
    chat = _create_chat(test_client, "msg chat")
    _append_message(test_client, chat["id"], "unique_token_xyz in a message")

    resp = test_client.get("/api/search?q=unique_token_xyz&scope=messages")
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert isinstance(results, list)
    assert any("unique_token_xyz" in m["content"] for m in results)


def test_search_scope_messages_default(test_client: TestClient) -> None:
    """Omitting scope= defaults to messages behavior (backwards compat)."""
    _register_and_login(test_client, "scope_def_user", "correct-horse-battery")
    chat = _create_chat(test_client, "def chat")
    _append_message(test_client, chat["id"], "default_scope_probe")

    resp = test_client.get("/api/search?q=default_scope_probe")
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert isinstance(results, list)


def test_search_scope_chats_returns_chat_titles_matching(
    test_client: TestClient,
) -> None:
    """scope=chats returns chats whose titles match q."""
    _register_and_login(test_client, "scope_chats_user", "correct-horse-battery")
    _create_chat(test_client, "my special chat title")
    _create_chat(test_client, "unrelated chat")

    resp = test_client.get("/api/search?q=special&scope=chats")
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert isinstance(results, list)
    assert any("special" in r["title"].lower() for r in results), (
        f"Expected 'special' in titles, got: {[r['title'] for r in results]}"
    )


def test_search_scope_chats_only_owned(test_client: TestClient) -> None:
    """scope=chats results are user-scoped — user B's chats not returned to A."""
    _register_and_login(test_client, "scope_chats_a", "correct-horse-battery-a")
    _create_chat(test_client, "alice exclusive chat")

    # Login as user B (overwrites cookie).
    _register_and_login(test_client, "scope_chats_b", "correct-horse-battery-b")
    _create_chat(test_client, "bob exclusive chat")

    # Logged in as bob — should only see bob's chats.
    resp = test_client.get("/api/search?q=exclusive&scope=chats")
    assert resp.status_code == 200, resp.text
    results = resp.json()
    for r in results:
        assert "alice" not in r["title"].lower(), (
            f"Alice's chat appeared in Bob's results: {r['title']}"
        )


async def test_search_scope_memory_returns_pinned_insights_matching(
    test_client: TestClient,
) -> None:
    """scope=memory returns pinned insights matching q (in-Python filter)."""
    await _insert_user_via_engine_async("scope_mem_user", "correct-horse-battery")
    test_client.post(
        "/api/auth/login",
        data={"username": "scope_mem_user", "password": "correct-horse-battery"},
    )

    user_id = await _get_user_id("scope_mem_user")
    await _pin_insight(user_id, "memory insight about python programming")
    await _pin_insight(user_id, "another insight about rust")

    resp = test_client.get("/api/search?q=python&scope=memory")
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert isinstance(results, list)
    assert any("python" in r["text"].lower() for r in results), (
        f"Expected 'python' insight, got: {results}"
    )


async def test_search_scope_all_returns_dict_with_three_keys(
    test_client: TestClient,
) -> None:
    """scope=all returns a dict with messages, chats, and memory keys."""
    await _insert_user_via_engine_async("scope_all_user", "correct-horse-battery")
    test_client.post(
        "/api/auth/login",
        data={"username": "scope_all_user", "password": "correct-horse-battery"},
    )

    chat = _create_chat(test_client, "all-scope test chat")
    _append_message(test_client, chat["id"], "all-scope unique term")

    resp = test_client.get("/api/search?q=all-scope&scope=all")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert isinstance(data, dict), f"Expected dict, got {type(data)}: {data}"
    assert set(data.keys()) == {"messages", "chats", "memory"}, (
        f"Expected keys {{messages, chats, memory}}, got: {set(data.keys())}"
    )
    assert isinstance(data["messages"], list)
    assert isinstance(data["chats"], list)
    assert isinstance(data["memory"], list)


def test_search_scope_cross_user_messages_not_returned(
    test_client: TestClient,
) -> None:
    """Messages from user B are never returned to user A's search."""
    _register_and_login(test_client, "cross_user_a", "correct-horse-battery-a")
    chat_a = _create_chat(test_client, "user a chat")
    _append_message(test_client, chat_a["id"], "cross_user_secret_message")

    # Login as user B.
    _register_and_login(test_client, "cross_user_b", "correct-horse-battery-b")

    resp = test_client.get("/api/search?q=cross_user_secret_message&scope=messages")
    assert resp.status_code == 200, resp.text
    results = resp.json()
    for m in results:
        assert "cross_user_secret_message" not in m["content"], (
            "Cross-user message leaked in search results"
        )


def test_search_unauthenticated_returns_401(test_client: TestClient) -> None:
    """Unauthenticated GET /api/search returns 401."""
    test_client.cookies.clear()
    resp = test_client.get("/api/search?q=test")
    assert resp.status_code in (401, 403), (
        f"Expected 401/403, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# /api/search?project_id=X — scope=memory + all
# ---------------------------------------------------------------------------


async def _retag_pin_with_project(
    pin_text: str, *, user_id: int, project_id: int | None
) -> None:
    """Direct DB write — re-tag a pin's project_id by matching its text."""
    from sqlalchemy import update as _update

    from lmchat.db.engine import get_engine
    from lmchat.db.schema import memory_insights as _mi

    eng = get_engine()
    async with eng.begin() as conn:
        await conn.execute(
            _update(_mi)
            .where(_mi.c.user_id == user_id)
            .where(_mi.c.text == pin_text)
            .values(project_id=project_id)
        )


async def test_search_scope_memory_filters_by_project_id(
    test_client: TestClient,
) -> None:
    """`?scope=memory&project_id=X` returns only pins tagged with X."""
    await _insert_user_via_engine_async("scope_mem_proj_user", "correct-horse-battery")
    test_client.post(
        "/api/auth/login",
        data={"username": "scope_mem_proj_user", "password": "correct-horse-battery"},
    )
    user_id = await _get_user_id("scope_mem_proj_user")
    proj_resp = test_client.post(
        "/api/projects", data={"name": "Proj"}
    )
    assert proj_resp.status_code == 201
    pid = int(proj_resp.json()["id"])

    await _pin_insight(user_id, "python in this project")
    await _pin_insight(user_id, "python elsewhere")
    await _retag_pin_with_project(
        "python in this project", user_id=user_id, project_id=pid
    )

    resp = test_client.get(
        f"/api/search?q=python&scope=memory&project_id={pid}"
    )
    assert resp.status_code == 200
    results = resp.json()
    texts = {r["text"] for r in results}
    assert texts == {"python in this project"}


async def test_search_scope_all_with_project_id_filters_memory(
    test_client: TestClient,
) -> None:
    """`?scope=all&project_id=X` — the memory arm filters by project_id."""
    await _insert_user_via_engine_async("scope_all_proj_user", "correct-horse-battery")
    test_client.post(
        "/api/auth/login",
        data={"username": "scope_all_proj_user", "password": "correct-horse-battery"},
    )
    user_id = await _get_user_id("scope_all_proj_user")
    proj_resp = test_client.post(
        "/api/projects", data={"name": "Proj"}
    )
    pid = int(proj_resp.json()["id"])

    await _pin_insight(user_id, "rustprojectspecific")
    await _pin_insight(user_id, "rustglobal")
    await _retag_pin_with_project(
        "rustprojectspecific", user_id=user_id, project_id=pid
    )

    resp = test_client.get(
        f"/api/search?q=rust&scope=all&project_id={pid}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "memory" in body
    mem_texts = {r["text"] for r in body["memory"]}
    assert mem_texts == {"rustprojectspecific"}


async def test_search_404_on_foreign_project_id(
    test_client: TestClient,
) -> None:
    """Foreign project_id on /api/search → 404."""
    await _insert_user_via_engine_async("search_proj_404_user", "correct-horse-battery")
    test_client.post(
        "/api/auth/login",
        data={"username": "search_proj_404_user", "password": "correct-horse-battery"},
    )
    resp = test_client.get(
        "/api/search?q=anything&scope=memory&project_id=99999"
    )
    assert resp.status_code == 404


async def test_search_404_on_cross_user_project_id(
    test_client: TestClient,
) -> None:
    """User-B's search with User-A's project_id returns 404."""
    await _insert_user_via_engine_async("search_cross_alice", "correct-horse-battery")
    test_client.post(
        "/api/auth/login",
        data={"username": "search_cross_alice", "password": "correct-horse-battery"},
    )
    proj_resp = test_client.post(
        "/api/projects", data={"name": "alices"}
    )
    assert proj_resp.status_code == 201
    pid = int(proj_resp.json()["id"])
    test_client.cookies.clear()
    await _insert_user_via_engine_async("search_cross_bob", "correct-horse-battery")
    test_client.post(
        "/api/auth/login",
        data={
            "username": "search_cross_bob",
            "password": "correct-horse-battery",
        },
    )
    resp = test_client.get(
        f"/api/search?q=anything&scope=memory&project_id={pid}"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# scope=messages + scope=chats project_id wiring,
# plus the explicit scope=all fan-out regression test.
# ---------------------------------------------------------------------------


def test_a2_scope_messages_filters_by_project_id(
    test_client: TestClient,
) -> None:
    """scope=messages + project_id=X returns only messages whose parent
    chat carries that project_id. Cross-project messages are excluded.
    """
    _register_and_login(
        test_client, "a2_scope_msg_user", "correct-horse-battery"
    )
    # Two projects, one un-projected chat. Same query term in all.
    pa = test_client.post(
        "/api/projects", data={"name": "ProjA"}
    )
    pb = test_client.post(
        "/api/projects", data={"name": "ProjB"}
    )
    pa_id = int(pa.json()["id"])
    pb_id = int(pb.json()["id"])

    chat_pa = test_client.post(
        f"/api/projects/{pa_id}/chats", data={"title": "in PA"}
    ).json()
    chat_pb = test_client.post(
        f"/api/projects/{pb_id}/chats", data={"title": "in PB"}
    ).json()
    chat_unp = _create_chat(test_client, "no project")

    _append_message(test_client, chat_pa["id"], "a2probe in PA")
    _append_message(test_client, chat_pb["id"], "a2probe in PB")
    _append_message(test_client, chat_unp["id"], "a2probe un-projected")

    # project_id=PA — only PA chat's message.
    resp = test_client.get(
        f"/api/search?q=a2probe&scope=messages&project_id={pa_id}"
    )
    assert resp.status_code == 200, resp.text
    chat_ids = sorted(m["chat_id"] for m in resp.json())
    assert chat_ids == [chat_pa["id"]], (
        f"project_id=PA leaked messages from other scopes: {chat_ids}"
    )


def test_a2_scope_chats_filters_by_project_id(
    test_client: TestClient,
) -> None:
    """scope=chats + project_id=X returns only chats whose project_id
    matches. Un-projected chats are excluded.
    """
    _register_and_login(
        test_client, "a2_scope_chats_user", "correct-horse-battery"
    )
    pa = test_client.post(
        "/api/projects", data={"name": "ProjA"}
    )
    pa_id = int(pa.json()["id"])

    chat_pa = test_client.post(
        f"/api/projects/{pa_id}/chats", data={"title": "a2chatprobe in PA"}
    ).json()
    _create_chat(test_client, "a2chatprobe un-projected")

    resp = test_client.get(
        f"/api/search?q=a2chatprobe&scope=chats&project_id={pa_id}"
    )
    assert resp.status_code == 200, resp.text
    ids = sorted(c["id"] for c in resp.json())
    assert ids == [chat_pa["id"]], (
        f"project_id=PA leaked un-projected chats: {ids}"
    )


def test_a2_scope_all_fanout_filters_messages_and_chats(
    test_client: TestClient,
) -> None:
    """scope=all + project_id=X — the EXPLICIT fan-out regression:
    per-scope tests catch per-sub-path leakage,
    but a bug INSIDE the asyncio.gather() fan-out (e.g. forgetting to
    forward project_id on one branch) passes per-scope checks while
    leaking on scope=all. This test asserts both the messages AND
    chats arms of scope=all respect project_id.
    """
    _register_and_login(
        test_client, "a2_scope_all_user", "correct-horse-battery"
    )
    pa = test_client.post(
        "/api/projects", data={"name": "ProjA"}
    )
    pa_id = int(pa.json()["id"])

    chat_pa = test_client.post(
        f"/api/projects/{pa_id}/chats", data={"title": "a2allprobe in PA"}
    ).json()
    chat_unp = _create_chat(test_client, "a2allprobe un-projected")

    _append_message(test_client, chat_pa["id"], "a2allprobe msg PA")
    _append_message(test_client, chat_unp["id"], "a2allprobe msg unp")

    resp = test_client.get(
        f"/api/search?q=a2allprobe&scope=all&project_id={pa_id}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Messages arm of fan-out — only PA's message.
    msg_chat_ids = sorted(m["chat_id"] for m in body.get("messages", []))
    assert msg_chat_ids == [chat_pa["id"]], (
        f"scope=all messages arm leaked: {msg_chat_ids}"
    )

    # Chats arm of fan-out — only PA's chat.
    chat_ids = sorted(c["id"] for c in body.get("chats", []))
    assert chat_ids == [chat_pa["id"]], (
        f"scope=all chats arm leaked: {chat_ids}"
    )


def test_a2_scope_messages_no_project_id_returns_user_union(
    test_client: TestClient,
) -> None:
    """project_id=None preserves the legacy user-scoped union — every
    message the user owns regardless of project_id. NOT "rows where
    chats.project_id IS NULL".
    """
    _register_and_login(
        test_client, "a2_msg_union_user", "correct-horse-battery"
    )
    pa = test_client.post(
        "/api/projects", data={"name": "ProjA"}
    )
    pa_id = int(pa.json()["id"])

    chat_pa = test_client.post(
        f"/api/projects/{pa_id}/chats", data={"title": "x"}
    ).json()
    chat_unp = _create_chat(test_client, "y")

    _append_message(test_client, chat_pa["id"], "a2unionprobe in PA")
    _append_message(test_client, chat_unp["id"], "a2unionprobe un-projected")

    # No project_id query param.
    resp = test_client.get("/api/search?q=a2unionprobe&scope=messages")
    assert resp.status_code == 200, resp.text
    chat_ids = sorted(m["chat_id"] for m in resp.json())
    assert chat_ids == [chat_pa["id"], chat_unp["id"]], (
        f"project_id=None must return user-scoped union, got: {chat_ids}"
    )
