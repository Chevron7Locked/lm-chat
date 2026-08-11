# SPDX-License-Identifier: Apache-2.0
"""Tests for GET /api/lmstudio/health — live reachability probe.

Covers:
- Unauthenticated requests → 401
- Authenticated requests → 200 with correct shape
- reachable=False when the upstream probe raises httpx.ConnectError
- reachable=True with loaded_count when the probe succeeds
- TTL rate-limiting: only one upstream probe fires within the 5-second window
"""
from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    get_default_session_store_dep,
    get_engine_dep,
    get_models_service_dep,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.models_service import (
    ModelsService,
)
from lmchat.session.sqlite_store import SQLiteSessionStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOW_N: int = 2**10
_LOW_COST: dict[str, int] = {"_hash_n": _LOW_N, "_hash_r": 8, "_hash_p": 1}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Required env vars + cache clearing."""
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
    db_path = tmp_path / "test_lmstudio_health.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
def mock_models_service() -> MagicMock:
    """Mock ModelsService with controllable live_health + _refresh_if_loaded_cache_stale."""
    svc = MagicMock(spec=ModelsService)
    # Default: reachable, 1 model loaded
    svc.live_health = AsyncMock(
        return_value={
            "reachable": True,
            "loaded_count": 1,
            "auth_failed": False,
            "last_probe_at": time.time(),
        }
    )
    svc.list_loaded = AsyncMock(return_value=[])
    return svc


@pytest.fixture()
def test_client(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
) -> Generator[TestClient]:
    """TestClient wired to per-test engine + mock ModelsService."""
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)

    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_models_service_dep] = lambda: mock_models_service

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.model_catalog = None  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_and_login(client: TestClient) -> None:
    """Register a user and log them in (sets session cookie)."""
    client.post(
        "/api/auth/register",
        data={"username": "alice", "password": "correct-horse-battery"},
    )
    client.post("/api/auth/login", data={"username": "alice", "password": "correct-horse-battery"})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_health_requires_auth(test_client: TestClient) -> None:
    """GET /api/lmstudio/health without a session → 401."""
    resp = test_client.get("/api/lmstudio/health")
    assert resp.status_code == 401


def test_health_returns_200_when_authed(
    test_client: TestClient,
    mock_models_service: MagicMock,
) -> None:
    """GET /api/lmstudio/health with valid session → 200 with correct shape."""
    _register_and_login(test_client)
    resp = test_client.get("/api/lmstudio/health")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert "reachable" in body
    assert "loaded_count" in body
    assert "auth_failed" in body
    assert "last_probe_at" in body


def test_health_reachable_true_when_probe_succeeds(
    test_client: TestClient,
    mock_models_service: MagicMock,
) -> None:
    """reachable=True and loaded_count>0 when the upstream probe succeeds."""
    now = time.time()
    mock_models_service.live_health = AsyncMock(
        return_value={
            "reachable": True,
            "loaded_count": 2,
            "auth_failed": False,
            "last_probe_at": now,
        }
    )
    _register_and_login(test_client)
    resp = test_client.get("/api/lmstudio/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert body["loaded_count"] == 2
    assert body["auth_failed"] is False


def test_health_reachable_false_when_probe_fails(
    test_client: TestClient,
    mock_models_service: MagicMock,
) -> None:
    """reachable=False when the upstream connection raises (LM Studio is down)."""
    now = time.time()
    mock_models_service.live_health = AsyncMock(
        return_value={
            "reachable": False,
            "loaded_count": 0,
            "auth_failed": False,
            "last_probe_at": now,
        }
    )
    _register_and_login(test_client)
    resp = test_client.get("/api/lmstudio/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is False
    assert body["loaded_count"] == 0


def test_health_calls_live_health_on_service(
    test_client: TestClient,
    mock_models_service: MagicMock,
) -> None:
    """GET /api/lmstudio/health delegates to models_service.live_health()."""
    _register_and_login(test_client)
    test_client.get("/api/lmstudio/health")
    mock_models_service.live_health.assert_called_once()


# ---------------------------------------------------------------------------
# Unit tests for ModelsService.live_health() directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_health_reachable_false_on_connect_error() -> None:
    """live_health() returns reachable=False when the upstream raises ConnectError.

    Simulates LM Studio being down: _probe_upstream raises httpx.ConnectError
    which refresh() catches as httpx.RequestError, sets _last_probe_reachable=False,
    and returns. live_health() then reads that flag and reports reachable=False.
    """
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    svc = ModelsService(
        http_client=mock_client,
        base_url="http://localhost:1234",
        loaded_models_ttl=0.0,  # always stale → always re-probes
    )

    result = await svc.live_health()

    assert result["reachable"] is False
    assert result["loaded_count"] == 0
    assert result["auth_failed"] is False
    assert result["last_probe_at"] is not None


@pytest.mark.asyncio
async def test_live_health_reachable_true_on_success() -> None:
    """live_health() returns reachable=True when the upstream probe succeeds."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(
        return_value={
            "models": [
                {
                    "key": "qwen3",
                    "loaded_instances": [{"id": "qwen3:0", "config": {"context_length": 8192}}],
                    "capabilities": {"vision": False, "trained_for_tool_use": False},
                    "type": "llm",
                }
            ]
        }
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=mock_response)

    svc = ModelsService(
        http_client=mock_client,
        base_url="http://localhost:1234",
        loaded_models_ttl=0.0,  # always stale → always re-probes
    )

    result = await svc.live_health()

    assert result["reachable"] is True
    assert result["loaded_count"] == 1
    assert result["auth_failed"] is False
    assert result["last_probe_at"] is not None


@pytest.mark.asyncio
async def test_live_health_ttl_rate_limits_upstream_probes() -> None:
    """live_health() triggers at most one upstream probe per TTL window.

    Two successive calls within the TTL window should only produce one
    HTTP request to LM Studio.
    """
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"models": []})
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=mock_response)

    svc = ModelsService(
        http_client=mock_client,
        base_url="http://localhost:1234",
        loaded_models_ttl=60.0,  # 60-second TTL
    )

    # First call: cache is cold (_cache_timestamp=0), so a probe fires.
    await svc.live_health()
    # Second call: cache is fresh (within TTL), no second probe.
    await svc.live_health()

    # Exactly one upstream GET should have been made.
    assert mock_client.get.call_count == 1


# ---------------------------------------------------------------------------
# Finding 3: cold-start 401 → reachable=True, auth_failed=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_health_cold_start_401_sets_reachable_true() -> None:
    """A 401 on the FIRST probe (cold-start) must set reachable=True.

    Transport succeeded (LM Studio IS reachable but rejected our key).
    Before the fix, _last_probe_reachable stayed None → resolved to False,
    reporting the service as unreachable even though it answered.
    """
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "401 Unauthorized",
            request=MagicMock(),
            response=mock_response,
        )
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=mock_response)

    svc = ModelsService(
        http_client=mock_client,
        base_url="http://localhost:1234",
        loaded_models_ttl=0.0,  # always stale → always re-probes
    )

    # State check before any probe: _last_probe_reachable is None.
    assert svc._last_probe_reachable is None  # noqa: SLF001

    result = await svc.live_health()

    # 401 → transport succeeded → reachable must be True.
    assert result["reachable"] is True, (
        "A 401 response means LM Studio IS reachable; reachable must be True"
    )
    assert result["auth_failed"] is True
    assert result["last_probe_at"] is not None


@pytest.mark.asyncio
async def test_live_health_401_does_not_set_reachable_false() -> None:
    """A 401 must never set reachable=False (contrast with RequestError)."""
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "401 Unauthorized",
            request=MagicMock(),
            response=mock_response,
        )
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=mock_response)

    svc = ModelsService(
        http_client=mock_client,
        base_url="http://localhost:1234",
        loaded_models_ttl=0.0,
    )

    await svc.live_health()

    # The internal flag must be True (or at minimum not False).
    assert svc._last_probe_reachable is True  # noqa: SLF001


# ---------------------------------------------------------------------------
# Finding 4: down-state rate-limit — failed probe stamps _cache_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_health_down_state_rate_limits_probes() -> None:
    """live_health() probes at most once per TTL even when LM Studio is DOWN.

    Before the fix a failed probe never advanced _cache_timestamp, so the
    cache stayed "stale forever" → every live_health() call re-probed,
    creating a probe storm during outages (each probe blocks up to 5s).

    With the fix the failed probe stamps _cache_timestamp, so a second call
    within the TTL window skips the upstream request.
    """
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    svc = ModelsService(
        http_client=mock_client,
        base_url="http://localhost:1234",
        loaded_models_ttl=60.0,  # 60-second TTL
    )

    # First call: cache is cold → probe fires → fails → timestamps stamped.
    result1 = await svc.live_health()
    assert result1["reachable"] is False

    # Second call within TTL: must NOT re-probe (cache timestamp was stamped).
    result2 = await svc.live_health()
    assert result2["reachable"] is False

    # Only ONE upstream request despite two live_health() calls.
    assert mock_client.get.call_count == 1, (
        "Down-state probe storm: expected exactly 1 upstream GET, "
        f"got {mock_client.get.call_count}"
    )
