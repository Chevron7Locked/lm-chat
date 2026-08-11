# SPDX-License-Identifier: Apache-2.0
"""Integration tests for prompt library routes.

Covers:
- GET /api/prompts — 200 list[Prompt] (Invariant 3), 401 unauthed.
- POST /api/prompts — 201 created, 409 name conflict, 401 unauthed.
- PATCH /api/prompts/{id} — 200 updated, 404 cross-user, 409 conflict, 401.
- DELETE /api/prompts/{id} — 204, 404 cross-user, 401.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path  # used by _set_env
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from lmchat.app import create_app
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes.prompt_library import _get_prompt_service
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.prompt_library_service import (
    Prompt,
    PromptLibraryService,
    PromptNameConflictError,
    PromptNotFoundError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[None]:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/prompt_routes_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()



def _make_prompt(
    pid: int = 1, uid: int = 1, name: str = "greeting", content: str = "Hello!"
) -> Prompt:
    now = datetime.now(UTC)
    return Prompt(
        id=pid, user_id=uid, name=name, content=content, created_at=now, updated_at=now
    )


@pytest.fixture()
def mock_prompt_svc() -> MagicMock:
    svc = MagicMock(spec=PromptLibraryService)
    svc.list_for_user = AsyncMock(return_value=[_make_prompt()])
    svc.create = AsyncMock(return_value=_make_prompt())
    svc.update = AsyncMock(return_value=_make_prompt())
    svc.delete = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def test_client(
    mock_prompt_svc: MagicMock,
) -> Generator[TestClient]:
    app = create_app()
    app.dependency_overrides[_get_prompt_service] = lambda: mock_prompt_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


def _login(client: TestClient, username: str = "alice") -> None:
    client.post("/api/auth/register", data={"username": username, "password": "correct-horse-battery"})
    client.post("/api/auth/login", data={"username": username, "password": "correct-horse-battery"})


# ---------------------------------------------------------------------------
# GET /api/prompts
# ---------------------------------------------------------------------------


def test_list_prompts_200(test_client: TestClient) -> None:
    """Authed user gets list[Prompt] (Invariant 3: plain array)."""
    _login(test_client)
    resp = test_client.get("/api/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["name"] == "greeting"


def test_list_prompts_401_unauthed(test_client: TestClient) -> None:
    resp = test_client.get("/api/prompts")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/prompts
# ---------------------------------------------------------------------------


def test_create_prompt_201(test_client: TestClient) -> None:
    _login(test_client)
    resp = test_client.post(
        "/api/prompts",
        data={"name": "greeting", "content": "Hello!"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "greeting"


def test_create_prompt_409_conflict(
    test_client: TestClient,
    mock_prompt_svc: MagicMock,
) -> None:
    _login(test_client)
    mock_prompt_svc.create = AsyncMock(side_effect=PromptNameConflictError("dup"))
    resp = test_client.post(
        "/api/prompts",
        data={"name": "dup", "content": "x"},
    )
    assert resp.status_code == 409


def test_create_prompt_422_oversized_content(test_client: TestClient) -> None:
    """POST /api/prompts with content exceeding 32 768 chars → 422."""
    _login(test_client)
    oversized = "x" * 32769
    resp = test_client.post(
        "/api/prompts",
        data={"name": "big", "content": oversized},
    )
    assert resp.status_code == 422


def test_create_prompt_401_unauthed(test_client: TestClient) -> None:
    resp = test_client.post(
        "/api/prompts",
        data={"name": "n", "content": "c"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PATCH /api/prompts/{id}
# ---------------------------------------------------------------------------


def test_update_prompt_200(test_client: TestClient) -> None:
    _login(test_client)
    resp = test_client.patch(
        "/api/prompts/1",
        data={"name": "updated"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "greeting"  # mock returns fixed fixture


def test_update_prompt_404_cross_user(
    test_client: TestClient,
    mock_prompt_svc: MagicMock,
) -> None:
    _login(test_client)
    mock_prompt_svc.update = AsyncMock(side_effect=PromptNotFoundError("not found"))
    resp = test_client.patch("/api/prompts/999", data={"name": "x"})
    assert resp.status_code == 404


def test_update_prompt_409_conflict(
    test_client: TestClient,
    mock_prompt_svc: MagicMock,
) -> None:
    _login(test_client)
    mock_prompt_svc.update = AsyncMock(side_effect=PromptNameConflictError("conflict"))
    resp = test_client.patch("/api/prompts/1", data={"name": "taken"})
    assert resp.status_code == 409


def test_update_prompt_401_unauthed(test_client: TestClient) -> None:
    resp = test_client.patch("/api/prompts/1", data={"name": "x"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/prompts/{id}
# ---------------------------------------------------------------------------


def test_delete_prompt_204(test_client: TestClient) -> None:
    _login(test_client)
    resp = test_client.delete("/api/prompts/1")
    assert resp.status_code == 204


def test_delete_prompt_404_cross_user(
    test_client: TestClient,
    mock_prompt_svc: MagicMock,
) -> None:
    _login(test_client)
    mock_prompt_svc.delete = AsyncMock(side_effect=PromptNotFoundError("not found"))
    resp = test_client.delete("/api/prompts/999")
    assert resp.status_code == 404


def test_delete_prompt_401_unauthed(test_client: TestClient) -> None:
    resp = test_client.delete("/api/prompts/1")
    assert resp.status_code == 401
