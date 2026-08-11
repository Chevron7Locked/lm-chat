# SPDX-License-Identifier: Apache-2.0
"""P13h — integration tests for the per-request MCP integrations override.

Scope
-----
P13h ships a per-request MCP server picker on top of P12e's admin-supplied
integrations list.  The wire field carrying the subset chosen
by the user for the next message is ``CanonicalChatRequest.integrations``;
the admin's per-row ``enabled_by_default`` flag seeds the composer's
initial chip-row state.

The two backend invariants exercised here:

1. **Override narrows the LM Studio request.**  When the chat-send payload
   carries an explicit ``integrations`` array, that exact array is what
   ``encode_native`` forwards to LM Studio's ``/api/v1/chat`` body.

2. **Absent override stays absent.**  When the chat-send payload omits
   ``integrations``, the outbound LM Studio body has no ``integrations``
   key at all (LM Studio sees no MCP execution).  The admin's default-on
   flag is a UI seed for the composer; it is NOT applied server-side.
   This boundary is intentional — the backend stays a thin proxy.

3. **PUT + GET round-trip of the ``enabled_by_default`` flag.**  An admin
   write that flips the flag is reflected verbatim in the subsequent GET
   response so the composer can seed its chip-row.

Stub plumbing
-------------
``tests/integration/conftest.py::get_stub_last_chat_body`` captures the
most recent JSON body posted to the stub LM Studio's ``/api/v1/chat``.
These tests issue a POST /api/chat/stream and then assert against that
captured body.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.services.integrations_service import IntegrationsService
from tests.integration.conftest import (
    _STUB_TOOL_MODEL_ID,
    register_admin_and_login,
    register_and_login,
)


def _get_stub_last_chat_body() -> dict[str, Any]:
    """Resolve the stub's last-captured chat body from the *runtime* conftest.

    Pytest's conftest discovery and the importer-style ``tests.integration.conftest``
    path can produce two distinct module objects with two separate
    ``_STUB_LAST_CHAT_BODY`` dicts.  The stub server's HTTP handler runs in
    a uvicorn thread whose closure resolves whichever module was loaded
    first by ``_stub_lm_app``; reading from the OTHER module's name yields
    an empty dict.  Iterate every loaded ``conftest``-suffixed module and
    return the first non-empty dict we find.
    """
    import sys

    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if not name.endswith("conftest"):
            continue
        dct = getattr(mod, "_STUB_LAST_CHAT_BODY", None)
        if isinstance(dct, dict) and dct:
            return dict(dct)
    return {}


def _reset_stub_last_chat_body() -> None:
    """Clear the stub's captured body on every reachable conftest module.

    See :func:`_get_stub_last_chat_body` for the dual-module rationale.
    """
    import sys

    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if not name.endswith("conftest"):
            continue
        dct = getattr(mod, "_STUB_LAST_CHAT_BODY", None)
        if isinstance(dct, dict):
            dct.clear()

pytestmark = pytest.mark.asyncio


_STUB_MODEL = "stub-model-q4"
# Use the tool-trained stub so streaming_service does not drop integrations
# before they reach LM Studio (non-tool models trigger the filter added in
# fix(streaming): drop integrations for non-tool-trained models).
_STUB_TOOL_MODEL = _STUB_TOOL_MODEL_ID


async def _create_chat(client: httpx.AsyncClient, cookie: str) -> int:
    """Create a chat and return its id."""
    resp = await client.post(
        "/api/chats",
        data={"title": "p13h override chat"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    body: dict[str, Any] = resp.json()
    chat_id = int(body["id"])
    return chat_id


# ---------------------------------------------------------------------------
# 1. Override narrows the LM Studio request body
# ---------------------------------------------------------------------------


async def test_chat_send_integrations_override_forwarded_to_lmstudio(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """An explicit, available integrations override is forwarded to LM Studio."""
    _reset_stub_last_chat_body()
    _, cookie = await register_and_login(client)
    chat_id = await _create_chat(client, cookie)

    # The streaming route filters an explicit selection against the available
    # catalog (dropping removed servers like a stale mcp/firecrawl). Seed the
    # catalog directly with the two chosen ids so the override survives — the
    # test user is not the session admin, so the admin PUT would 403. Seeded via
    # the service and cleared in ``finally`` to avoid leaking into other
    # session-scoped integration tests.
    from lmchat.services.integrations_service import (
        IntegrationSetEntry,
        IntegrationsService,
    )

    _integ = IntegrationsService(
        engine=db_engine, env_default=[], local_mcp_config=None
    )
    await _integ.set_available(
        [
            IntegrationSetEntry(value="mcp/searxng", sort_order=0),
            IntegrationSetEntry(value="mcp/filesystem", sort_order=1),
        ]
    )

    try:
        # Per-message integrations subset chosen by the user via the composer
        # chip-row.  IMPORTANT: must use a tool-trained model — streaming_service
        # drops integrations for non-tool-trained models before they reach LM
        # Studio.
        payload = {
            "chat_id": chat_id,
            "payload": {
                "model": _STUB_TOOL_MODEL,
                "input": [{"type": "text", "content": "hello"}],
                "integrations": ["mcp/searxng", "mcp/filesystem"],
            },
        }

        resp = await client.post(
            "/api/chat/stream",
            json=payload,
            headers={"Cookie": f"lmchat_session={cookie}"},
        )
        assert resp.status_code == 200
        # Drain the SSE body so the streaming service finishes the upstream call.
        _ = resp.content

        captured = _get_stub_last_chat_body()
        assert captured.get("integrations") == ["mcp/searxng", "mcp/filesystem"], (
            f"expected integrations override to reach LM Studio, got {captured!r}"
        )
    finally:
        await _integ.set_available([])


# ---------------------------------------------------------------------------
# 2. Absent override leaves the LM Studio body without an integrations key
# ---------------------------------------------------------------------------


async def test_chat_send_without_integrations_omits_field(
    client: httpx.AsyncClient,
) -> None:
    """Default behaviour (no override): LM Studio body has no integrations key."""
    _reset_stub_last_chat_body()
    _, cookie = await register_and_login(client)
    chat_id = await _create_chat(client, cookie)

    payload = {
        "chat_id": chat_id,
        "payload": {
            "model": _STUB_MODEL,
            "input": [{"type": "text", "content": "hello"}],
            # integrations intentionally omitted
        },
    }

    resp = await client.post(
        "/api/chat/stream",
        json=payload,
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    _ = resp.content

    captured = _get_stub_last_chat_body()
    assert "integrations" not in captured, (
        f"expected no integrations key in LM Studio body, got {captured!r}"
    )


# ---------------------------------------------------------------------------
# 3. Admin enabled_by_default flag round-trips PUT → GET
# ---------------------------------------------------------------------------


async def test_admin_sets_default_on_flag_and_user_reads_it(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Admin PUTs entries with mixed ``enabled_by_default`` flags; GET reflects them."""
    _, admin_cookie = await register_admin_and_login(client, db_engine)
    _, user_cookie = await register_and_login(client)

    entries = [
        {
            "value": "mcp/searxng",
            "sort_order": 0,
            "enabled_by_default": True,
        },
        {
            "value": "mcp/filesystem",
            "sort_order": 1,
            "enabled_by_default": False,
        },
    ]

    put_resp = await client.put(
        "/api/integrations/available",
        data={"entries": json.dumps(entries)},
        headers={"Cookie": f"lmchat_session={admin_cookie}"},
    )
    assert put_resp.status_code == 200
    put_data = put_resp.json()
    assert len(put_data) == 2
    by_value = {e["value"]: e for e in put_data}
    assert by_value["mcp/searxng"]["enabled_by_default"] is True
    assert by_value["mcp/filesystem"]["enabled_by_default"] is False

    # Regular user reads the same shape — required for the composer chip-row
    # to seed its initial state from the admin's flag values.
    get_resp = await client.get(
        "/api/integrations/available",
        headers={"Cookie": f"lmchat_session={user_cookie}"},
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    by_value_get = {e["value"]: e for e in get_data}
    assert by_value_get["mcp/searxng"]["enabled_by_default"] is True
    assert by_value_get["mcp/filesystem"]["enabled_by_default"] is False


# ---------------------------------------------------------------------------
# 4. PUT defaults enabled_by_default to False when omitted (backwards-compat)
# ---------------------------------------------------------------------------


async def test_put_without_enabled_by_default_defaults_to_false(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Pre-P13h PUT clients still work; default flag is False."""
    _, admin_cookie = await register_admin_and_login(client, db_engine)

    entries = [{"value": "mcp/legacy", "sort_order": 0}]  # no enabled_by_default
    put_resp = await client.put(
        "/api/integrations/available",
        data={"entries": json.dumps(entries)},
        headers={"Cookie": f"lmchat_session={admin_cookie}"},
    )
    assert put_resp.status_code == 200
    data = put_resp.json()
    assert len(data) == 1
    assert data[0]["enabled_by_default"] is False


# ---------------------------------------------------------------------------
# 5. Fresh-DB discover-but-off contract, end-to-end (real app.py:685 wiring)
# ---------------------------------------------------------------------------
#
# Closes two review nits on the discover-but-off change:
#   N1: app.py:685's Settings.lm_chat_default_integrations_enabled_by_default
#       -> IntegrationsService(synthetic_enabled_by_default=...) wiring was
#       only exercised at the unit level (constructing IntegrationsService
#       directly in tests/services/test_integrations_service.py), never
#       through a live app whose config actually drives the constructor.
#   N2: no test proved the full round trip on an EMPTY DB — discovery
#       surfaces a server, but the default-injection path (integrations
#       omitted) must not arm it on a fresh install.
#
# This test runs against the session-scoped live app (real lifespan, real
# config-driven construction of app.state.integrations_service). Tier-2
# (local mcp.json) discovery is force-disabled for the whole session via
# LM_CHAT_LOCAL_MCP_DISCOVERY_ENABLED=false (see conftest.live_servers), so
# a temp mcp.json is swapped onto the *live* singleton's ``_local_mcp_config``
# post-construction -- the same pattern conftest itself uses to point
# models_service/lmstudio_adapter at the stub after startup.


async def test_fresh_db_discovered_server_off_by_default_and_not_injected(
    client: httpx.AsyncClient,
    live_servers: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh DB + a discoverable mcp.json server: listed but not pre-armed.

    1. GET /api/integrations/available surfaces the discovered server with
       enabled_by_default=False (discover-but-off).
    2. POST /api/chat/stream with integrations OMITTED (the default-
       injection path in routes/streaming.py) does not forward it to LM
       Studio -- proving the real config default reaches the live route,
       not just an isolated IntegrationsService instance.
    """
    integrations_service: IntegrationsService = live_servers["app"].state.integrations_service

    # Fresh DB: clear any rows a prior test in this session-scoped fixture
    # left behind (e.g. test_put_without_enabled_by_default_defaults_to_false
    # does not clean up its "mcp/legacy" row). set_available([]) is the
    # service's own "clear DB, fall back to synthetic tiers" path.
    await integrations_service.set_available([])

    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"fresh-discovered": {"command": "x"}}}),
        encoding="utf-8",
    )
    # monkeypatch auto-restores the original (None, per the session-wide
    # discovery-disabled env override) after this test.
    monkeypatch.setattr(integrations_service, "_local_mcp_config", cfg)

    _reset_stub_last_chat_body()
    _, cookie = await register_and_login(client)
    chat_id = await _create_chat(client, cookie)

    try:
        # 1. Discovered, listed, but NOT pre-selected.
        get_resp = await client.get(
            "/api/integrations/available",
            headers={"Cookie": f"lmchat_session={cookie}"},
        )
        assert get_resp.status_code == 200
        entries = get_resp.json()
        by_value = {e["value"]: e for e in entries}
        assert "mcp/fresh-discovered" in by_value, (
            f"expected discovered server in catalog, got {entries!r}"
        )
        assert by_value["mcp/fresh-discovered"]["enabled_by_default"] is False

        # 2. Default-injection path (integrations omitted) must not arm it.
        payload = {
            "chat_id": chat_id,
            "payload": {
                "model": _STUB_TOOL_MODEL_ID,
                "input": [{"type": "text", "content": "hello"}],
                # integrations intentionally omitted -> default-injection
            },
        }
        resp = await client.post(
            "/api/chat/stream",
            json=payload,
            headers={"Cookie": f"lmchat_session={cookie}"},
        )
        assert resp.status_code == 200
        _ = resp.content  # drain SSE so the upstream call completes

        captured = _get_stub_last_chat_body()
        assert captured.get("integrations", []) == [], (
            f"expected no tools auto-armed on a fresh install, got {captured!r}"
        )
    finally:
        await integrations_service.set_available([])
