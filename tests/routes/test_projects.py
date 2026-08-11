# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the projects CRUD routes.

Covers:
- POST   /api/projects             — create
- GET    /api/projects             — list
- GET    /api/projects/{id}        — fetch + 404 + cross-user-404
- PATCH  /api/projects/{id}        — update + clear= semantics + 404
- DELETE /api/projects/{id}        — delete + 404 + cross-user-404
- Auth: every route returns 401 unauthenticated.
- Cascade: deleting a project flips chat.project_id to NULL.
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

_LOW_N: int = 2**10


def _make_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/projects_route_test.db"
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
    db_url = f"sqlite+aiosqlite:///{tmp_path}/projects_route_test.db"
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


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_list_requires_auth(test_client: TestClient) -> None:
    assert test_client.get("/api/projects").status_code == 401


def test_create_requires_auth(test_client: TestClient) -> None:
    assert (
        test_client.post("/api/projects", data={"name": "X"}).status_code
        == 401
    )


def test_get_requires_auth(test_client: TestClient) -> None:
    assert test_client.get("/api/projects/1").status_code == 401


def test_patch_requires_auth(test_client: TestClient) -> None:
    assert (
        test_client.patch(
            "/api/projects/1", data={"name": "X"}
        ).status_code
        == 401
    )


def test_delete_requires_auth(test_client: TestClient) -> None:
    assert test_client.delete("/api/projects/1").status_code == 401


@pytest.mark.anyio
async def test_get_one_rejects_non_int_path(
    tmp_path: Path, test_client: TestClient
) -> None:
    """A non-int project_id path param must not silently succeed.

    A future router-level conversion change could silently swallow the
    rejection and pass the string downstream where int() would crash with
    500. Pin the behavior by asserting the response is NEVER a 200 with a
    project body. The exact code (422 path-validation vs 401 auth-first)
    depends on FastAPI's dependency-resolution order and isn't load-bearing.
    """
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.get("/api/projects/abc")
    assert resp.status_code in (422, 404), (
        f"expected 422 or 404, got {resp.status_code}: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# POST /api/projects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_minimal(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": "Research"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Research"
    assert body["description"] == ""
    assert body["system_prompt"] == ""
    # folders field removed from the wire shape.
    assert "folders" not in body
    assert isinstance(body["id"], int)


@pytest.mark.anyio
async def test_create_all_fields(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post(
        "/api/projects",
        data={
            "name": "Book",
            "description": "Outline",
            "system_prompt": "Write tightly.",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Book"
    assert body["description"] == "Outline"
    assert body["system_prompt"] == "Write tightly."
    # folders removed from wire.
    assert "folders" not in body


@pytest.mark.anyio
async def test_create_rejects_empty_name(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects", data={"name": ""})
    # FastAPI's Form(...) rejects empty for required-non-empty? Actually
    # Form(...) only requires presence — empty string passes through.
    # ProjectsService catches it as InvalidProjectFieldError → 422.
    assert resp.status_code == 422, resp.text


# The folders feature was removed. The
# ``test_create_rejects_invalid_folders_json`` test was DELETED
# alongside; the route still accepts the form field for older-client
# backward compat, but no longer parses or validates it.


# ---------------------------------------------------------------------------
# GET /api/projects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_empty(tmp_path: Path, test_client: TestClient) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_isolation(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Alice never sees Bob's projects via /api/projects."""
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    test_client.post("/api/projects", data={"name": "A-one"})
    test_client.post("/api/projects", data={"name": "A-two"})
    test_client.cookies.clear()
    _login(test_client, "bob")
    test_client.post("/api/projects", data={"name": "B-one"})
    resp = test_client.get("/api/projects")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert names == {"B-one"}


# ---------------------------------------------------------------------------
# GET /api/projects/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_one_owner_sees(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post(
        "/api/projects", data={"name": "Proj", "description": "D"}
    )
    pid = create.json()["id"]
    resp = test_client.get(f"/api/projects/{pid}")
    assert resp.status_code == 200
    assert resp.json()["description"] == "D"


@pytest.mark.anyio
async def test_get_one_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Bob hitting Alice's project gets a 404 (never leak existence)."""
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "secret"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.get(f"/api/projects/{pid}")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_get_one_404_missing(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.get("/api/projects/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/projects/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_patch_name(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post(
        "/api/projects", data={"name": "Old", "description": "kept"}
    )
    pid = create.json()["id"]
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"name": "New"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "New"
    assert body["description"] == "kept"


@pytest.mark.anyio
async def test_patch_clear_description(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post(
        "/api/projects", data={"name": "Xyz", "description": "remove me"}
    )
    pid = create.json()["id"]
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"clear": "description"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == ""


@pytest.mark.anyio
async def test_patch_clear_rejects_unknown_field(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Xyz"})
    pid = create.json()["id"]
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"clear": "username"}
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_patch_clear_rejects_mixed_valid_and_invalid(
    tmp_path: Path, test_client: TestClient
) -> None:
    """clear=description,bogus returns 422 — no partial clear."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post(
        "/api/projects", data={"name": "Xyz", "description": "kept"}
    )
    pid = create.json()["id"]
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"clear": "description,bogus"}
    )
    assert resp.status_code == 422
    # description was NOT cleared.
    fetched = test_client.get(f"/api/projects/{pid}").json()
    assert fetched["description"] == "kept"


@pytest.mark.anyio
async def test_patch_clear_system_prompt(
    tmp_path: Path, test_client: TestClient
) -> None:
    """clear=system_prompt clears the column to ''."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post(
        "/api/projects",
        data={"name": "Xyz", "system_prompt": "remove me"},
    )
    pid = create.json()["id"]
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"clear": "system_prompt"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["system_prompt"] == ""


@pytest.mark.anyio
async def test_patch_clear_folders_returns_422(
    tmp_path: Path, test_client: TestClient
) -> None:
    """``folders`` is
    no longer a clearable field (the column was dropped in 0023b).
    ``?clear=folders`` falls into the existing ``parse_clear``
    422-unknown-field path. Older clients get a structured error
    instead of a 500."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post(
        "/api/projects", data={"name": "Xyz"}
    )
    pid = create.json()["id"]
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"clear": "folders"}
    )
    assert resp.status_code == 422, resp.text
    assert "folders" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# PATCH /api/projects/{id} — default_model_id / rag_threshold
# (project-settings writer)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_patch_sets_default_model_id(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Proj"})
    pid = create.json()["id"]
    assert create.json()["default_model_id"] is None
    resp = test_client.patch(
        f"/api/projects/{pid}",
        data={"default_model_id": "qwen3.6-35b-a3b"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_model_id"] == "qwen3.6-35b-a3b"
    # GET reflects it too.
    fetched = test_client.get(f"/api/projects/{pid}")
    assert fetched.json()["default_model_id"] == "qwen3.6-35b-a3b"


@pytest.mark.anyio
async def test_patch_clear_default_model_id(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Proj"})
    pid = create.json()["id"]
    test_client.patch(f"/api/projects/{pid}", data={"default_model_id": "x"})
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"clear": "default_model_id"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_model_id"] is None


@pytest.mark.anyio
async def test_patch_sets_rag_threshold(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Proj"})
    pid = create.json()["id"]
    assert create.json()["rag_threshold"] is None
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"rag_threshold": "4096"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rag_threshold"] == 4096
    fetched = test_client.get(f"/api/projects/{pid}")
    assert fetched.json()["rag_threshold"] == 4096


@pytest.mark.anyio
async def test_patch_clear_rag_threshold(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Proj"})
    pid = create.json()["id"]
    test_client.patch(f"/api/projects/{pid}", data={"rag_threshold": "1000"})
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"clear": "rag_threshold"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rag_threshold"] is None


@pytest.mark.anyio
async def test_patch_rejects_negative_rag_threshold(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Proj"})
    pid = create.json()["id"]
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"rag_threshold": "-5"}
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_create_chat_in_project_seeds_model_from_patched_default(
    tmp_path: Path, test_client: TestClient
) -> None:
    """End-to-end via the real writer (no direct-SQL workaround): PATCH
    sets default_model_id, then a new project chat seeds model_id from it."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Proj"})
    pid = create.json()["id"]
    patched = test_client.patch(
        f"/api/projects/{pid}",
        data={"default_model_id": "qwen3.6-35b-a3b"},
    )
    assert patched.status_code == 200, patched.text
    chat = test_client.post(
        f"/api/projects/{pid}/chats", data={"title": "t"}
    )
    assert chat.status_code == 201, chat.text
    assert chat.json().get("model_id") == "qwen3.6-35b-a3b"


@pytest.mark.anyio
async def test_patch_empty_name_is_noop(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH name='' is a no-op — documents the FastAPI Form behavior.

    FastAPI's Form(default=None) with `str | None` typing coerces empty form
    fields to None BEFORE the handler sees them, so the route genuinely cannot
    distinguish "name=''" from "name omitted". Both become "don't touch."
    The contract: name cannot be cleared (required-non-empty per the service);
    to rename, send a non-empty value. This test pins the behavior so a future
    FastAPI / Pydantic upgrade that changes the coercion semantics is loud
    about it.
    """
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post(
        "/api/projects", data={"name": "Original"}
    )
    pid = create.json()["id"]
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"name": ""}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Original"


@pytest.mark.anyio
async def test_patch_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Alice"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.patch(
        f"/api/projects/{pid}", data={"name": "Bobby"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/projects/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_succeeds(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "goner"})
    pid = create.json()["id"]
    resp = test_client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 204
    assert test_client.get(f"/api/projects/{pid}").status_code == 404


@pytest.mark.anyio
async def test_delete_404_missing(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    assert (
        test_client.delete("/api/projects/99999").status_code == 404
    )


@pytest.mark.anyio
async def test_delete_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Alice"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    assert test_client.delete(f"/api/projects/{pid}").status_code == 404
    test_client.cookies.clear()
    _login(test_client, "alice")
    assert (
        test_client.get(f"/api/projects/{pid}").status_code == 200
    )


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/re-embed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_re_embed_requires_auth(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Re-embed route returns 401 for unauthenticated requests."""
    test_client.cookies.clear()
    resp = test_client.post("/api/projects/1/re-embed")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_re_embed_404_missing_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Re-embed route returns 404 for a project that does not exist.

    Exercises the _require_owned_project gate and verifies that
    get_engine_dep(request) resolves without TypeError (the DI bug
    this test was added to catch).
    """
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects/99999/re-embed")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_re_embed_503_no_embedding_model(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Re-embed route returns 503 when no embedding model is loaded.

    Overrides app.state.models_service with a stub that returns [] so
    _resolve_active_embedding_model_id returns None regardless of what is
    actually running in the local environment.  The route must convert that
    to HTTP 503.  Also proves engine/embedding_client/models_service all
    resolve from app.state without TypeError.
    """
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "EmbedTest"})
    assert create.status_code == 201, create.text
    pid = create.json()["id"]

    # Stub out models_service so no embedding model is reported as loaded.
    stub_ms = AsyncMock()
    stub_ms.list_loaded = AsyncMock(return_value=[])
    test_client.app.state.models_service = stub_ms  # type: ignore[attr-defined]

    resp = test_client.post(f"/api/projects/{pid}/re-embed")
    assert resp.status_code == 503, resp.text
    assert "embedding model" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_re_embed_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Re-embed route returns 404 when project belongs to a different user."""
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "AliceProj"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.post(f"/api/projects/{pid}/re-embed")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_re_embed_happy_path(
    tmp_path: Path, test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-embed returns the expected payload when an embedding model is active.

    Patches re_embed_project_documents so the test does not need a live
    embedding server.  The patch target is the name as imported inside the
    route handler (lmchat.services.documents_service.re_embed_project_documents).
    """
    import io as _io  # noqa: F401  (unused here, but keeps import block tidy)
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import patch as _patch

    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "HappyEmbed"})
    assert create.status_code == 201, create.text
    pid = create.json()["id"]

    stub_result = {
        "documents_re_embedded": 0,
        "chunks_re_embedded": 0,
        "active_embedding_model_id": "stub-embed-model",
    }

    # Stub models_service on app.state so get_models_service_dep(request)
    # returns a service that reports one loaded embedding model.
    from lmchat.services.models_service import ModelInfo as _ModelInfo

    stub_ms = _AsyncMock()
    stub_ms.list_loaded = _AsyncMock(
        return_value=[
            _ModelInfo(key="stub-embed-model", type="embedding"),
        ]
    )
    test_client.app.state.models_service = stub_ms  # type: ignore[attr-defined]

    with _patch(
        "lmchat.services.documents_service.re_embed_project_documents",
        new=_AsyncMock(return_value=stub_result),
    ):
        resp = test_client.post(f"/api/projects/{pid}/re-embed")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["documents_re_embedded"] == 0
    assert body["chunks_re_embedded"] == 0
    assert body["active_embedding_model_id"] == "stub-embed-model"


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/documents  (upload shim)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_project_upload_requires_auth(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Project-document upload route returns 401 for unauthenticated requests."""
    import io

    test_client.cookies.clear()
    resp = test_client.post(
        "/api/projects/1/documents",
        files={"file": ("t.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_project_upload_404_missing_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Project-document upload returns 404 for a project that does not exist.

    Exercises the _require_owned_project gate and proves that
    get_engine_dep(request) / get_embedding_client_dep(request) /
    get_models_service_dep(request) all resolve from app.state without
    a TypeError (the DI signature bug this suite was added to catch).
    """
    import io

    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post(
        "/api/projects/99999/documents",
        files={"file": ("t.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_project_upload_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Project-document upload returns 404 for a project owned by another user."""
    import io

    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "AliceDoc"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.post(
        f"/api/projects/{pid}/documents",
        files={"file": ("t.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_project_upload_happy_path(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Project-document upload returns 201 and the expected UploadResponse shape.

    Patches upload_document (the service-layer function) so the test does
    not need a live embedding server or LM Studio.  The shim route delegates
    to upload_document_route which calls upload_document internally; patching
    at the service level keeps the full route stack exercised including the
    engine/embedding_client/models_service DI chain.
    """
    import io
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import patch as _patch

    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "DocProj"})
    assert create.status_code == 201, create.text
    pid = create.json()["id"]

    # Minimal stub that mirrors what upload_document returns (a dataclass/
    # namespace with .id, .title, .chunk_count).
    from types import SimpleNamespace as _NS

    stub_doc = _NS(id=42, title="hello.txt", chunk_count=3)

    with _patch(
        "lmchat.routes.documents.upload_document",
        new=_AsyncMock(return_value=stub_doc),
    ):
        resp = test_client.post(
            f"/api/projects/{pid}/documents",
            files={"file": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == 42
    assert body["filename"] == "hello.txt"
    assert body["chunk_count"] == 3


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/archive + /unarchive
# ---------------------------------------------------------------------------


def test_archive_requires_auth(test_client: TestClient) -> None:
    assert test_client.post("/api/projects/1/archive").status_code == 401


def test_unarchive_requires_auth(test_client: TestClient) -> None:
    assert test_client.post("/api/projects/1/unarchive").status_code == 401


@pytest.mark.anyio
async def test_archive_sets_archived_at_and_excludes_from_default_list(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Stale"})
    pid = create.json()["id"]
    assert create.json()["archived_at"] is None

    resp = test_client.post(f"/api/projects/{pid}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived_at"] is not None

    # Default list (no include_archived) no longer shows it.
    listed = test_client.get("/api/projects")
    assert pid not in {p["id"] for p in listed.json()}

    # But GET-by-id still resolves it (archiving is soft, not delete).
    fetched = test_client.get(f"/api/projects/{pid}")
    assert fetched.status_code == 200
    assert fetched.json()["archived_at"] is not None


@pytest.mark.anyio
async def test_archive_404_missing(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects/99999/archive")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_archive_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Alice"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.post(f"/api/projects/{pid}/archive")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_unarchive_clears_archived_at(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Stale"})
    pid = create.json()["id"]
    test_client.post(f"/api/projects/{pid}/archive")
    resp = test_client.post(f"/api/projects/{pid}/unarchive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived_at"] is None
    listed = test_client.get("/api/projects")
    assert pid in {p["id"] for p in listed.json()}


@pytest.mark.anyio
async def test_list_include_archived_true_includes_archived(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Stale"})
    pid = create.json()["id"]
    test_client.post(f"/api/projects/{pid}/archive")

    default_listed = test_client.get("/api/projects")
    assert pid not in {p["id"] for p in default_listed.json()}

    all_listed = test_client.get("/api/projects?include_archived=true")
    assert pid in {p["id"] for p in all_listed.json()}


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/knowledge-stats
# ---------------------------------------------------------------------------


def test_knowledge_stats_requires_auth(test_client: TestClient) -> None:
    assert (
        test_client.get("/api/projects/1/knowledge-stats").status_code == 401
    )


@pytest.mark.anyio
async def test_knowledge_stats_404_missing(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.get("/api/projects/99999/knowledge-stats")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_knowledge_stats_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Alice"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.get(f"/api/projects/{pid}/knowledge-stats")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_knowledge_stats_empty_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    """An empty project has zero corpus tokens and a positive threshold."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Empty"})
    pid = create.json()["id"]
    resp = test_client.get(f"/api/projects/{pid}/knowledge-stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["corpus_tokens"] == 0
    assert body["threshold"] > 0
    assert body["ctx_window"] > 0


@pytest.mark.anyio
async def test_knowledge_stats_respects_rag_threshold_override(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Proj"})
    pid = create.json()["id"]
    test_client.patch(f"/api/projects/{pid}", data={"rag_threshold": "777"})
    resp = test_client.get(f"/api/projects/{pid}/knowledge-stats")
    assert resp.status_code == 200, resp.text
    assert resp.json()["threshold"] == 777


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/export
# ---------------------------------------------------------------------------


def test_export_requires_auth(test_client: TestClient) -> None:
    assert test_client.get("/api/projects/1/export").status_code == 401


@pytest.mark.anyio
async def test_export_404_missing(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.get("/api/projects/99999/export")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_export_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Alice"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.get(f"/api/projects/{pid}/export")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_export_empty_project_shape(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post(
        "/api/projects",
        data={
            "name": "Backup me",
            "description": "d",
            "system_prompt": "s",
        },
    )
    pid = create.json()["id"]
    resp = test_client.get(f"/api/projects/{pid}/export")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project"]["name"] == "Backup me"
    assert body["project"]["description"] == "d"
    assert body["project"]["system_prompt"] == "s"
    assert body["project"]["embedding_model_id"] is None
    assert body["documents"] == []
    assert body["chats"] == []
    assert "exported_at" in body


@pytest.mark.anyio
async def test_export_includes_chat_messages(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "WithChat"})
    pid = create.json()["id"]
    chat = test_client.post(
        f"/api/projects/{pid}/chats", data={"title": "convo"}
    )
    assert chat.status_code == 201, chat.text
    chat_id = chat.json()["id"]
    msg = test_client.post(
        f"/api/chats/{chat_id}/messages",
        data={"role": "user", "content": "hello there"},
    )
    assert msg.status_code == 201, msg.text

    resp = test_client.get(f"/api/projects/{pid}/export")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["chats"]) == 1
    assert body["chats"][0]["title"] == "convo"
    assert len(body["chats"][0]["messages"]) == 1
    assert body["chats"][0]["messages"][0]["role"] == "user"
    assert body["chats"][0]["messages"][0]["content"] == "hello there"


@pytest.mark.anyio
async def test_export_includes_document_text_not_embeddings(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Documents carry re-extracted chunk text, never embedding vectors."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "WithDoc"})
    pid = create.json()["id"]

    from sqlalchemy import insert, select

    from lmchat.db.schema import document_chunks, documents, users

    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            uid_row = (
                await conn.execute(
                    select(users.c.id).where(users.c.username == "alice")
                )
            ).fetchone()
            uid = int(uid_row[0])  # type: ignore[index]
            dr = await conn.execute(
                insert(documents).values(
                    user_id=uid,
                    project_id=pid,
                    title="notes.txt",
                    sha256="deadbeef",
                    mime_type="text/plain",
                    byte_size=100,
                    chunk_count=1,
                    embedding_model_id="stub-embed-model",
                )
            )
            pk = dr.inserted_primary_key
            assert pk is not None
            did = int(pk[0])
            await conn.execute(
                insert(document_chunks).values(
                    document_id=did,
                    ordinal=0,
                    text="the extracted body text",
                    text_hash="h-0",
                    embedding=b"\x00\x01\x02\x03",
                )
            )
    finally:
        await eng.dispose()

    resp = test_client.get(f"/api/projects/{pid}/export")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["documents"]) == 1
    doc = body["documents"][0]
    assert doc["title"] == "notes.txt"
    assert doc["text"] == "the extracted body text"
    # Never leaks the raw embedding bytes onto the wire.
    assert "embedding" not in doc


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/regenerate-summary
# ---------------------------------------------------------------------------


def test_regenerate_summary_requires_auth(test_client: TestClient) -> None:
    assert (
        test_client.post("/api/projects/1/regenerate-summary").status_code == 401
    )


@pytest.mark.anyio
async def test_regenerate_summary_404_missing(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/projects/99999/regenerate-summary")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_regenerate_summary_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Alice"})
    pid = create.json()["id"]
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.post(f"/api/projects/{pid}/regenerate-summary")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_regenerate_summary_empty_project_is_a_noop(
    tmp_path: Path, test_client: TestClient
) -> None:
    """A project with no chats/messages and no existing summary has
    nothing to gather — real end-to-end wiring (no mocking), the
    "nothing to summarize yet" fail-soft path in
    ``project_summary_service.refresh_project_summary`` returns before
    ever touching the OOB model, so this exercises the route's app.state
    wiring (``lm_streaming_client`` / ``models_service``) for real.
    """
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Empty"})
    pid = create.json()["id"]
    assert create.json()["summary"] == ""
    assert create.json()["summary_updated_at"] is None

    resp = test_client.post(f"/api/projects/{pid}/regenerate-summary")
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"] == ""
    assert resp.json()["summary_updated_at"] is None


@pytest.mark.anyio
async def test_regenerate_summary_returns_the_new_summary_shape(
    tmp_path: Path,
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route surfaces whatever ``refresh_project_summary`` persists.

    Stubs the service-level entrypoint (not the OOB call underneath it —
    that's covered at the service layer in
    ``tests/services/test_project_summary_service.py``) so this test
    stays focused on the route's own contract: it calls through with the
    right args and echoes ``{summary, summary_updated_at}`` back.
    """
    import lmchat.services.project_summary_service as pss
    from lmchat.services.projects_service import Project

    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    create = test_client.post("/api/projects", data={"name": "Proj"})
    pid = create.json()["id"]

    captured: dict[str, Any] = {}

    async def _fake_refresh(**kwargs: Any) -> Project:
        captured.update(kwargs)
        return Project(
            id=pid,
            user_id=kwargs["user_id"],
            name="P",
            description="",
            system_prompt="",
            embedding_model_id=None,
            default_model_id=None,
            rag_threshold=None,
            created_at=0.0,
            updated_at=0.0,
            summary="A fresh rolling summary.",
            summary_updated_at=12345.0,
            summary_message_watermark=4,
        )

    monkeypatch.setattr(pss, "refresh_project_summary", _fake_refresh)

    resp = test_client.post(f"/api/projects/{pid}/regenerate-summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"] == "A fresh rolling summary."
    assert body["summary_updated_at"] == 12345.0
    assert captured["project_id"] == pid
