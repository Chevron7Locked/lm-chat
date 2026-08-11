# SPDX-License-Identifier: Apache-2.0
"""Tests for project-name minimum-length enforcement (§1E — 54-"P" gap).

Covers:
- Empty / whitespace-only → 422 (existing behavior; pin it)
- Single character "P", "a", "1" → 422 (new behavior)
- Two-char "ab" → 422 (new behavior, the threshold is 3)
- Three-char "Cat" → 200 (admitted)
- Long valid name → 200
- Control characters interspersed → 422 (existing)

Every assertion is made via the policy function directly AND via
POST /api/projects route integration.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_models_service_dep
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.utils.hashing import hash_password
from lmchat.utils.text_input_policy import (
    SHORT_FIELD_MAX_LENGTH,
    TextInputPolicyError,
    validate_text,
)

_LOW_N: int = 2**10


# ---------------------------------------------------------------------------
# Policy-direct tests
# ---------------------------------------------------------------------------


def test_validate_empty_rejected() -> None:
    """Empty name raises — existing behavior pinned."""
    with pytest.raises(TextInputPolicyError):
        validate_text("", field="name", max_length=SHORT_FIELD_MAX_LENGTH)


def test_validate_whitespace_only_rejected() -> None:
    """Whitespace-only raises — existing behavior pinned."""
    with pytest.raises(TextInputPolicyError):
        validate_text("   ", field="name", max_length=SHORT_FIELD_MAX_LENGTH)


def test_validate_single_char_P_rejected() -> None:
    """Single char 'P' is below min_length=3 — new behavior."""
    with pytest.raises(TextInputPolicyError):
        validate_text(
            "P",
            field="name",
            max_length=SHORT_FIELD_MAX_LENGTH,
            min_length=3,
        )


def test_validate_single_char_a_rejected() -> None:
    """Single char 'a' is below min_length=3 — new behavior."""
    with pytest.raises(TextInputPolicyError):
        validate_text(
            "a",
            field="name",
            max_length=SHORT_FIELD_MAX_LENGTH,
            min_length=3,
        )


def test_validate_single_digit_rejected() -> None:
    """Single digit '1' is below min_length=3 — new behavior."""
    with pytest.raises(TextInputPolicyError):
        validate_text(
            "1",
            field="name",
            max_length=SHORT_FIELD_MAX_LENGTH,
            min_length=3,
        )


def test_validate_two_char_ab_rejected() -> None:
    """Two-char 'ab' is below min_length=3 — new behavior."""
    with pytest.raises(TextInputPolicyError):
        validate_text(
            "ab",
            field="name",
            max_length=SHORT_FIELD_MAX_LENGTH,
            min_length=3,
        )


def test_validate_three_char_Cat_accepted() -> None:
    """Three-char 'Cat' meets min_length=3 — admitted."""
    result = validate_text(
        "Cat",
        field="name",
        max_length=SHORT_FIELD_MAX_LENGTH,
        min_length=3,
    )
    assert result == "Cat"


def test_validate_long_valid_name_accepted() -> None:
    """Long valid name meets min_length=3 — admitted."""
    result = validate_text(
        "My Research Project 2026",
        field="name",
        max_length=SHORT_FIELD_MAX_LENGTH,
        min_length=3,
    )
    assert result == "My Research Project 2026"


def test_validate_control_chars_rejected() -> None:
    """Name with control chars is rejected — existing behavior pinned."""
    with pytest.raises(TextInputPolicyError):
        validate_text(
            "Cat\x00",
            field="name",
            max_length=SHORT_FIELD_MAX_LENGTH,
            min_length=3,
        )


# ---------------------------------------------------------------------------
# Route-integration tests
# ---------------------------------------------------------------------------


def _make_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/policy_names_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()
    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models
    return app


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
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
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/policy_names_route_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(tmp_path: Path, username: str) -> int:
    pw_hash = hash_password("test-pw", n=_LOW_N, r=8, p=1)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO users (username, password_hash, is_admin) "
                    "VALUES (:u, :pw, 0)"
                ),
                {"u": username, "pw": pw_hash},
            )
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE username = :u"),
                    {"u": username},
                )
            ).fetchone()
            return int(row[0])  # type: ignore[index]
    finally:
        await eng.dispose()


def _login(client: TestClient, username: str) -> None:
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": "test-pw"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.anyio
async def test_route_empty_name_422(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with empty name returns 422."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": ""})
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_route_whitespace_name_422(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with whitespace-only name returns 422."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": "   "})
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_route_single_char_P_422(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with single-char 'P' returns 422."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": "P"})
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_route_single_char_a_422(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with single-char 'a' returns 422."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": "a"})
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_route_single_digit_422(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with single digit '1' returns 422."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": "1"})
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_route_two_char_ab_422(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with two-char 'ab' returns 422."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": "ab"})
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_route_three_char_Cat_201(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with three-char 'Cat' returns 201."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": "Cat"})
    assert resp.status_code == 201, resp.text


@pytest.mark.anyio
async def test_route_long_valid_name_201(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with long valid name returns 201."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post(
        "/api/projects", data={"name": "My Research Project"}
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.anyio
async def test_route_control_chars_422(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/projects with control chars in name returns 422."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post(
        "/api/projects", data={"name": "Cat\x00"}
    )
    assert resp.status_code == 422, resp.text