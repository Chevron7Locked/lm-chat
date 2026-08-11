# SPDX-License-Identifier: Apache-2.0
"""Integration tests for /readyz endpoint.

Tests for the /readyz route.

The readiness probe gates its 200/503 status on DB + sessions only, with a
3-strike rule: three *consecutive* cycles where DB or sessions fails are
needed before the endpoint returns 503.  Tests that want to see a 503 must
call the endpoint at least three times (or mock to always fail).

LM Studio reachability is probed and reported in the response body
(``checks.lm_studio`` / ``degraded``) for observability only — it never
gates the 200/503 status, because an admin closing the LM Studio desktop
app is normal operation for this local-first app.

Tests
-----
- test_readyz_returns_200_when_all_checks_pass       — DB+sessions+LMStudio all ok
- test_readyz_returns_503_when_db_ping_fails          — DB probe fails after 3 strikes
- test_readyz_lm_studio_unreachable_stays_ready       — LM Studio down never gates readiness
"""
from __future__ import annotations

from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._meta import _STRIKE_THRESHOLD
from lmchat.services.auth_service import _reset_dummy_hash_cache

# ---------------------------------------------------------------------------
# App factory (same isolation pattern as test_streaming.py)
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """Build the app with per-test DB isolation."""
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/readyz_test.db"
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


@pytest.fixture()
def test_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    """TestClient with per-test DB."""
    app = _make_app(tmp_path, monkeypatch)

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


def _reset_strike_count() -> None:
    """Reset the module-level strike counter between tests."""
    import lmchat.routes._meta as meta

    meta._strike_count = 0  # noqa: SLF001


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_readyz_returns_200_when_all_checks_pass(
    test_client: TestClient,
) -> None:
    """All probes pass (real DB, mocked LM Studio) → 200."""
    _reset_strike_count()

    with patch(
        "lmchat.routes._meta._probe_lm_studio",
        new=AsyncMock(return_value=True),
    ):
        resp = test_client.get("/readyz")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"] is True
    assert body["checks"]["sessions"] is True
    assert body["checks"]["lm_studio"] is True

    _reset_strike_count()


async def test_readyz_returns_200_healthy_checks_structure(
    test_client: TestClient,
) -> None:
    """Response body includes required checks keys."""
    _reset_strike_count()

    with patch(
        "lmchat.routes._meta._probe_lm_studio",
        new=AsyncMock(return_value=True),
    ):
        resp = test_client.get("/readyz")

    body = resp.json()
    assert "checks" in body
    assert set(body["checks"].keys()) == {"db", "sessions", "lm_studio"}

    _reset_strike_count()


async def test_readyz_returns_503_when_db_ping_fails(
    test_client: TestClient,
) -> None:
    """DB probe fails → 503 after hitting the 3-strike threshold."""
    _reset_strike_count()

    with (
        patch(
            "lmchat.routes._meta._probe_db",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "lmchat.routes._meta._probe_sessions",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "lmchat.routes._meta._probe_lm_studio",
            new=AsyncMock(return_value=True),
        ),
    ):
        responses = [test_client.get("/readyz") for _ in range(_STRIKE_THRESHOLD)]

    # The last response must be 503.
    assert responses[-1].status_code == 503, responses[-1].text
    body = responses[-1].json()
    assert body["status"] == "not_ready"
    assert body["checks"]["db"] is False

    _reset_strike_count()


async def test_readyz_lm_studio_unreachable_stays_ready(
    test_client: TestClient,
) -> None:
    """LM Studio down must NOT gate readiness — even across the strike window.

    Pins the split-readiness contract: LM Studio is an optional local
    upstream for this local-first app (the admin closing the desktop app
    is normal, expected operation), so it must never pull the pod from
    Service routing. This asserts 200/ready for every call in a run long
    enough to have tripped the old 3-strike rule, with the outage surfaced
    as ``degraded`` for observability only.

    Red-on-revert: if ``readyz()`` is reverted to gate on
    ``db_ok and sessions_ok and lm_ok``, the last response here flips to
    503 and this assertion fails.
    """
    _reset_strike_count()

    with patch(
        "lmchat.routes._meta._probe_lm_studio",
        new=AsyncMock(return_value=False),
    ):
        responses = [test_client.get("/readyz") for _ in range(_STRIKE_THRESHOLD + 2)]

    for resp in responses:
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["lm_studio"] is False
        assert body["checks"]["db"] is True
        assert body["checks"]["sessions"] is True
        assert body.get("degraded") == ["lm_studio"]

    import lmchat.routes._meta as meta

    assert meta._strike_count == 0  # noqa: SLF001

    _reset_strike_count()


async def test_readyz_strike_count_resets_on_success(
    test_client: TestClient,
) -> None:
    """Strike counter resets when a fully-passing DB+sessions cycle fires."""
    _reset_strike_count()

    with patch(
        "lmchat.routes._meta._probe_db",
        new=AsyncMock(return_value=False),
    ):
        for _ in range(_STRIKE_THRESHOLD - 1):
            test_client.get("/readyz")

    import lmchat.routes._meta as meta

    assert meta._strike_count == _STRIKE_THRESHOLD - 1  # noqa: SLF001

    resp = test_client.get("/readyz")

    assert resp.status_code == 200, resp.text
    assert meta._strike_count == 0  # noqa: SLF001

    _reset_strike_count()
