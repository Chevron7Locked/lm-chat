# SPDX-License-Identifier: Apache-2.0
"""POST /api/chats/{chat_id}/promote-to-project.

Turns an existing chat into a new project, carrying selected documents.
The heavy per-chat data (messages, compactions, message_embeddings) is
``chat_id``-scoped and travels for free once ``chats.project_id`` is set;
this route's real job is the FK flips (chat + documents) plus the
embedding-model pin decision for any documents moved along.

Pattern: real app (``create_app()``) against a per-test SQLite file, real
services wired by the lifespan — mirrors
``tests/routes/test_create_chat_in_project_model_seed.py`` and
``tests/routes/test_projects_phase5.py``. Documents are inserted directly
(bypassing the upload pipeline) so the embedding-model-pin guard can be
exercised without a real LM Studio round trip.
"""
from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """Build the app with per-test DB isolation."""
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/promote_to_project.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
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


def _register_and_login(client: TestClient, username: str = "alice") -> None:
    client.post(
        "/api/auth/register",
        data={"username": username, "password": "correct-horse-battery"},
    )
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"


async def _insert_user_direct(username: str) -> None:
    """Bypass the single-admin registration gate — insert a second user
    directly (mirrors ``tests/routes/test_chats.py::_insert_user_direct``).
    """
    from sqlalchemy import func, select, text

    from lmchat.db.engine import get_engine
    from lmchat.db.schema import users

    pw_hash = hash_password("correct-horse-battery", n=_LOW_N, r=8, p=1)
    eng = get_engine()
    async with eng.begin() as conn:
        next_id = (
            await conn.execute(select(func.coalesce(func.max(users.c.id), 0) + 1))
        ).scalar()
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": int(next_id or 1), "u": username, "ph": pw_hash},
        )


def _current_user_id(client: TestClient) -> int:
    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    return int(me.json()["user_id"])


async def _insert_document(
    *,
    user_id: int,
    title: str,
    embedding_model_id: str = "",
    project_id: int | None = None,
) -> int:
    """Bypass the upload pipeline — write a minimal ``documents`` row
    directly (mirrors ``test_projects_phase5.py::_insert_document_directly``,
    extended with ``embedding_model_id`` + ``project_id`` so the
    embedding-pin and already-projected guards can be exercised without a
    real LM Studio embedding round trip).
    """
    from sqlalchemy import insert

    from lmchat.db.engine import get_engine
    from lmchat.db.schema import documents

    eng = get_engine()
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(documents).values(
                user_id=user_id,
                title=title,
                mime_type="text/plain",
                byte_size=1,
                chunk_count=1,
                embedding_model_id=embedding_model_id,
                sha256=f"sha-{title}",
                project_id=project_id,
            )
        )
        pk = result.inserted_primary_key
        assert pk is not None
        return int(pk[0])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_promote_creates_project_and_moves_chat(test_client: TestClient) -> None:
    _register_and_login(test_client)
    chat = test_client.post("/api/chats", data={"title": "My Research Chat"}).json()

    resp = test_client.post(f"/api/chats/{chat['id']}/promote-to-project")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "My Research Chat"
    assert body["moved_document_count"] == 0
    assert body["embedding_model_id"] is None

    chat_after = test_client.get(f"/api/chats/{chat['id']}").json()
    assert chat_after["project_id"] == body["id"]


def test_promote_with_explicit_name_and_system_prompt(
    test_client: TestClient,
) -> None:
    _register_and_login(test_client)
    chat = test_client.post("/api/chats", data={"title": "x"}).json()

    resp = test_client.post(
        f"/api/chats/{chat['id']}/promote-to-project",
        data={"name": "Custom Name", "system_prompt": "Be terse."},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Custom Name"
    assert body["system_prompt"] == "Be terse."


def test_promote_moves_selected_documents_and_pins_embedding_model(
    test_client: TestClient,
) -> None:
    _register_and_login(test_client)
    uid = _current_user_id(test_client)
    chat = test_client.post("/api/chats", data={"title": "Docs Chat"}).json()

    doc_a = asyncio.run(
        _insert_document(user_id=uid, title="a", embedding_model_id="modelA")
    )
    doc_b = asyncio.run(
        _insert_document(user_id=uid, title="b", embedding_model_id="modelA")
    )

    resp = test_client.post(
        f"/api/chats/{chat['id']}/promote-to-project",
        data={"document_ids": f"{doc_a},{doc_b}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["moved_document_count"] == 2
    assert body["embedding_model_id"] == "modelA"

    docs = {d["id"]: d for d in test_client.get("/api/documents").json()}
    assert docs[doc_a]["project_id"] == body["id"]
    assert docs[doc_b]["project_id"] == body["id"]


def test_promote_ignores_unembedded_docs_in_span_check(
    test_client: TestClient,
) -> None:
    """A document still mid-chunking (``embedding_model_id == ""``) doesn't
    constrain the pin decision — only docs that HAVE a model participate."""
    _register_and_login(test_client)
    uid = _current_user_id(test_client)
    chat = test_client.post("/api/chats", data={"title": "Docs Chat"}).json()

    doc_pinned = asyncio.run(
        _insert_document(user_id=uid, title="a", embedding_model_id="modelA")
    )
    doc_unembedded = asyncio.run(
        _insert_document(user_id=uid, title="b", embedding_model_id="")
    )

    resp = test_client.post(
        f"/api/chats/{chat['id']}/promote-to-project",
        data={"document_ids": f"{doc_pinned},{doc_unembedded}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["moved_document_count"] == 2
    assert body["embedding_model_id"] == "modelA"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_promote_unknown_chat_404(test_client: TestClient) -> None:
    _register_and_login(test_client)
    resp = test_client.post("/api/chats/99999/promote-to-project")
    assert resp.status_code == 404


def test_promote_chat_owned_by_another_user_404(test_client: TestClient) -> None:
    _register_and_login(test_client, "alice")
    chat = test_client.post("/api/chats", data={"title": "alice chat"}).json()

    asyncio.run(_insert_user_direct("mallory"))
    _register_and_login(test_client, "mallory")

    resp = test_client.post(f"/api/chats/{chat['id']}/promote-to-project")
    assert resp.status_code == 404


def test_promote_already_in_project_409(test_client: TestClient) -> None:
    _register_and_login(test_client)
    proj = test_client.post("/api/projects", data={"name": "Existing"}).json()
    chat = test_client.post("/api/chats", data={"title": "x"}).json()
    test_client.patch(
        f"/api/chats/{chat['id']}", data={"project_id": str(proj["id"])}
    )

    resp = test_client.post(f"/api/chats/{chat['id']}/promote-to-project")
    assert resp.status_code == 409


def test_promote_incognito_chat_422(test_client: TestClient) -> None:
    _register_and_login(test_client)
    chat = test_client.post(
        "/api/chats", data={"title": "secret", "incognito": "true"}
    ).json()

    resp = test_client.post(f"/api/chats/{chat['id']}/promote-to-project")
    assert resp.status_code == 422


def test_promote_multi_embedding_model_conflict_409(
    test_client: TestClient,
) -> None:
    _register_and_login(test_client)
    uid = _current_user_id(test_client)
    chat = test_client.post("/api/chats", data={"title": "x"}).json()

    doc_a = asyncio.run(
        _insert_document(user_id=uid, title="a", embedding_model_id="modelA")
    )
    doc_b = asyncio.run(
        _insert_document(user_id=uid, title="b", embedding_model_id="modelB")
    )

    resp = test_client.post(
        f"/api/chats/{chat['id']}/promote-to-project",
        data={"document_ids": f"{doc_a},{doc_b}"},
    )
    assert resp.status_code == 409

    # Fail-closed: rejecting on the embedding-model span check must not
    # create the project OR move the chat.
    chat_after = test_client.get(f"/api/chats/{chat['id']}").json()
    assert chat_after["project_id"] is None
    assert test_client.get("/api/projects").json() == []


def test_promote_document_already_in_another_project_409(
    test_client: TestClient,
) -> None:
    _register_and_login(test_client)
    uid = _current_user_id(test_client)
    other_proj = test_client.post("/api/projects", data={"name": "Other"}).json()
    chat = test_client.post("/api/chats", data={"title": "x"}).json()

    doc_id = asyncio.run(
        _insert_document(user_id=uid, title="a", project_id=int(other_proj["id"]))
    )

    resp = test_client.post(
        f"/api/chats/{chat['id']}/promote-to-project",
        data={"document_ids": str(doc_id)},
    )
    assert resp.status_code == 409


def test_promote_document_owned_by_another_user_rejected(
    test_client: TestClient,
) -> None:
    """A document owned by someone else 404s — never leak existence,
    never silently steal it."""
    _register_and_login(test_client, "alice")
    chat = test_client.post("/api/chats", data={"title": "x"}).json()

    asyncio.run(_insert_user_direct("mallory"))
    _register_and_login(test_client, "mallory")
    mallory_id = _current_user_id(test_client)
    doc_id = asyncio.run(_insert_document(user_id=mallory_id, title="secret"))

    _register_and_login(test_client, "alice")
    resp = test_client.post(
        f"/api/chats/{chat['id']}/promote-to-project",
        data={"document_ids": str(doc_id)},
    )
    assert resp.status_code == 404


def test_promote_bad_document_ids_token_422(test_client: TestClient) -> None:
    _register_and_login(test_client)
    chat = test_client.post("/api/chats", data={"title": "x"}).json()

    resp = test_client.post(
        f"/api/chats/{chat['id']}/promote-to-project",
        data={"document_ids": "1,not-a-number"},
    )
    assert resp.status_code == 422
