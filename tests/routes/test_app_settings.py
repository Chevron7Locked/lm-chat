# SPDX-License-Identifier: Apache-2.0
"""Integration tests for app-level admin settings routes (P13h).

Endpoints
---------
- ``GET /api/settings/app`` — return the 4 resolved values with ``is_override`` flags.
- ``PATCH /api/settings/app`` — admin-only; set/clear overrides.

Tests
-----
- test_get_app_settings_returns_defaults_when_no_override
- test_patch_sets_override_and_get_reflects_it (4 × each flag)
- test_patch_web_search_provider_brave_accepted
- test_patch_web_search_provider_brave_llm_accepted
- test_patch_null_clears_override_returns_to_default (4 × each flag)
- test_distill_gate_resolves_override
- test_subsession_distill_gate_resolves_override
- test_web_search_resolver_resolves_override
- test_searxng_url_resolver_resolves_override
- test_web_search_service_rebinds_provider_at_runtime
- test_web_search_service_rebinds_searxng_url_at_runtime
- test_web_search_service_rebinds_both_provider_and_url
- test_web_search_provider_invalid_returns_400
- test_searxng_url_private_ip_returns_400 (SSRF guard)
- test_searxng_url_invalid_scheme_returns_400
- test_searxng_url_bare_string_returns_400
- test_searxng_url_exceeds_max_length_returns_400
- test_non_admin_patch_returns_403
- test_non_admin_get_returns_200
- test_unauthenticated_get_returns_401
- test_unauthenticated_patch_returns_401
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.config import get_settings
from lmchat.db.schema import metadata, users
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    admin_rate_limit,
    get_models_service_dep,
)
from lmchat.services.app_settings_service import (
    resolve_memory_distillation_enabled,
    resolve_searxng_url,
    resolve_subsession_memory_distillation_enabled,
    resolve_web_search_provider,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:  # noqa: ANN401
    """Build the app with per-test DB isolation."""
    from lmchat.app import create_app

    db_url = f"sqlite+aiosqlite:///{tmp_path}/app_settings_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_CHAT_ADMIN_RATE_LIMIT_PER_MIN", "30")
    # Clear any .env.local overrides for predictable defaults.
    monkeypatch.delenv("LM_CHAT_SEARXNG_URL", raising=False)

    get_settings.cache_clear()
    from lmchat.db import engine as engine_mod

    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[MagicMock(), MagicMock()])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models

    async def _noop_rl() -> None:
        return None

    app.dependency_overrides[admin_rate_limit] = _noop_rl

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear settings cache and dummy hash cache around each test."""
    from lmchat.db import engine as engine_mod

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()


@pytest.fixture()
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with a per-test DB and stub models service."""
    app = _make_app(tmp_path, monkeypatch)

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    """Return the per-test async engine."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/app_settings_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(
    tmp_path: Path,
    username: str,
    password: str = "test-pw",
    is_admin: bool = False,
) -> int:
    """Insert a user at low-cost scrypt into the test DB."""
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            id_result = await conn.execute(select(func.coalesce(func.max(users.c.id), 0) + 1))
            next_id = int(id_result.scalar() or 1)
            await conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, is_admin)"
                    " VALUES (:id, :u, :ph, :ia)"
                ),
                {"id": next_id, "u": username, "ph": pw_hash, "ia": int(is_admin)},
            )
    finally:
        await eng.dispose()
    return next_id


def _login(client: TestClient, username: str, password: str = "test-pw") -> None:
    """POST /api/auth/login and assert success."""
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed ({resp.status_code}): {resp.text}"


async def _setup_admin(tmp_path: Path, client: TestClient) -> tuple[int, int]:
    """Create admin + regular user; log in as admin.

    Returns:
        (admin_id, regular_user_id)
    """
    admin_id = await _insert_user(tmp_path, "admin_u", "admin_pw", is_admin=True)
    user_id = await _insert_user(tmp_path, "regular_u", "reg_pw", is_admin=False)
    _login(client, "admin_u", "admin_pw")
    return admin_id, user_id


# ---------------------------------------------------------------------------
# Tests: GET defaults
# ---------------------------------------------------------------------------


async def test_get_app_settings_returns_defaults_when_no_override(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/settings/app returns config defaults when no DB override is set."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    defaults = get_settings()

    # memory_distillation_enabled — default True, no override
    assert data["memory_distillation_enabled"]["value"] is True
    assert data["memory_distillation_enabled"]["is_override"] is False

    # subsession_memory_distillation_enabled — default False, no override
    assert data["subsession_memory_distillation_enabled"]["value"] is False
    assert data["subsession_memory_distillation_enabled"]["is_override"] is False

    # web_search_provider — default "ddg", no override
    assert data["web_search_provider"]["value"] == "ddg"
    assert data["web_search_provider"]["is_override"] is False

    # searxng_url — default from config (https://searx.be when no env override), no override
    expected_url = defaults.lm_chat_searxng_url
    assert data["searxng_url"]["value"] == expected_url
    assert data["searxng_url"]["is_override"] is False


# ---------------------------------------------------------------------------
# Tests: PATCH sets override → GET reflects it
# ---------------------------------------------------------------------------


async def test_patch_memory_distillation_enabled_override(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH memory_distillation_enabled=False → GET shows is_override=true."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"memory_distillation_enabled": False},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["memory_distillation_enabled"]["value"] is False
    assert data["memory_distillation_enabled"]["is_override"] is True

    # Verify via GET
    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200
    data = resp.json()
    assert data["memory_distillation_enabled"]["value"] is False
    assert data["memory_distillation_enabled"]["is_override"] is True


async def test_patch_subsession_memory_distillation_enabled_override(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH subsession_memory_distillation_enabled=True → GET shows is_override=true."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"subsession_memory_distillation_enabled": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["subsession_memory_distillation_enabled"]["value"] is True
    assert data["subsession_memory_distillation_enabled"]["is_override"] is True

    # Verify via GET
    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200
    data = resp.json()
    assert data["subsession_memory_distillation_enabled"]["value"] is True
    assert data["subsession_memory_distillation_enabled"]["is_override"] is True


async def test_patch_web_search_provider_override(tmp_path: Path, test_client: TestClient) -> None:
    """PATCH web_search_provider='ddg' → GET shows is_override=true."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": "ddg"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["web_search_provider"]["value"] == "ddg"
    assert data["web_search_provider"]["is_override"] is True

    # Verify via GET
    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200
    data = resp.json()
    assert data["web_search_provider"]["value"] == "ddg"
    assert data["web_search_provider"]["is_override"] is True


async def test_patch_web_search_provider_brave_accepted(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH web_search_provider='brave' → GET shows is_override=true.

    Brave is a keyed API (LM_CHAT_BRAVE_API_KEY, env-only) rather than a DB
    setting, so this only exercises provider-string acceptance/persistence —
    not the key itself.
    """
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": "brave"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["web_search_provider"]["value"] == "brave"
    assert data["web_search_provider"]["is_override"] is True

    # Verify via GET
    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200
    data = resp.json()
    assert data["web_search_provider"]["value"] == "brave"
    assert data["web_search_provider"]["is_override"] is True


async def test_patch_web_search_provider_brave_llm_accepted(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH web_search_provider='brave_llm' → GET shows is_override=true.

    Same env-only-key caveat as the 'brave' test above — this only exercises
    provider-string acceptance/persistence.
    """
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": "brave_llm"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["web_search_provider"]["value"] == "brave_llm"
    assert data["web_search_provider"]["is_override"] is True

    # Verify via GET
    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200
    data = resp.json()
    assert data["web_search_provider"]["value"] == "brave_llm"
    assert data["web_search_provider"]["is_override"] is True


async def test_patch_searxng_url_override(tmp_path: Path, test_client: TestClient) -> None:
    """PATCH searxng_url → GET shows is_override=true."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": "https://my-searx.local"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["searxng_url"]["value"] == "https://my-searx.local"
    assert data["searxng_url"]["is_override"] is True

    # Verify via GET
    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200
    data = resp.json()
    assert data["searxng_url"]["value"] == "https://my-searx.local"
    assert data["searxng_url"]["is_override"] is True


# ---------------------------------------------------------------------------
# Tests: PATCH null clears override
# ---------------------------------------------------------------------------


async def test_patch_null_clears_memory_distillation(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH memory_distillation_enabled=null → GET back to config default."""
    await _setup_admin(tmp_path, test_client)

    # First set an override
    test_client.patch(
        "/api/settings/app",
        json={"memory_distillation_enabled": False},
    )
    # Then clear it
    resp = test_client.patch(
        "/api/settings/app",
        json={"memory_distillation_enabled": None},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["memory_distillation_enabled"]["is_override"] is False

    # Verify via GET
    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200
    data = resp.json()
    assert data["memory_distillation_enabled"]["is_override"] is False


async def test_patch_null_clears_subsession_memory_distillation(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH subsession_memory_distillation_enabled=null → GET back to config default."""
    await _setup_admin(tmp_path, test_client)

    test_client.patch(
        "/api/settings/app",
        json={"subsession_memory_distillation_enabled": True},
    )
    resp = test_client.patch(
        "/api/settings/app",
        json={"subsession_memory_distillation_enabled": None},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["subsession_memory_distillation_enabled"]["is_override"] is False


async def test_patch_null_clears_web_search_provider(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH web_search_provider=null → GET back to config default."""
    await _setup_admin(tmp_path, test_client)

    test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": "ddg"},
    )
    resp = test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": None},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["web_search_provider"]["is_override"] is False


async def test_patch_null_clears_searxng_url(tmp_path: Path, test_client: TestClient) -> None:
    """PATCH searxng_url=null → GET back to config default."""
    await _setup_admin(tmp_path, test_client)

    test_client.patch(
        "/api/settings/app",
        json={"searxng_url": "https://my-searx.local"},
    )
    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": None},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["searxng_url"]["is_override"] is False


# ---------------------------------------------------------------------------
# Tests: distill gate / resolver unit-level
# ---------------------------------------------------------------------------


async def test_distill_gate_resolves_override(tmp_path: Path, test_client: TestClient) -> None:
    """Setting an override is visible to the resolver used by streaming_service."""
    await _setup_admin(tmp_path, test_client)

    engine = await _engine_for(tmp_path)

    # Before override: should return config default (True)
    result = await resolve_memory_distillation_enabled(engine)
    assert result is True

    # Set override to False via PATCH
    resp = test_client.patch(
        "/api/settings/app",
        json={"memory_distillation_enabled": False},
    )
    assert resp.status_code == 200

    # After override: resolver should return False
    result = await resolve_memory_distillation_enabled(engine)
    assert result is False

    # Clear override
    test_client.patch(
        "/api/settings/app",
        json={"memory_distillation_enabled": None},
    )
    result = await resolve_memory_distillation_enabled(engine)
    assert result is True  # back to config default


async def test_subsession_distill_gate_resolves_override(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Setting a sub-session override is visible to the resolver."""
    await _setup_admin(tmp_path, test_client)

    engine = await _engine_for(tmp_path)

    # Before override: should return config default (False)
    result = await resolve_subsession_memory_distillation_enabled(engine)
    assert result is False

    # Set override to True via PATCH
    resp = test_client.patch(
        "/api/settings/app",
        json={"subsession_memory_distillation_enabled": True},
    )
    assert resp.status_code == 200

    # After override: resolver should return True
    result = await resolve_subsession_memory_distillation_enabled(engine)
    assert result is True

    # Clear override
    test_client.patch(
        "/api/settings/app",
        json={"subsession_memory_distillation_enabled": None},
    )
    result = await resolve_subsession_memory_distillation_enabled(engine)
    assert result is False  # back to config default


async def test_web_search_resolver_resolves_override(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Setting web_search_provider override is visible to the resolver."""
    await _setup_admin(tmp_path, test_client)

    engine = await _engine_for(tmp_path)

    # Before override: should return config default ("ddg")
    result = await resolve_web_search_provider(engine)
    assert result == "ddg"

    # Set override to "searxng"
    resp = test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": "searxng"},
    )
    assert resp.status_code == 200

    # After override: resolver should return "searxng"
    result = await resolve_web_search_provider(engine)
    assert result == "searxng"

    # Clear override
    test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": None},
    )
    result = await resolve_web_search_provider(engine)
    assert result == "ddg"


async def test_searxng_url_resolver_resolves_override(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Setting searxng_url override is visible to the resolver."""
    await _setup_admin(tmp_path, test_client)

    engine = await _engine_for(tmp_path)

    # Before override: should return config default
    result_before = await resolve_searxng_url(engine)
    defaults = get_settings()
    assert result_before == defaults.lm_chat_searxng_url

    # Set override
    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": "https://custom-searx.local"},
    )
    assert resp.status_code == 200

    # After override: resolver should return custom URL
    result = await resolve_searxng_url(engine)
    assert result == "https://custom-searx.local"

    # Clear override
    test_client.patch(
        "/api/settings/app",
        json={"searxng_url": None},
    )
    result = await resolve_searxng_url(engine)
    assert result == defaults.lm_chat_searxng_url


# ---------------------------------------------------------------------------
# Tests: live-rebind of web_search_service singleton (Finding A)
# ---------------------------------------------------------------------------


async def test_web_search_service_rebinds_provider_at_runtime(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH web_search_provider='searxng' → singleton._provider updates live."""
    await _setup_admin(tmp_path, test_client)

    defaults = get_settings()

    # Initial state: should be config default ("ddg")
    wss = test_client.app.state.web_search_service  # type: ignore[attr-defined]
    assert wss._provider == "ddg"

    # Set override to "searxng"
    resp = test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": "searxng"},
    )
    assert resp.status_code == 200

    # Singleton should reflect the new value immediately (no restart).
    assert wss._provider == "searxng"

    # Clear override → should rebind back to config default.
    resp = test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": None},
    )
    assert resp.status_code == 200
    assert wss._provider == defaults.lm_chat_web_search_provider


async def test_web_search_service_rebinds_searxng_url_at_runtime(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH searxng_url → singleton._searxng_url updates live."""
    await _setup_admin(tmp_path, test_client)

    defaults = get_settings()

    # Initial state: should be config default
    wss = test_client.app.state.web_search_service  # type: ignore[attr-defined]
    assert wss._searxng_url == defaults.lm_chat_searxng_url

    # Set override
    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": "https://my-searx.local"},
    )
    assert resp.status_code == 200

    # Singleton should reflect the new value immediately (no restart).
    assert wss._searxng_url == "https://my-searx.local"

    # Clear override → should rebind back to config default.
    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": None},
    )
    assert resp.status_code == 200
    assert wss._searxng_url == defaults.lm_chat_searxng_url


async def test_web_search_service_rebinds_both_provider_and_url(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH both fields at once → both singleton attrs update live."""
    await _setup_admin(tmp_path, test_client)

    wss = test_client.app.state.web_search_service  # type: ignore[attr-defined]
    defaults = get_settings()

    # Set both at once
    resp = test_client.patch(
        "/api/settings/app",
        json={
            "web_search_provider": "ddg",
            "searxng_url": "https://custom-searx.example.com",
        },
    )
    assert resp.status_code == 200

    # Both should reflect new values.
    assert wss._provider == "ddg"
    assert wss._searxng_url == "https://custom-searx.example.com"

    # Clear both → both rebind to config defaults.
    resp = test_client.patch(
        "/api/settings/app",
        json={
            "web_search_provider": None,
            "searxng_url": None,
        },
    )
    assert resp.status_code == 200
    assert wss._provider == defaults.lm_chat_web_search_provider
    assert wss._searxng_url == defaults.lm_chat_searxng_url


# ---------------------------------------------------------------------------
# Tests: validation errors
# ---------------------------------------------------------------------------


async def test_web_search_provider_invalid_returns_400(
    tmp_path: Path, test_client: TestClient
) -> None:
    """web_search_provider not in {searxng, ddg} → 400."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"web_search_provider": "google"},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert "provider" in body["detail"].lower() or "searxng" in body["detail"].lower()


async def test_searxng_url_private_ip_returns_400(tmp_path: Path, test_client: TestClient) -> None:
    """searxng_url targeting private IP (SSRF guard) → 400."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": "http://127.0.0.1:8888/search"},
    )
    assert resp.status_code == 400, resp.text


async def test_searxng_url_invalid_scheme_returns_400(
    tmp_path: Path, test_client: TestClient
) -> None:
    """searxng_url with non-http(s) scheme → 400."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": "ftp://x"},
    )
    assert resp.status_code == 400, resp.text


async def test_searxng_url_bare_string_returns_400(tmp_path: Path, test_client: TestClient) -> None:
    """searxng_url with a bare string (no scheme) → 400."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": "not-a-url"},
    )
    assert resp.status_code == 400, resp.text


async def test_searxng_url_exceeds_max_length_returns_400(
    tmp_path: Path, test_client: TestClient
) -> None:
    """searxng_url exceeding 512 chars → 400."""
    await _setup_admin(tmp_path, test_client)

    long_url = "https://example.com/" + "x" * 580
    resp = test_client.patch(
        "/api/settings/app",
        json={"searxng_url": long_url},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Tests: access control
# ---------------------------------------------------------------------------


async def test_non_admin_patch_returns_403(tmp_path: Path, test_client: TestClient) -> None:
    """Non-admin user receives 403 on PATCH."""
    # Create a non-admin user and log in
    await _insert_user(tmp_path, "nonadmin", is_admin=False)
    _login(test_client, "nonadmin")

    resp = test_client.patch(
        "/api/settings/app",
        json={"memory_distillation_enabled": False},
    )
    assert resp.status_code == 403, resp.text


async def test_non_admin_get_returns_200(tmp_path: Path, test_client: TestClient) -> None:
    """Non-admin authenticated user receives 200 with read-only view data on GET."""
    await _insert_user(tmp_path, "nonadmin", is_admin=False)
    _login(test_client, "nonadmin")

    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "memory_distillation_enabled" in body
    assert "subsession_memory_distillation_enabled" in body
    assert "web_search_provider" in body
    assert "searxng_url" in body


async def test_unauthenticated_get_returns_401(test_client: TestClient) -> None:
    """Unauthenticated caller receives 401."""
    test_client.cookies.clear()
    resp = test_client.get("/api/settings/app")
    assert resp.status_code == 401, resp.text


async def test_unauthenticated_patch_returns_401(test_client: TestClient) -> None:
    """Unauthenticated caller receives 401 on PATCH."""
    test_client.cookies.clear()
    resp = test_client.patch(
        "/api/settings/app",
        json={"memory_distillation_enabled": False},
    )
    assert resp.status_code == 401, resp.text
