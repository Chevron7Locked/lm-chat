# SPDX-License-Identifier: Apache-2.0
"""POST /api/projects/{id}/chats seeds chats.model_id from default_model_id.

End-to-end route-level: when a
project has ``default_model_id`` pinned, creating a chat in it seeds
the new chat's ``model_id`` from the pin. NULL falls through to the
legacy NULL-on-insert behavior (user's global default resolved at
stream time).
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lmchat.services.auth_service import _reset_dummy_hash_cache


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """Build the app with per-test DB isolation."""
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/b2_seed_route.db"
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


async def _set_default_model(project_id: int, model_id: str) -> None:
    """Direct DB write — projects route doesn't accept default_model_id
    in form input yet (that's added by a later UI). For the seed contract
    test we exercise the SEED path: stamp the pin via SQL, then create
    a chat via the route, then assert."""
    from sqlalchemy import update

    from lmchat.db.engine import get_engine
    from lmchat.db.schema import projects

    eng = get_engine()
    async with eng.begin() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == project_id)
            .values(default_model_id=model_id)
        )


def test_create_chat_in_project_seeds_model_id_from_pin(
    test_client: TestClient,
) -> None:
    """Project has default_model_id=qwen3.6-35b-a3b → new chat
    carries that model_id from the create response."""
    import asyncio

    _register_and_login(test_client)
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    )
    assert proj.status_code == 201
    pid = int(proj.json()["id"])

    asyncio.run(_set_default_model(pid, "qwen3.6-35b-a3b"))

    chat = test_client.post(
        f"/api/projects/{pid}/chats", data={"title": "t"}
    )
    assert chat.status_code == 201, chat.text
    body = chat.json()
    # ChatResponse round-trip carries model_id.
    assert body.get("model_id") == "qwen3.6-35b-a3b", (
        f"model_id seed failed; got: {body}"
    )


def test_create_chat_in_project_without_pin_leaves_model_id_null(
    test_client: TestClient,
) -> None:
    """default_model_id NULL → new chat.model_id is None (legacy
    behavior preserved; user's global default resolved at stream time)."""
    _register_and_login(test_client)
    proj = test_client.post(
        "/api/projects", data={"name": "Proj"}
    )
    pid = int(proj.json()["id"])

    # No default_model_id stamp.

    chat = test_client.post(
        f"/api/projects/{pid}/chats", data={"title": "t"}
    )
    assert chat.status_code == 201, chat.text
    body = chat.json()
    assert body.get("model_id") in (None, ""), (
        f"model_id should be NULL/empty without pin; got: {body}"
    )



def test_create_chat_in_project_blank_title_defaults_not_422(
    test_client: TestClient,
) -> None:
    """Regression: a blank/absent title from the
    project "New chat" box previously 422'd with a raw "Unprocessable Content"
    toast. It must now create an untitled chat ("New Chat"), matching the main
    POST /api/chats flow.

    RED-ON-REVERT: restore ``title: str = Form(...)`` and the no-title POST 422s.
    """
    _register_and_login(test_client)
    proj = test_client.post("/api/projects", data={"name": "Proj"})
    pid = int(proj.json()["id"])

    # (a) No title field at all.
    chat = test_client.post(f"/api/projects/{pid}/chats", data={})
    assert chat.status_code == 201, chat.text
    assert chat.json().get("title") == "New Chat", chat.json()

    # (b) Whitespace-only title also defaults rather than persisting blank.
    chat2 = test_client.post(f"/api/projects/{pid}/chats", data={"title": "   "})
    assert chat2.status_code == 201, chat2.text
    assert chat2.json().get("title") == "New Chat", chat2.json()
