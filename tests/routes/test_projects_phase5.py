# SPDX-License-Identifier: Apache-2.0
"""Project-scoped writes + ownership invariants.

Covers:
- POST /api/projects/{id}/chats — create chat in project
- PATCH /api/chats/{id}      — move chat between projects + clear=
- PATCH /api/documents/{id}  — move document between projects + clear=
- POST /api/documents?project_id=X — upload doc straight into project
- Ownership check on every project-touching write.
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

    db_url = f"sqlite+aiosqlite:///{tmp_path}/projects_p5.db"
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
def _reset(monkeypatch: pytest.MonkeyPatch):
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
    db_url = f"sqlite+aiosqlite:///{tmp_path}/projects_p5.db"
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
# POST /api/projects/{id}/chats
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_chat_in_project_sets_project_id(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    pid = int(proj["id"])
    resp = test_client.post(
        f"/api/projects/{pid}/chats", data={"title": "first"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "first"
    assert body["project_id"] == pid


@pytest.mark.anyio
async def test_get_chat_detail_reports_project_id(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/chats/{id} reports project_id for a project-linked chat.

    The chat-detail response (ChatWithMessagesResponse) previously omitted
    project_id, so a chat created inside a project reported project_id: null
    even though it inherited the project's system prompt and showed in the
    sidebar.
    """
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    pid = int(test_client.post("/api/projects", data={"name": "Proj"}).json()["id"])
    chat_id = int(
        test_client.post(
            f"/api/projects/{pid}/chats", data={"title": "first"}
        ).json()["id"]
    )

    detail = test_client.get(f"/api/chats/{chat_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == pid

    # A non-project chat still reports null (no false linkage).
    plain_id = int(
        test_client.post("/api/chats", data={"title": "plain"}).json()["id"]
    )
    plain_detail = test_client.get(f"/api/chats/{plain_id}")
    assert plain_detail.status_code == 200, plain_detail.text
    assert plain_detail.json()["project_id"] is None


@pytest.mark.anyio
async def test_create_chat_in_project_404_unknown(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post(
        "/api/projects/99999/chats", data={"title": "x"}
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_create_chat_in_project_404_cross_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    proj = test_client.post(
        "/api/projects", data={"name": "alices"}
    ).json()
    pid = int(proj["id"])
    test_client.cookies.clear()
    _login(test_client, "bob")
    resp = test_client.post(
        f"/api/projects/{pid}/chats", data={"title": "x"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/chats/{id} — move + clear=project_id
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_patch_chat_moves_into_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    chat = test_client.post(
        "/api/chats", data={"title": "un"}
    ).json()
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    resp = test_client.patch(
        f"/api/chats/{chat['id']}",
        data={"project_id": str(proj["id"])},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == proj["id"]


@pytest.mark.anyio
async def test_patch_chat_clear_project_id_detaches(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    chat = test_client.post(
        f"/api/projects/{proj['id']}/chats",
        data={"title": "in"},
    ).json()
    assert chat["project_id"] == proj["id"]
    resp = test_client.patch(
        f"/api/chats/{chat['id']}",
        data={"clear": "project_id"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] is None


@pytest.mark.anyio
async def test_patch_chat_move_404_unknown_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    chat = test_client.post("/api/chats", data={"title": "x"}).json()
    resp = test_client.patch(
        f"/api/chats/{chat['id']}",
        data={"project_id": "99999"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_patch_chat_move_404_cross_user_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    a_proj = test_client.post(
        "/api/projects", data={"name": "alices"}
    ).json()
    test_client.cookies.clear()
    _login(test_client, "bob")
    chat = test_client.post(
        "/api/chats", data={"title": "bob"}
    ).json()
    resp = test_client.patch(
        f"/api/chats/{chat['id']}",
        data={"project_id": str(a_proj["id"])},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_patch_chat_clear_rejects_unknown_field(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    chat = test_client.post("/api/chats", data={"title": "x"}).json()
    resp = test_client.patch(
        f"/api/chats/{chat['id']}",
        data={"clear": "title"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /api/documents/{id} — move + clear=project_id
# ---------------------------------------------------------------------------


async def _insert_document_directly(
    eng: AsyncEngine, *, user_id: int, title: str
) -> int:
    """Bypass the upload pipeline (mime detection) — write a minimal row."""
    async with eng.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO documents (user_id, title, mime_type, "
                "byte_size, chunk_count, embedding_model_id, sha256, "
                "deleted_at) VALUES (:u, :t, 'text/plain', 1, 0, '', "
                ":s, NULL)"
            ),
            {"u": user_id, "t": title, "s": f"sha-{title}"},
        )
        return int(result.lastrowid)  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_patch_document_moves_into_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    uid = await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    eng = await _engine_for(tmp_path)
    try:
        doc_id = await _insert_document_directly(
            eng, user_id=uid, title="d"
        )
    finally:
        await eng.dispose()
    resp = test_client.patch(
        f"/api/documents/{doc_id}",
        data={"project_id": str(proj["id"])},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == proj["id"]


@pytest.mark.anyio
async def test_patch_document_clear_detaches(
    tmp_path: Path, test_client: TestClient
) -> None:
    uid = await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    eng = await _engine_for(tmp_path)
    try:
        doc_id = await _insert_document_directly(
            eng, user_id=uid, title="d"
        )
    finally:
        await eng.dispose()
    test_client.patch(
        f"/api/documents/{doc_id}",
        data={"project_id": str(proj["id"])},
    )
    resp = test_client.patch(
        f"/api/documents/{doc_id}",
        data={"clear": "project_id"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] is None


@pytest.mark.anyio
async def test_patch_document_404_unknown_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    uid = await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    eng = await _engine_for(tmp_path)
    try:
        doc_id = await _insert_document_directly(
            eng, user_id=uid, title="d"
        )
    finally:
        await eng.dispose()
    resp = test_client.patch(
        f"/api/documents/{doc_id}",
        data={"project_id": "99999"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_patch_document_404_cross_user_project(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "alice")
    uid_b = await _insert_user(tmp_path, "bob")
    _login(test_client, "alice")
    a_proj = test_client.post(
        "/api/projects", data={"name": "alices"}
    ).json()
    test_client.cookies.clear()
    _login(test_client, "bob")
    eng = await _engine_for(tmp_path)
    try:
        doc_id = await _insert_document_directly(
            eng, user_id=uid_b, title="b-doc"
        )
    finally:
        await eng.dispose()
    resp = test_client.patch(
        f"/api/documents/{doc_id}",
        data={"project_id": str(a_proj["id"])},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_patch_document_clear_rejects_unknown_field(
    tmp_path: Path, test_client: TestClient
) -> None:
    uid = await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    eng = await _engine_for(tmp_path)
    try:
        doc_id = await _insert_document_directly(
            eng, user_id=uid, title="d"
        )
    finally:
        await eng.dispose()
    resp = test_client.patch(
        f"/api/documents/{doc_id}",
        data={"clear": "title"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/documents?project_id=X — upload straight into project
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upload_with_project_id_404_unknown(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Upload with foreign project_id 404s BEFORE the body is read."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.post(
        "/api/documents",
        params={"project_id": 99999},
        files={"file": ("hello.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 404
