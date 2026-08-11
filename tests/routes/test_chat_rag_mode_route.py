# SPDX-License-Identifier: Apache-2.0
"""GET /api/chats/{id}/rag_mode — read-only endpoint.

Read-only surface that the frontend
uses to render the RAG-mode badge (INLINE / HYBRID / FOCUSED) +
display the active project corpus size + threshold.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/rag_mode_route.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv(
        "LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!"
    )
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()
    return create_app()


@pytest.fixture()
def test_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        yield client


def _register_and_login(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        data={"username": "alice", "password": "correct-horse-battery"},
    )
    client.post(
        "/api/auth/login",
        data={"username": "alice", "password": "correct-horse-battery"},
    )


# ─── Tests ────────────────────────────────────────────────────────────────


def test_unprojected_chat_resolves_to_hybrid(
    test_client: TestClient,
) -> None:
    """An un-projected chat (no project_id) → HYBRID."""
    _register_and_login(test_client)
    chat = test_client.post(
        "/api/chats", data={"title": "x"}
    ).json()

    resp = test_client.get(f"/api/chats/{chat['id']}/rag_mode")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "hybrid"
    assert body["focused_document_id"] is None


def test_in_project_empty_corpus_resolves_to_inline(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat in a project with NO documents and a KNOWN context window →
    corpus_tokens=0, INLINE (0 ≤ threshold).

    The badge now resolves the active model's real ctx_window; the test env
    has no model loaded, so pin a known value the way a loaded 131K model
    would report it.
    """
    import lmchat.services.rag_service as _rag_service

    async def _fake_ctx(**_kwargs: Any) -> int:  # noqa: ANN401
        return 131_072

    monkeypatch.setattr(_rag_service, "_resolve_chat_ctx_window", _fake_ctx)

    _register_and_login(test_client)
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    chat = test_client.post(
        f"/api/projects/{proj['id']}/chats", data={"title": "x"}
    ).json()

    resp = test_client.get(f"/api/chats/{chat['id']}/rag_mode")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "inline", body
    assert body["project_corpus_tokens"] == 0


def test_in_project_unknown_ctx_window_resolves_to_hybrid(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the context window is unknown (ctx_window == 0, no model loaded)
    the badge reports HYBRID and skips the corpus estimate — matching
    augment_prompt, which gates INLINE on a known ctx_window rather than a
    hardcoded default."""
    import lmchat.services.rag_service as _rag_service

    async def _fake_ctx(**_kwargs: Any) -> int:  # noqa: ANN401
        return 0

    monkeypatch.setattr(_rag_service, "_resolve_chat_ctx_window", _fake_ctx)

    _register_and_login(test_client)
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    chat = test_client.post(
        f"/api/projects/{proj['id']}/chats", data={"title": "x"}
    ).json()

    resp = test_client.get(f"/api/chats/{chat['id']}/rag_mode")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "hybrid", body
    assert body["project_corpus_tokens"] is None


def test_focused_document_id_short_circuits_to_focused(
    test_client: TestClient,
) -> None:
    """When chats.settings.focused_document_id is set, the resolver
    returns FOCUSED regardless of project state."""
    _register_and_login(test_client)
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    ).json()
    chat = test_client.post(
        f"/api/projects/{proj['id']}/chats", data={"title": "x"}
    ).json()

    # Direct DB write — set focused_document_id on chat.settings.
    import asyncio

    async def _set_focused() -> None:
        from sqlalchemy import update

        from lmchat.db.engine import get_engine
        from lmchat.db.schema import chats as _chats

        eng = get_engine()
        async with eng.begin() as conn:
            await conn.execute(
                update(_chats)
                .where(_chats.c.id == chat["id"])
                .values(settings={"focused_document_id": 42})
            )

    asyncio.run(_set_focused())

    resp = test_client.get(f"/api/chats/{chat['id']}/rag_mode")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "focused"
    assert body["focused_document_id"] == 42


def test_unknown_chat_returns_404(test_client: TestClient) -> None:
    _register_and_login(test_client)
    resp = test_client.get("/api/chats/99999/rag_mode")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_user_chat_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User A's chat is not visible to user B."""
    from lmchat.db.schema import metadata
    from lmchat.db.schema import users as users_table

    app = _make_app(tmp_path, monkeypatch)
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rag_mode_route.db"

    with TestClient(app) as client:
        _register_and_login(client)  # alice — first user, gate open
        a_chat = client.post("/api/chats", data={"title": "alice"}).json()

        # Insert bob directly to bypass the single-admin registration gate.
        eng = create_async_engine(db_url, pool_pre_ping=True)
        try:
            async with eng.begin() as conn:
                await conn.run_sync(metadata.create_all)
                id_result = await conn.execute(
                    select(func.coalesce(func.max(users_table.c.id), 0) + 1)
                )
                next_id = id_result.scalar()
                pw_hash = hash_password("correct-horse-battery", n=_LOW_N, r=8, p=1)
                await conn.execute(
                    text(
                        "INSERT INTO users (id, username, password_hash)"
                        " VALUES (:id, :u, :ph)"
                    ),
                    {"id": int(next_id or 2), "u": "bob", "ph": pw_hash},
                )
        finally:
            await eng.dispose()

        client.cookies.clear()
        client.post(
            "/api/auth/login",
            data={"username": "bob", "password": "correct-horse-battery"},
        )

        resp = client.get(f"/api/chats/{a_chat['id']}/rag_mode")
        assert resp.status_code == 404
