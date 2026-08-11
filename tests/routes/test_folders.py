# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the folder catalogue routes.

Tests:
- GET    /api/folders          — 401 unauth / 200 union of prefs + chats
- POST   /api/folders          — 201 create; 400 invalid name; idempotent
- PATCH  /api/folders/{name}   — rename atomically migrates chats.folder
- DELETE /api/folders/{name}   — unfoldering chats; idempotent
- Cross-user isolation: alice's folders are invisible to bob.
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
from tests.routes.conftest import encode_path_param_for_testclient

_LOW_N: int = 2**10


def _make_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/folders_route_test.db"
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
    db_url = f"sqlite+aiosqlite:///{tmp_path}/folders_route_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(
    tmp_path: Path,
    username: str,
    is_admin: bool = False,
) -> int:
    pw_hash = hash_password("test-pw", n=_LOW_N, r=8, p=1)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO users (username, password_hash, is_admin) "
                    "VALUES (:u, :pw, :admin)"
                ),
                {"u": username, "pw": pw_hash, "admin": 1 if is_admin else 0},
            )
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE username = :u"), {"u": username}
                )
            ).fetchone()
            return int(row[0])  # type: ignore[index]
    finally:
        await eng.dispose()


async def _insert_chat(
    tmp_path: Path,
    *,
    user_id: int,
    title: str,
    folder: str | None,
) -> int:
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO chats (user_id, title, folder) "
                    "VALUES (:u, :t, :f)"
                ),
                {"u": user_id, "t": title, "f": folder},
            )
            return int(result.lastrowid)  # type: ignore[union-attr]
    finally:
        await eng.dispose()


def _login(client: TestClient, username: str) -> None:
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": "test-pw"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# GET /api/folders
# ---------------------------------------------------------------------------


def test_list_folders_requires_auth(test_client: TestClient) -> None:
    resp = test_client.get("/api/folders")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_list_folders_empty(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.get("/api/folders")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_folders_unions_chats_and_prefs(
    tmp_path: Path, test_client: TestClient
) -> None:
    """List = DISTINCT chats.folder ∪ user_prefs.folders."""
    uid = await _insert_user(tmp_path, "alice")
    await _insert_chat(tmp_path, user_id=uid, title="c1", folder="Work")
    await _insert_chat(tmp_path, user_id=uid, title="c2", folder="Personal")
    _login(test_client, "alice")

    # Add an empty folder via prefs.
    resp = test_client.post("/api/folders", data={"name": "Archive"})
    assert resp.status_code == 201
    assert resp.json() == ["Archive", "Personal", "Work"]

    # Listing returns merged sorted result.
    resp = test_client.get("/api/folders")
    assert resp.status_code == 200
    assert resp.json() == ["Archive", "Personal", "Work"]


# ---------------------------------------------------------------------------
# POST /api/folders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_add_folder_creates_empty_bucket(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/folders", data={"name": "Drafts"})
    assert resp.status_code == 201
    assert resp.json() == ["Drafts"]


@pytest.mark.anyio
async def test_add_folder_is_idempotent(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    test_client.post("/api/folders", data={"name": "Drafts"})
    resp = test_client.post("/api/folders", data={"name": "Drafts"})
    assert resp.status_code == 201
    assert resp.json() == ["Drafts"]


@pytest.mark.anyio
async def test_add_folder_rejects_empty_name(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/folders", data={"name": "   "})
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_add_folder_rejects_long_name(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post("/api/folders", data={"name": "x" * 200})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PATCH /api/folders/{name}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rename_folder_migrates_chats(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Rename moves both prefs entries and chats.folder values atomically."""
    uid = await _insert_user(tmp_path, "alice")
    await _insert_chat(tmp_path, user_id=uid, title="c1", folder="Old")
    await _insert_chat(tmp_path, user_id=uid, title="c2", folder="Old")
    _login(test_client, "alice")

    resp = test_client.patch("/api/folders/Old", data={"new_name": "New"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == ["New"]

    # GET /api/chats reflects the rename.
    chats_resp = test_client.get("/api/chats")
    assert chats_resp.status_code == 200
    folders = {c["folder"] for c in chats_resp.json()}
    assert folders == {"New"}


@pytest.mark.anyio
async def test_rename_folder_validates_new_name(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    # Empty body content triggers FastAPI's Form-validation 422 before our
    # service runs; whitespace-only is parsed but rejected by the service
    # with our explicit 400.
    resp = test_client.patch("/api/folders/Old", data={"new_name": "   "})
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_rename_folder_does_not_double_decode_path(
    tmp_path: Path, test_client: TestClient
) -> None:
    """FastAPI's ``{folder_name:path}`` converter already URL-decodes the
    path segment once. A second ``unquote()`` on top of that
    mis-targets any folder whose real name itself contains a
    percent-encoded-looking sequence — e.g. a folder literally named
    ``A%2Fb``. A real ASGI server (uvicorn) decodes the wire path
    exactly once, so a client sending the name correctly percent-
    encoded once (``A%252Fb``) has it arrive at the route as the
    literal ``A%2Fb``; an extra app-level unquote() turns that into
    ``A/b``, a completely different (nonexistent) folder, leaving the
    real one silently untouched.

    ``starlette.testclient.TestClient`` itself decodes the path an
    EXTRA time when building its ASGI scope (``scope["path"] =
    unquote(request.url.path)``, where ``request.url.path`` is already
    httpx's own one-level-decoded view) — a quirk of the test
    transport, not present with a real ASGI server. The URL sent here
    is therefore encoded one level deeper than a real HTTP client would
    send, via ``encode_path_param_for_testclient`` (see its docstring
    in ``tests/routes/conftest.py``), purely to compensate for that
    extra test-transport decode and land the route on the same value
    (``A%2Fb``) production code actually sees.
    """
    real_name = "A%2Fb"
    uid = await _insert_user(tmp_path, "alice")
    await _insert_chat(tmp_path, user_id=uid, title="c1", folder=real_name)
    _login(test_client, "alice")

    resp = test_client.patch(
        f"/api/folders/{encode_path_param_for_testclient(real_name)}",
        data={"new_name": "Renamed"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == ["Renamed"]

    chats_resp = test_client.get("/api/chats")
    assert chats_resp.status_code == 200
    folders = {c["folder"] for c in chats_resp.json()}
    assert folders == {"Renamed"}


@pytest.mark.anyio
async def test_delete_folder_does_not_double_decode_path(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Same double-decode bug as rename, exercised via DELETE.

    See ``test_rename_folder_does_not_double_decode_path`` for why the
    URL is encoded one level deeper than a real client would send.
    """
    real_name = "A%2Fb"
    uid = await _insert_user(tmp_path, "alice")
    chat_id = await _insert_chat(
        tmp_path, user_id=uid, title="c1", folder=real_name
    )
    _login(test_client, "alice")

    resp = test_client.delete(
        f"/api/folders/{encode_path_param_for_testclient(real_name)}"
    )
    assert resp.status_code == 200, resp.text

    chats_resp = test_client.get("/api/chats")
    found = [c for c in chats_resp.json() if c["id"] == chat_id]
    assert len(found) == 1
    assert found[0]["folder"] is None


@pytest.mark.anyio
async def test_rename_folder_collision_returns_409(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Rename to an existing folder name returns 409.

    The collision guard prevents silent merging of two folders into one.
    """
    uid = await _insert_user(tmp_path, "alice")
    await _insert_chat(tmp_path, user_id=uid, title="c1", folder="Personal")
    await _insert_chat(tmp_path, user_id=uid, title="c2", folder="Work")
    _login(test_client, "alice")

    resp = test_client.patch("/api/folders/Personal", data={"new_name": "Work"})
    assert resp.status_code == 409, resp.text
    assert "already exists" in resp.json()["detail"]

    # Confirm no destructive side effect: both folders + both chats survive.
    resp_list = test_client.get("/api/folders")
    assert resp_list.status_code == 200
    folders = resp_list.json()
    assert "Personal" in folders
    assert "Work" in folders


# ---------------------------------------------------------------------------
# DELETE /api/folders/{name}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_folder_unfolders_chats(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Delete sets chats.folder=NULL for matching chats; the chat survives."""
    uid = await _insert_user(tmp_path, "alice")
    chat_id = await _insert_chat(
        tmp_path, user_id=uid, title="c1", folder="Archive"
    )
    _login(test_client, "alice")
    test_client.post("/api/folders", data={"name": "Archive"})

    resp = test_client.delete("/api/folders/Archive")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    # Chat survives, folder is now NULL.
    chats_resp = test_client.get("/api/chats")
    assert chats_resp.status_code == 200
    found = [c for c in chats_resp.json() if c["id"] == chat_id]
    assert len(found) == 1
    assert found[0]["folder"] is None


@pytest.mark.anyio
async def test_delete_folder_is_idempotent(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.delete("/api/folders/Nonexistent")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_folder_catalogue_is_per_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alice's prefs.folders are invisible to bob; chats.folder is per-user."""
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        alice_id = await _insert_user(tmp_path, "alice")
        await _insert_user(tmp_path, "bob")
        await _insert_chat(
            tmp_path, user_id=alice_id, title="alice's", folder="Secret"
        )

        _login(client, "alice")
        client.post("/api/folders", data={"name": "Alice-only"})

        # Switch to bob — log out, log in.
        client.post("/api/auth/logout")
        _login(client, "bob")

        bob_resp = client.get("/api/folders")
        assert bob_resp.status_code == 200
        # Bob sees nothing.
        assert bob_resp.json() == []


# ---------------------------------------------------------------------------
# Per-project folders REMOVED.
# All 4 folder routes return 410 GONE with body containing the
# ``FOLDER_API_REMOVED_FOR_PROJECTS`` sentinel code when called with
# ``project_id``.
# ---------------------------------------------------------------------------


async def _create_project_for(
    client: TestClient, name: str = "Proj"
) -> int:
    resp = client.post("/api/projects", data={"name": name})
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


@pytest.mark.anyio
async def test_list_folders_with_project_id_returns_410(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    pid = await _create_project_for(test_client, "Proj")
    resp = test_client.get(f"/api/folders?project_id={pid}")
    assert resp.status_code == 410, resp.text
    detail = resp.json().get("detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "FOLDER_API_REMOVED_FOR_PROJECTS"


@pytest.mark.anyio
async def test_add_folder_with_project_id_returns_410(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    pid = await _create_project_for(test_client, "Proj")
    resp = test_client.post(
        f"/api/folders?project_id={pid}", data={"name": "X"}
    )
    assert resp.status_code == 410
    detail = resp.json().get("detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "FOLDER_API_REMOVED_FOR_PROJECTS"


@pytest.mark.anyio
async def test_rename_folder_with_project_id_returns_410(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    pid = await _create_project_for(test_client, "Proj")
    resp = test_client.patch(
        f"/api/folders/X?project_id={pid}", data={"new_name": "Y"}
    )
    assert resp.status_code == 410
    detail = resp.json().get("detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "FOLDER_API_REMOVED_FOR_PROJECTS"


@pytest.mark.anyio
async def test_delete_folder_with_project_id_returns_410(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    pid = await _create_project_for(test_client, "Proj")
    resp = test_client.delete(f"/api/folders/X?project_id={pid}")
    assert resp.status_code == 410
    detail = resp.json().get("detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "FOLDER_API_REMOVED_FOR_PROJECTS"
