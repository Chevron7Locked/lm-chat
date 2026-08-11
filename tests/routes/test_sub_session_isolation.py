# SPDX-License-Identifier: Apache-2.0
"""Sub-session × Projects v1 isolation contract.

Sub-sessions cannot be projected; the parent chat's project membership is
intentionally irrelevant to the ephemeral sub-session pipeline.
This isolation is a deliberate design decision: sub-sessions are ephemeral
scatchpads and inheriting project context would conflate two distinct scopes.

Tests:

* The `project_id` Form field on `/sub-session/{stream,finalize}`
  is rejected with HTTP 400 + `code=sub_session_not_projectable`.
* The reject fires regardless of whether the parent chat is
  itself in a project (the rejection is on the FIELD existence,
  not on parent state).
"""
from __future__ import annotations

import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import chats, metadata, projects, users
from lmchat.services.auth_service import _reset_dummy_hash_cache


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/sub_session_isolation.db"
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


async def _seed_projected_chat(db_url: str) -> tuple[int, int, int]:
    """Insert a user + project + chat-in-project. Returns
    (user_id, project_id, chat_id)."""
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
        u = await conn.execute(
            insert(users).values(
                username="alice",
                password_hash="dummy",
            )
        )
        pk_u = u.inserted_primary_key
        assert pk_u is not None
        uid = int(pk_u[0])
        now = time.time()
        p = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P",
                description="",
                system_prompt="Project-specific tone instructions.",
                created_at=now,
                updated_at=now,
            )
        )
        pk_p = p.inserted_primary_key
        assert pk_p is not None
        pid = int(pk_p[0])
        c = await conn.execute(
            insert(chats).values(
                user_id=uid, title="t", project_id=pid
            )
        )
        pk_c = c.inserted_primary_key
        assert pk_c is not None
        cid = int(pk_c[0])
    await eng.dispose()
    return uid, pid, cid


# ─── Tests ────────────────────────────────────────────────────────────────


def test_sub_session_stream_rejects_project_id_field(
    test_client: TestClient,
) -> None:
    """A POST to /sub-session/stream carrying ``project_id`` returns
    400 with the expected code, NOT a silent ignore."""
    _register_and_login(test_client)
    chat = test_client.post(
        "/api/chats", data={"title": "t"}
    ).json()
    cid = chat["id"]

    resp = test_client.post(
        f"/api/chats/{cid}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "test prompt",
            "messages_json": '[{"role": "user", "content": "hi"}]',
            "project_id": "42",
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    detail = body.get("detail", {})
    assert isinstance(detail, dict), body
    assert detail.get("code") == "sub_session_not_projectable", body


def test_sub_session_finalize_rejects_project_id_field(
    test_client: TestClient,
) -> None:
    """Same rejection on /sub-session/finalize."""
    _register_and_login(test_client)
    chat = test_client.post(
        "/api/chats", data={"title": "t"}
    ).json()
    cid = chat["id"]

    resp = test_client.post(
        f"/api/chats/{cid}/sub-session/finalize",
        data={
            "model_id": "test-model",
            "system_prompt": "test prompt",
            "messages_json": '[{"role": "user", "content": "hi"}]',
            "project_id": "42",
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    detail = body.get("detail", {})
    assert isinstance(detail, dict), body
    assert detail.get("code") == "sub_session_not_projectable", body


def test_sub_session_stream_accepts_no_project_id(
    test_client: TestClient,
) -> None:
    """The Happy path (no project_id field) still works — verifies
    the new guard hasn't broken normal sub-session flow. We don't
    drive the full stream here (would need a live LM Studio fixture);
    we assert the request gets PAST the project_id check and into
    the ownership check + LM Studio call (which will fail with
    upstream-unreachable but NOT 400-not-projectable)."""
    _register_and_login(test_client)
    chat = test_client.post(
        "/api/chats", data={"title": "t"}
    ).json()
    cid = chat["id"]

    resp = test_client.post(
        f"/api/chats/{cid}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "test prompt",
            "messages_json": '[{"role": "user", "content": "hi"}]',
        },
    )
    # Whatever the upstream LM Studio response is, it MUST NOT be
    # the 400 "sub_session_not_projectable" rejection (that's our
    # negative — the path got past the new guard).
    if resp.status_code == 400:
        body = resp.json()
        detail = body.get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") != "sub_session_not_projectable", (
                "sub-session reject fired even without a project_id field"
            )


def test_sub_session_stream_rejects_project_id_even_when_parent_is_projected(
    test_client: TestClient,
) -> None:
    """The rejection is FIELD-based, not parent-state-based — even
    when the parent chat IS in a project, sending project_id on the
    sub-session still fails with the same code. (The asymmetry per
    §2.1 + §2.2: parent chat keeps inheriting project context via
    streaming_service.py:836-862 on main-chat turns; the sub-session
    stays clean regardless.)"""
    _register_and_login(test_client)
    # Create a project + chat-in-project via the API.
    proj = test_client.post(
        "/api/projects",
        data={"name": "Proj", "description": "", "system_prompt": "X"},
    ).json()
    proj_id = proj["id"]
    chat = test_client.post(
        f"/api/projects/{proj_id}/chats", data={"title": "t"}
    ).json()
    cid = chat["id"]

    resp = test_client.post(
        f"/api/chats/{cid}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "test prompt",
            "messages_json": '[{"role": "user", "content": "hi"}]',
            "project_id": str(proj_id),
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    detail = body.get("detail", {})
    assert isinstance(detail, dict), body
    assert detail.get("code") == "sub_session_not_projectable", body
