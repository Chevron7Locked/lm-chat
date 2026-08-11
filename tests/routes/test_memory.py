# SPDX-License-Identifier: Apache-2.0
"""Integration tests for memory routes.

Tests for the memory routes.

Pattern: FastAPI TestClient with real SQLite DB (tmp_path) + mock
EmbeddingClient so tests don't require a live LM Studio.  Service
dependencies are injected via ``app.dependency_overrides`` using the
module-local dependency helpers defined in ``routes/memory.py``.

Mock strategy
-------------
``MemoryService`` is NOT mocked — the real service runs against a real
(in-memory SQLite) DB.  The only mock is at the embedding boundary:
``EmbeddingClient`` is replaced with an ``AsyncMock`` that returns fixed
768-dimension zero vectors.  ``ModelsService.list_loaded`` is mocked to
return a single embedding model so ``MemoryService._default_embedding_model``
does not call LM Studio.

Fixtures
--------
``db_engine``           — per-test SQLite engine with full schema.
``mock_embedding``      — EmbeddingClient with mocked embed_one / embed_batch.
``mock_models_service`` — ModelsService with mocked list_loaded (embedding model).
``memory_service``      — real MemoryService backed by db_engine + mocks.
``status_holder``       — fresh ReindexStatusHolder instance.
``test_client``         — TestClient with all overrides wired.

Admin promotion happens via ``_make_admin(engine, username)`` — same
pattern as test_models.py.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import metadata, users
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_default_session_store_dep, get_engine_dep
from lmchat.routes.memory import (
    ReindexStatusHolder,
    _get_memory_service,
    _get_reindex_status_holder,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMBEDDING_DIM: int = 768
_FAKE_VECTOR: list[float] = [0.0] * _EMBEDDING_DIM


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
    """Per-test SQLite engine with full schema."""
    db_path = tmp_path / "test_routes_memory.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
def mock_embedding_client() -> MagicMock:
    """EmbeddingClient with embed_one / embed_batch returning fixed vectors."""
    client = MagicMock()
    client.embed_one = AsyncMock(return_value=_FAKE_VECTOR)
    client.embed_batch = AsyncMock(return_value=[_FAKE_VECTOR])
    return client


@pytest.fixture()
def mock_models_service() -> MagicMock:
    """ModelsService whose list_loaded returns one embedding model."""
    svc = MagicMock(spec=ModelsService)
    svc.list_loaded = AsyncMock(
        return_value=[
            ModelInfo(
                key="test-embed-model",
                type="embedding",
                capabilities=Capabilities(vision=False, trained_for_tool_use=False),
            )
        ]
    )
    svc.refresh = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def memory_svc(
    db_engine: AsyncEngine,
    mock_embedding_client: MagicMock,
    mock_models_service: MagicMock,
) -> MemoryService:
    """Real MemoryService backed by the test DB + mock embedding boundary."""
    return MemoryService(
        engine=db_engine,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )


@pytest.fixture()
def status_holder() -> ReindexStatusHolder:
    """Fresh ReindexStatusHolder."""
    return ReindexStatusHolder()


@pytest.fixture()
def test_client(
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
    status_holder: ReindexStatusHolder,
) -> Generator[TestClient]:
    """TestClient wired to per-test engine + real MemoryService + status holder."""
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)

    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_memory_service] = lambda: memory_svc
    app.dependency_overrides[_get_reindex_status_holder] = lambda: status_holder

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
    *,
    engine: AsyncEngine | None = None,
) -> None:
    """Register and log in a user (sets session cookie on client).

    Pass engine to bypass the single-admin gate via direct DB insert.
    """
    if engine is not None:
        _insert_user_sync(engine, username, password)
    else:
        client.post("/api/auth/register", data={"username": username, "password": password})
    client.post("/api/auth/login", data={"username": username, "password": password})


def _seed_placeholder(client: TestClient, *, engine: AsyncEngine | None = None) -> None:
    """Insert a throwaway user so the next user does not get the bootstrap-admin grant."""
    if engine is not None:
        _insert_user_sync(engine, "__placeholder__", "placeholder-pw")
    else:
        client.post(
            "/api/auth/register",
            data={"username": "__placeholder__", "password": "placeholder-pw"},
        )


async def _make_admin(engine: AsyncEngine, username: str) -> None:
    """Promote a user to admin directly in the DB."""
    async with engine.begin() as conn:
        await conn.execute(
            update(users).where(users.c.username == username).values(is_admin=True)
        )


async def _fill_pin_cap(
    engine: AsyncEngine,
    memory_service: MemoryService,
    user_id: int,
    cap: int,
) -> None:
    """Pin *cap* distinct insights for *user_id* to exhaust the cap."""
    for i in range(cap):
        await memory_service.pin_insight(user_id=user_id, text=f"insight {i}")


async def _get_user_id(engine: AsyncEngine, username: str) -> int:
    """Look up a user's integer ID from the DB."""
    async with engine.connect() as conn:
        result = await conn.execute(select(users.c.id).where(users.c.username == username))
        row = result.fetchone()
    assert row is not None, f"user {username!r} not found"
    return int(row.id)


def _insert_user_sync(
    engine: AsyncEngine,
    username: str,
    password: str = "correct-horse-battery",
) -> None:
    """Bypass the single-admin registration gate with a direct synchronous DB insert."""
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)

    async def _do() -> None:
        async with engine.begin() as conn:
            id_result = await conn.execute(
                select(func.coalesce(func.max(users.c.id), 0) + 1)
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


async def _insert_user_async(
    engine: AsyncEngine,
    username: str,
    password: str = "correct-horse-battery",
) -> None:
    """Bypass the single-admin registration gate with a direct async DB insert."""
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    async with engine.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": int(next_id or 1), "u": username, "ph": pw_hash},
        )


# ---------------------------------------------------------------------------
# POST /api/memory/pin — auth guard
# ---------------------------------------------------------------------------


def test_pin_requires_auth(test_client: TestClient) -> None:
    """POST /api/memory/pin without session → 401."""
    resp = test_client.post("/api/memory/pin", data={"text": "hello"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/memory/pin — happy path
# ---------------------------------------------------------------------------


def test_pin_creates_insight_returns_201(test_client: TestClient) -> None:
    """POST /api/memory/pin with valid text → 201 + MemoryInsight body."""
    _register_and_login(test_client)
    resp = test_client.post("/api/memory/pin", data={"text": "remember the mission"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["text"] == "remember the mission"
    assert body["pinned"] is True
    assert isinstance(body["id"], int)
    assert isinstance(body["user_id"], int)
    assert body["created_at"] is not None


# ---------------------------------------------------------------------------
# POST /api/memory/pin — text policy
# ---------------------------------------------------------------------------


def test_pin_rejects_whitespace_only(test_client: TestClient) -> None:
    """Whitespace-only text → 422 (UI gates against, but API did not)."""
    _register_and_login(test_client)
    resp = test_client.post("/api/memory/pin", data={"text": "     "})
    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"].lower() or "whitespace" in resp.json()["detail"].lower()


def test_pin_rejects_oversize(test_client: TestClient) -> None:
    """Text over 8192 chars → 422 (memory-pressure defense)."""
    _register_and_login(test_client)
    resp = test_client.post(
        "/api/memory/pin",
        data={"text": "A" * 10000},
    )
    assert resp.status_code == 422
    assert "at most" in resp.json()["detail"]


def test_pin_rejects_null_bytes(test_client: TestClient) -> None:
    """NUL bytes in text → 422 (downstream C truncation hazard)."""
    _register_and_login(test_client)
    resp = test_client.post(
        "/api/memory/pin",
        data={"text": "hello\x00world"},
    )
    assert resp.status_code == 422
    assert "null" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /api/memory/pin — dedup (idempotent on same text)
# ---------------------------------------------------------------------------


def test_pin_idempotent_on_same_text(test_client: TestClient) -> None:
    """Pinning the same text twice returns the same insight row (dedup)."""
    _register_and_login(test_client)
    resp1 = test_client.post("/api/memory/pin", data={"text": "unique thought"})
    resp2 = test_client.post("/api/memory/pin", data={"text": "unique thought"})
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    # Both must reference the same row by ID.
    assert resp1.json()["id"] == resp2.json()["id"]


# ---------------------------------------------------------------------------
# POST /api/memory/pin — cap exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_pin_returns_400_when_cap_exceeded(
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
    status_holder: ReindexStatusHolder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/memory/pin returns 400 when the user is at cap."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    # Set cap to 2 so we don't need to insert 100 rows.
    monkeypatch.setenv("LM_CHAT_PINNED_INSIGHTS_CAP", "2")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_memory_service] = lambda: memory_svc
    app.dependency_overrides[_get_reindex_status_holder] = lambda: status_holder

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "capuser", "password": "correct-horse-battery"})
        client.post("/api/auth/login", data={"username": "capuser", "password": "correct-horse-battery"})

        # Pin up to cap.
        client.post("/api/memory/pin", data={"text": "first"})
        client.post("/api/memory/pin", data={"text": "second"})

        # Third should fail.
        resp = client.post("/api/memory/pin", data={"text": "third"})
        assert resp.status_code == 400
        assert "cap exceeded" in resp.json()["detail"]

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# DELETE /api/memory/pin/{insight_id}
# ---------------------------------------------------------------------------


def test_unpin_deletes_insight_returns_204(test_client: TestClient) -> None:
    """DELETE /api/memory/pin/{id} → 204, then GET /pins no longer contains it."""
    _register_and_login(test_client)
    pin_resp = test_client.post("/api/memory/pin", data={"text": "to be deleted"})
    assert pin_resp.status_code == 201
    insight_id = pin_resp.json()["id"]

    del_resp = test_client.delete(f"/api/memory/pin/{insight_id}")
    assert del_resp.status_code == 204

    list_resp = test_client.get("/api/memory/pins")
    ids = [item["id"] for item in list_resp.json()]
    assert insight_id not in ids


def test_unpin_returns_404_for_unknown_id(test_client: TestClient) -> None:
    """DELETE /api/memory/pin/99999 → 404 (no such insight for this user)."""
    _register_and_login(test_client)
    resp = test_client.delete("/api/memory/pin/99999")
    assert resp.status_code == 404


def test_unpin_returns_404_for_other_users_insight(
    test_client: TestClient,
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
) -> None:
    """DELETE /api/memory/pin/{id} → 404 when the insight belongs to another user.

    Verifies the ownership check prevents cross-user deletion.
    """
    # Insert both users directly (bypass the single-admin registration gate).
    _insert_user_sync(db_engine, "alice2")
    _insert_user_sync(db_engine, "bob")

    # Log in as alice2, then log out.
    test_client.post("/api/auth/login", data={"username": "alice2", "password": "correct-horse-battery"})
    test_client.post("/api/auth/logout")

    # Log in as bob, pin something, get the ID.
    test_client.post("/api/auth/login", data={"username": "bob", "password": "correct-horse-battery"})
    bob_pin = test_client.post("/api/memory/pin", data={"text": "bob's secret"})
    bob_insight_id = bob_pin.json()["id"]
    test_client.post("/api/auth/logout")

    # Log back in as alice2 and try to delete bob's insight.
    test_client.post("/api/auth/login", data={"username": "alice2", "password": "correct-horse-battery"})
    resp = test_client.delete(f"/api/memory/pin/{bob_insight_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/memory/pins — auth guard
# ---------------------------------------------------------------------------


def test_list_pins_requires_auth(test_client: TestClient) -> None:
    """GET /api/memory/pins without session → 401."""
    resp = test_client.get("/api/memory/pins")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/memory/pins — user isolation
# ---------------------------------------------------------------------------


def test_list_pins_returns_user_pins_only(test_client: TestClient, db_engine: AsyncEngine) -> None:
    """GET /api/memory/pins returns ONLY the authenticated user's pins.

    Registers two users; each pins a distinct insight.  Verifies that
    each user sees only their own pin and NOT the other user's.
    """
    # Insert alice3 + bob2 directly (bypass single-admin gate).
    _insert_user_sync(db_engine, "alice3", "correct-horse-battery")
    _insert_user_sync(db_engine, "bob2", "correct-horse-battery")

    # Alice pins something.
    test_client.post("/api/auth/login", data={"username": "alice3", "password": "correct-horse-battery"})
    test_client.post("/api/memory/pin", data={"text": "alice3 only"})
    test_client.post("/api/auth/logout")

    # Bob pins something.
    test_client.post("/api/auth/login", data={"username": "bob2", "password": "correct-horse-battery"})
    test_client.post("/api/memory/pin", data={"text": "bob2 only"})

    bob_pins = test_client.get("/api/memory/pins").json()
    bob_texts = [p["text"] for p in bob_pins]
    assert "bob2 only" in bob_texts
    assert "alice3 only" not in bob_texts

    test_client.post("/api/auth/logout")

    # Alice sees only hers.
    test_client.post("/api/auth/login", data={"username": "alice3", "password": "correct-horse-battery"})
    alice_pins = test_client.get("/api/memory/pins").json()
    alice_texts = [p["text"] for p in alice_pins]
    assert "alice3 only" in alice_texts
    assert "bob2 only" not in alice_texts


# ---------------------------------------------------------------------------
# POST /api/memory/reindex — auth guard (non-admin → 403)
# ---------------------------------------------------------------------------


def test_reindex_requires_admin(test_client: TestClient, db_engine: AsyncEngine) -> None:
    """POST /api/memory/reindex as non-admin → 403."""
    _seed_placeholder(test_client, engine=db_engine)
    _register_and_login(test_client, engine=db_engine)
    resp = test_client.post(
        "/api/memory/reindex",
        data={"embedding_model_id": "test-embed-model"},
    )
    assert resp.status_code == 403


def test_reindex_requires_auth(test_client: TestClient) -> None:
    """POST /api/memory/reindex without session → 401."""
    resp = test_client.post(
        "/api/memory/reindex",
        data={"embedding_model_id": "test-embed-model"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/memory/reindex — admin happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_reindex_admin_returns_202_and_schedules_task(
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
    status_holder: ReindexStatusHolder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/memory/reindex as admin → 202 + ReindexStartResponse body."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_memory_service] = lambda: memory_svc
    app.dependency_overrides[_get_reindex_status_holder] = lambda: status_holder

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post(
            "/api/memory/reindex",
            data={"embedding_model_id": "test-embed-model"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["ok"] is True
        assert body["embedding_model_id"] == "test-embed-model"
        assert body["task_status"] == "started"

        # The task should now be on app.state.reindex_task.
        task = app.state.reindex_task
        assert task is not None
        assert isinstance(task, asyncio.Task)

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# POST /api/memory/reindex — 409 when already running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_reindex_conflict_when_already_running(
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
    status_holder: ReindexStatusHolder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second POST /api/memory/reindex while first still running → 409."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_memory_service] = lambda: memory_svc
    app.dependency_overrides[_get_reindex_status_holder] = lambda: status_holder

    # Patch memory_service.reindex to never complete during this test.
    # We replace it with an async function that just hangs (waits for
    # cancellation).
    async def _never_complete(**kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(9999)
        return {}

    memory_svc.reindex = _never_complete  # type: ignore[method-assign]

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin2", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin2")
        client.post("/api/auth/login", data={"username": "admin2", "password": "correct-horse-battery"})

        resp1 = client.post(
            "/api/memory/reindex",
            data={"embedding_model_id": "test-embed-model"},
        )
        assert resp1.status_code == 202

        # Second attempt while task is still scheduled (TestClient is sync;
        # the asyncio.Task from create_task is "running" until the test
        # event loop advances — but at the time of the second HTTP call the
        # task object is not done()).
        resp2 = client.post(
            "/api/memory/reindex",
            data={"embedding_model_id": "test-embed-model"},
        )
        assert resp2.status_code == 409
        assert "already running" in resp2.json()["detail"]

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# GET /api/memory/reindex/status — auth guard
# ---------------------------------------------------------------------------


def test_reindex_status_requires_admin(test_client: TestClient, db_engine: AsyncEngine) -> None:
    """GET /api/memory/reindex/status as non-admin → 403."""
    _seed_placeholder(test_client, engine=db_engine)
    _register_and_login(test_client, engine=db_engine)
    resp = test_client.get("/api/memory/reindex/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/memory/reindex/status — idle state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_reindex_status_idle_when_no_reindex_yet(
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
    status_holder: ReindexStatusHolder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/memory/reindex/status before any reindex → state=idle."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_memory_service] = lambda: memory_svc
    app.dependency_overrides[_get_reindex_status_holder] = lambda: status_holder

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin3", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin3")
        client.post("/api/auth/login", data={"username": "admin3", "password": "correct-horse-battery"})

        resp = client.get("/api/memory/reindex/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "idle"

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# GET /api/memory/reindex/status — after completion snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_reindex_status_after_completion_returns_snapshot(
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
    status_holder: ReindexStatusHolder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a completed reindex, status endpoint returns completed snapshot."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_memory_service] = lambda: memory_svc
    app.dependency_overrides[_get_reindex_status_holder] = lambda: status_holder

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin4", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin4")
        client.post("/api/auth/login", data={"username": "admin4", "password": "correct-horse-battery"})

        # Kick reindex.
        client.post(
            "/api/memory/reindex",
            data={"embedding_model_id": "test-embed-model"},
        )

        # Wait for the task to complete — the task runs on the asyncio loop
        # managed by TestClient.  We poll the holder by yielding control to
        # the event loop on each iteration.
        for _ in range(50):
            if status_holder.snapshot.state in ("completed", "failed"):
                break
            await asyncio.sleep(0.05)

        # Status endpoint should now reflect completion.
        resp = client.get("/api/memory/reindex/status")
        assert resp.status_code == 200
        body = resp.json()
        # The task ran on an empty DB so processed == 0, but state != idle.
        assert body["state"] in ("completed", "failed")
        assert body["embedding_model_id"] == "test-embed-model"

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# ReindexStatusHolder unit tests
# ---------------------------------------------------------------------------


def test_status_holder_initial_state_is_idle() -> None:
    """ReindexStatusHolder starts in idle state."""
    holder = ReindexStatusHolder()
    assert holder.snapshot.state == "idle"
    assert holder.is_running is False


def test_status_holder_start_sets_running() -> None:
    """start() transitions holder to running state."""
    holder = ReindexStatusHolder()
    holder.start(embedding_model_id="my-model", total=100)
    assert holder.snapshot.state == "running"
    assert holder.snapshot.embedding_model_id == "my-model"
    assert holder.snapshot.total == 100
    assert holder.is_running is True
    assert holder.snapshot.started_at is not None
    assert holder.snapshot.completed_at is None


def test_status_holder_complete_sets_completed() -> None:
    """complete() transitions holder from running to completed."""
    holder = ReindexStatusHolder()
    holder.start(embedding_model_id="my-model", total=10)
    holder.complete(
        snapshot_dict={
            "embedding_model_id": "my-model",
            "processed": 10,
            "total": 10,
            "elapsed_s": 1.5,
            "failed": 0,
        }
    )
    snap = holder.snapshot
    assert snap.state == "completed"
    assert snap.processed == 10
    assert snap.total == 10
    assert snap.elapsed_s == 1.5
    assert snap.failed == 0
    assert snap.completed_at is not None
    assert holder.is_running is False


def test_status_holder_fail_sets_failed() -> None:
    """fail() transitions holder from running to failed."""
    holder = ReindexStatusHolder()
    holder.start(embedding_model_id="my-model", total=10)
    holder.fail(error="upstream exploded")
    snap = holder.snapshot
    assert snap.state == "failed"
    assert snap.completed_at is not None
    assert holder.is_running is False


# ---------------------------------------------------------------------------
# _run general-error path coverage
# ---------------------------------------------------------------------------


def test_reindex_run_closure_records_failure_on_exception(
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
    status_holder: ReindexStatusHolder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When memory_service.reindex raises a non-CancelledError exception,
    the _run() closure catches it and calls status_holder.fail() with the
    error string. This closes a coverage gap — the general-error
    path was previously uncovered.

    Sync-style test: TestClient owns its own event loop, so we poll the
    status endpoint until the background task transitions to "failed"
    rather than awaiting the task from outside its loop. Each request
    drives the loop forward so the _run() closure runs.
    """
    import time

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_memory_service] = lambda: memory_svc
    app.dependency_overrides[_get_reindex_status_holder] = lambda: status_holder

    async def _exploding_reindex(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated upstream failure")

    memory_svc.reindex = _exploding_reindex  # type: ignore[method-assign]

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post(
            "/api/auth/register",
            data={"username": "admin_err", "password": "correct-horse-battery"},
        )
        # _make_admin is async; run it in a one-off loop scoped to this
        # test (TestClient's loop is per-request so this is safe).
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_make_admin(db_engine, "admin_err"))
        finally:
            loop.close()
        client.post(
            "/api/auth/login",
            data={"username": "admin_err", "password": "correct-horse-battery"},
        )

        resp = client.post(
            "/api/memory/reindex",
            data={"embedding_model_id": "test-embed-model"},
        )
        assert resp.status_code == 202

        # Poll the status endpoint — each request drives the event loop
        # forward so the background _run() closure runs the except branch
        # that calls status_holder.fail().
        deadline = time.monotonic() + 5.0
        state = "running"
        while time.monotonic() < deadline:
            status_resp = client.get("/api/memory/reindex/status")
            assert status_resp.status_code == 200
            state = status_resp.json().get("state")
            if state == "failed":
                break
            time.sleep(0.05)

        assert state == "failed", f"expected state=failed but got {state!r}"
        # The snapshot model intentionally doesn't expose the error string
        # (fail() logs it for admin inspection but doesn't store it on
        # the snapshot — kept out of the public API surface). The
        # state=="failed" transition is sufficient to verify the closure's
        # except-branch ran.
        snap = status_holder.snapshot
        assert snap.state == "failed"
        assert snap.completed_at is not None

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# GET /api/memory/pins?project_id=X
# ---------------------------------------------------------------------------


async def _create_project_via_api(
    client: TestClient, name: str = "Proj"
) -> int:
    resp = client.post("/api/projects", data={"name": name})
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _retag_pin_with_project(
    engine: AsyncEngine, *, pin_id: int, project_id: int | None
) -> None:
    """Direct DB write — the pin POST route doesn't accept project_id
    (its scoping happens at insight-write time, not pin-update time)."""
    from sqlalchemy import update as _update

    from lmchat.db.schema import memory_insights as _mi

    async with engine.begin() as conn:
        await conn.execute(
            _update(_mi)
            .where(_mi.c.id == pin_id)
            .values(project_id=project_id)
        )


@pytest.mark.anyio
async def test_list_pins_default_returns_all_user_pins(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """GET /api/memory/pins (no project_id) → every pin the user owns."""
    _register_and_login(test_client)
    pid = await _create_project_via_api(test_client)
    test_client.post(
        "/api/memory/pin", data={"text": "un-pin"}
    ).json()
    p_in = test_client.post(
        "/api/memory/pin", data={"text": "in-pin"}
    ).json()
    await _retag_pin_with_project(
        db_engine, pin_id=int(p_in["id"]), project_id=pid
    )
    resp = test_client.get("/api/memory/pins")
    assert resp.status_code == 200
    texts = {p["text"] for p in resp.json()}
    assert {"un-pin", "in-pin"} <= texts


@pytest.mark.anyio
async def test_list_pins_with_project_id_filters(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """GET /api/memory/pins?project_id=X → only pins tagged with X."""
    _register_and_login(test_client)
    pid = await _create_project_via_api(test_client)
    p_un = test_client.post(
        "/api/memory/pin", data={"text": "un-pin"}
    ).json()
    p_in = test_client.post(
        "/api/memory/pin", data={"text": "in-pin"}
    ).json()
    await _retag_pin_with_project(
        db_engine, pin_id=int(p_in["id"]), project_id=pid
    )
    resp = test_client.get(
        "/api/memory/pins", params={"project_id": pid}
    )
    assert resp.status_code == 200
    texts = {p["text"] for p in resp.json()}
    assert texts == {"in-pin"}
    _ = p_un  # un-pin must NOT appear.


@pytest.mark.anyio
async def test_list_pins_404_on_foreign_project_id(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Foreign / unknown project_id must 404.

    Without this, the route silently 200-emptied for both "missing
    project" and "wrong owner" — confusing admin UX even though
    no data actually leaked.
    """
    _register_and_login(test_client)
    resp = test_client.get(
        "/api/memory/pins", params={"project_id": 99999}
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_list_pins_404_on_cross_user_project_id(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Bob's request with Alice's project_id returns 404 (never leaks
    existence, never silently 200-empty).
    """
    await _insert_user_async(db_engine, "alice_pins404")
    test_client.post("/api/auth/login", data={"username": "alice_pins404", "password": "correct-horse-battery"})
    pid = await _create_project_via_api(test_client)
    # Switch to Bob.
    test_client.cookies.clear()
    await _insert_user_async(db_engine, "bob", "correct-horse-battery")
    test_client.post(
        "/api/auth/login",
        data={"username": "bob", "password": "correct-horse-battery"},
    )
    resp = test_client.get(
        "/api/memory/pins", params={"project_id": pid}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/memory/auto — auto (distilled) memories
# ---------------------------------------------------------------------------


def test_list_auto_requires_auth(test_client: TestClient) -> None:
    """GET /api/memory/auto without a session → 401."""
    resp = test_client.get("/api/memory/auto")
    assert resp.status_code == 401


@pytest.mark.asyncio()
async def test_list_auto_returns_distilled_memories(
    test_client: TestClient,
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
) -> None:
    """GET /api/memory/auto returns AUTO insights (pinned=False), not manual pins."""
    _register_and_login(test_client)
    uid = await _get_user_id(db_engine, "alice")

    # Seed one manual pin and one auto memory.
    await memory_svc.pin_insight(user_id=uid, text="Pinned by hand")
    await memory_svc.save_auto_insight(user_id=uid, text="Name is Kevin")

    # /pins shows only the manual pin.
    pins = test_client.get("/api/memory/pins").json()
    pin_texts = {p["text"] for p in pins}
    assert "Pinned by hand" in pin_texts
    assert "Name is Kevin" not in pin_texts

    # /auto shows only the auto memory, flagged pinned=False.
    resp = test_client.get("/api/memory/auto")
    assert resp.status_code == 200
    auto = resp.json()
    auto_texts = {a["text"] for a in auto}
    assert "Name is Kevin" in auto_texts
    assert "Pinned by hand" not in auto_texts
    assert all(a["pinned"] is False for a in auto)


@pytest.mark.asyncio()
async def test_delete_auto_memory_succeeds(
    test_client: TestClient,
    db_engine: AsyncEngine,
    memory_svc: MemoryService,
) -> None:
    """DELETE /api/memory/pin/{id} removes an AUTO memory (not just pins)."""
    _register_and_login(test_client)
    uid = await _get_user_id(db_engine, "alice")

    auto = await memory_svc.save_auto_insight(user_id=uid, text="Into astrophysics")
    assert auto is not None

    # The earlier list_pinned-only ownership check would have 404'd this.
    del_resp = test_client.delete(f"/api/memory/pin/{auto.id}")
    assert del_resp.status_code == 204

    remaining = {a["text"] for a in test_client.get("/api/memory/auto").json()}
    assert "Into astrophysics" not in remaining
