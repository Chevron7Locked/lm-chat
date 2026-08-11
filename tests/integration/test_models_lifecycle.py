# SPDX-License-Identifier: Apache-2.0
"""P11b integration tests — model lifecycle HTTP surface.

Tests the full happy path + error paths for:
  POST /api/models/load      — admin-only, form-encoded
  POST /api/models/unload    — admin-only, form-encoded
  POST /api/models/download  — admin-only, form-encoded

Uses the shared ``live_servers`` fixture from conftest.py.
The stub LM Studio lifecycle endpoints (/api/v1/models/load, /unload,
/download) are defined in conftest.py so they are registered before the
session-scoped stub server starts.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.integration.conftest import (
    register_admin_and_login,
    register_and_login,
)


@pytest.mark.asyncio()
async def test_load_model_admin_success(
    client: httpx.AsyncClient,
    db_engine: Any,
    live_servers: dict[str, Any],
) -> None:
    """Admin POST /api/models/load → 200 + {instance_id, model, ...}."""
    admin, cookie = await register_admin_and_login(client, db_engine)
    headers = {"Cookie": f"lmchat_session={cookie}"}

    resp = await client.post(
        "/api/models/load",
        data={"model": "qwen3-8b"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instance_id"] == "qwen3-8b:stub"
    assert body["model"] == "qwen3-8b"
    assert body["status"] == "loaded"
    assert isinstance(body["load_time_seconds"], float)


@pytest.mark.asyncio()
async def test_load_model_non_admin_403(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Non-admin POST /api/models/load → 403."""
    user, cookie = await register_and_login(client)
    headers = {"Cookie": f"lmchat_session={cookie}"}

    resp = await client.post(
        "/api/models/load",
        data={"model": "qwen3-8b"},
        headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio()
async def test_load_model_unauthenticated_401(
    client: httpx.AsyncClient,
) -> None:
    """Unauthenticated POST /api/models/load → 401."""
    resp = await client.post(
        "/api/models/load",
        data={"model": "qwen3-8b"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio()
async def test_unload_model_admin_success(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Admin POST /api/models/unload with instance_id → 200."""
    admin, cookie = await register_admin_and_login(client, db_engine)
    headers = {"Cookie": f"lmchat_session={cookie}"}

    resp = await client.post(
        "/api/models/unload",
        data={"instance_id": "qwen3-8b"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "qwen3-8b" in body["unloaded"]


@pytest.mark.asyncio()
async def test_unload_model_non_admin_403(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Non-admin POST /api/models/unload → 403."""
    user, cookie = await register_and_login(client)
    headers = {"Cookie": f"lmchat_session={cookie}"}

    resp = await client.post(
        "/api/models/unload",
        data={"instance_id": "qwen3-8b"},
        headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio()
async def test_download_model_admin_success(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Admin POST /api/models/download with hub-format model → 200."""
    admin, cookie = await register_admin_and_login(client, db_engine)
    headers = {"Cookie": f"lmchat_session={cookie}"}

    resp = await client.post(
        "/api/models/download",
        data={"model": "bartowski/Phi-3.5-mini-instruct-GGUF/file.gguf"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"


@pytest.mark.asyncio()
async def test_download_model_non_admin_403(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Non-admin POST /api/models/download → 403."""
    user, cookie = await register_and_login(client)
    headers = {"Cookie": f"lmchat_session={cookie}"}

    resp = await client.post(
        "/api/models/download",
        data={"model": "bartowski/Phi-3.5-mini-instruct-GGUF/file.gguf"},
        headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio()
async def test_download_model_unauthenticated_401(
    client: httpx.AsyncClient,
) -> None:
    """Unauthenticated POST /api/models/download → 401."""
    resp = await client.post(
        "/api/models/download",
        data={"model": "bartowski/Phi-3.5-mini-instruct-GGUF/file.gguf"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio()
async def test_load_model_form_encoded_required(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """POST /api/models/load with JSON body → 422 (form-encoded invariant)."""
    admin, cookie = await register_admin_and_login(client, db_engine)
    headers = {"Cookie": f"lmchat_session={cookie}"}

    resp = await client.post(
        "/api/models/load",
        json={"model": "qwen3-8b"},
        headers=headers,
    )
    assert resp.status_code == 422
