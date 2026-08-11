# SPDX-License-Identifier: Apache-2.0
"""Unit tests for B4 startup rehydration — store → McpHost._configs.

Covers:
- lifespan helper populates _configs from the store for enabled servers.
- Disabled servers are not added to _configs (list_host_configs returns only enabled).
- Empty store: _configs is not mutated.
- Existing _configs entries are not overwritten (lazy registration).
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lmchat.mcp.host import McpHost, McpServerConfig

# ---------------------------------------------------------------------------
# Helper: replicate the B4 rehydration snippet from app.py lifespan
# ---------------------------------------------------------------------------


async def _rehydrate(
    mcp_host: Any,
    mcp_server_store: Any,
) -> int:
    """Run the B4 rehydration logic (mirrors the lifespan snippet).

    Returns the number of configs added.  Mirrors app.py's handling of
    ``credential_errors``: servers whose secret failed to decrypt are
    excluded from the returned configs and instead marked errored via
    ``mcp_host.record_credential_error`` — never rehydrated keyless.
    """
    credential_errors: dict[str, str] = {}
    configs = await mcp_server_store.list_host_configs(
        credential_errors=credential_errors
    )
    added = 0
    for cfg in configs:
        if cfg.server_id not in mcp_host._configs:
            mcp_host._configs[cfg.server_id] = McpServerConfig(
                server_id=cfg.server_id,
                transport=cfg.transport,
                command=cfg.command,
                args=cfg.args,
                env=cfg.env,
                url=cfg.url,
            )
            added += 1
    for slug, message in credential_errors.items():
        mcp_host.record_credential_error(slug, message)
    return added


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydration_populates_configs() -> None:
    """list_host_configs() results are registered in mcp_host._configs."""
    store = AsyncMock()
    store.list_host_configs = AsyncMock(
        return_value=[
            McpServerConfig(
                server_id="github",
                transport="stdio",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                env={"GITHUB_TOKEN": "ghp_test"},
                url="",
            )
        ]
    )

    host = MagicMock()
    host._configs = {}

    added = await _rehydrate(host, store)

    assert added == 1
    assert "github" in host._configs
    cfg = host._configs["github"]
    assert cfg.server_id == "github"
    assert cfg.transport == "stdio"
    assert cfg.command == "npx"
    assert cfg.env == {"GITHUB_TOKEN": "ghp_test"}


@pytest.mark.asyncio
async def test_rehydration_empty_store() -> None:
    """Empty store: _configs is unchanged."""
    store = AsyncMock()
    store.list_host_configs = AsyncMock(return_value=[])

    host = MagicMock()
    host._configs = {}

    added = await _rehydrate(host, store)

    assert added == 0
    assert host._configs == {}


@pytest.mark.asyncio
async def test_rehydration_does_not_overwrite_existing() -> None:
    """Existing _configs entries are not overwritten (lazy: skip if already present)."""
    existing_cfg = McpServerConfig(
        server_id="github",
        transport="stdio",
        command="original-cmd",
        args=[],
        env={},
        url="",
    )

    store = AsyncMock()
    store.list_host_configs = AsyncMock(
        return_value=[
            McpServerConfig(
                server_id="github",
                transport="stdio",
                command="new-cmd",
                args=[],
                env={},
                url="",
            )
        ]
    )

    host = MagicMock()
    host._configs = {"github": existing_cfg}

    added = await _rehydrate(host, store)

    # Existing entry must NOT be replaced.
    assert added == 0
    assert host._configs["github"].command == "original-cmd"


@pytest.mark.asyncio
async def test_rehydration_multiple_servers() -> None:
    """Multiple enabled servers all end up in _configs."""
    store = AsyncMock()
    store.list_host_configs = AsyncMock(
        return_value=[
            McpServerConfig(
                server_id="github",
                transport="stdio",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                env={},
                url="",
            ),
            McpServerConfig(
                server_id="firecrawl",
                transport="http",
                command="",
                args=[],
                env={"FIRECRAWL_API_KEY": "fc-key"},
                url="https://api.firecrawl.dev",
            ),
        ]
    )

    host = MagicMock()
    host._configs = {}

    added = await _rehydrate(host, store)

    assert added == 2
    assert "github" in host._configs
    assert "firecrawl" in host._configs
    assert host._configs["firecrawl"].env == {"FIRECRAWL_API_KEY": "fc-key"}


@pytest.mark.asyncio
async def test_rehydration_credential_decrypt_failure_marks_host_errored() -> None:
    """P2 SILENT-FAILURE regression: a server whose secret failed to decrypt
    (wrong/rotated LM_CHAT_SECRET, corrupt ciphertext) must NOT be rehydrated
    into ``_configs`` as if healthy — it must land in McpHost.last_error()
    instead, so the admin's server list can show a needs-reauth state.
    """
    error_message = (
        "Credential decryption failed for API_KEY — re-enter the secret."
    )

    async def _fake_list_host_configs(
        *, credential_errors: dict[str, str] | None = None
    ) -> list[McpServerConfig]:
        # Mirrors McpServerStore.list_host_configs: the broken server is
        # excluded from the returned configs and reported via the out-param
        # instead of being silently rehydrated without its secret.
        if credential_errors is not None:
            credential_errors["broken-server"] = error_message
        return []

    store = MagicMock()
    store.list_host_configs = _fake_list_host_configs

    host = McpHost(config_path=None)

    added = await _rehydrate(host, store)

    assert added == 0
    # Never rehydrated — not configured, not connected, not "healthy".
    assert "broken-server" not in host._configs
    assert "broken-server" not in host.configured_server_ids
    assert "broken-server" not in host.connected_server_ids

    # But the failure IS visible via the same last_error() the admin route
    # (routes/mcp_store.py) already reads for every server in the list.
    assert host.last_error("broken-server") == error_message

    # A subsequent connect attempt on the unconfigured slug fails cleanly
    # (unknown server) and does NOT clear the credential error — it stays
    # visible until the admin actually fixes the secret.
    connected = await host.connect("broken-server")
    assert connected is False
    assert host.last_error("broken-server") == error_message


@pytest.mark.asyncio
async def test_rehydration_store_error_does_not_raise() -> None:
    """If list_host_configs() raises, _rehydrate propagates the error.

    This test validates that the LIFESPAN try/except (FIX A2) is what
    protects startup — the helper itself lets the exception bubble so the
    caller (lifespan) can decide whether to swallow it.
    The test confirms the failure mode the try/except in app.py guards against.
    """
    store = AsyncMock()
    store.list_host_configs = AsyncMock(
        side_effect=RuntimeError("simulated DB corruption")
    )

    host = MagicMock()
    host._configs = {}

    # _rehydrate propagates; the lifespan wrapper catches it.
    with pytest.raises(RuntimeError, match="simulated DB corruption"):
        await _rehydrate(host, store)

    # _configs must be unchanged.
    assert host._configs == {}


# ---------------------------------------------------------------------------
# Decouple guard — McpHost(config_path=None) is Store-only; mcp.json is
# LM Studio's own native-loop config and is NEVER a McpHost runtime source.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcphost_config_path_none_ignores_lmstudio_mcp_json(
    tmp_path: Path,
) -> None:
    """``McpHost(config_path=None)`` — the construction app.py's lifespan now
    ALWAYS uses — must never ingest ``~/.lmstudio/mcp.json``. Only the B4
    store-rehydration path (mirrored by ``_rehydrate`` above) may populate
    ``_configs``.

    Server ``Y`` simulates a server LM Studio itself runs (its own native
    tool loop, host-side); server ``X`` simulates a Store-installed server
    (the container's actual MCP execution source). After the decouple, an
    app-constructed McpHost must end up with X and never Y — even though a
    file containing Y sits on disk right next to the test.
    """
    lmstudio_mcp_json = tmp_path / "mcp.json"
    lmstudio_mcp_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "Y": {"command": "npx", "args": ["-y", "host-only-server"]}
                }
            }
        ),
        encoding="utf-8",
    )

    # Sanity check: the fixture file is valid and _parse_mcp_json would
    # ingest "Y" if McpHost were pointed AT it directly (proves the file
    # itself is well-formed, isolating the assertion below to the
    # config_path=None behavior rather than a bad fixture).
    sanity_host = McpHost(config_path=lmstudio_mcp_json)
    assert "Y" in sanity_host._configs

    # The actual app.py construction: config_path=None, unconditionally.
    host = McpHost(config_path=None)
    assert host._configs == {}
    assert "Y" not in host._configs

    # B4 rehydration (mirrors the lifespan snippet exactly): the Store —
    # never mcp.json — is the sole source of McpHost's configured servers.
    store = AsyncMock()
    store.list_host_configs = AsyncMock(
        return_value=[
            McpServerConfig(
                server_id="X",
                transport="stdio",
                command="uvx",
                args=["store-installed-server"],
                env={},
                url="",
            )
        ]
    )
    added = await _rehydrate(host, store)

    assert added == 1
    assert "X" in host._configs
    assert "Y" not in host._configs


def test_app_lifespan_constructs_mcphost_store_only() -> None:
    """Static regression guard: app.py's B1/B2 block must construct McpHost
    with an unconditional ``config_path=None`` — never resolving its own
    default (``~/.lmstudio/mcp.json``) and never branching on
    ``lm_chat_local_mcp_discovery_enabled`` (that setting now only gates
    IntegrationsService's native-picker file discovery, not McpHost).
    """
    from lmchat.app import lifespan

    src = inspect.getsource(lifespan)
    assert "McpHost(" in src and "config_path=None" in src, (
        "app.py must construct McpHost with an explicit, unconditional "
        "config_path=None — Store-only execution."
    )
    assert "_mcp_kwargs" not in src, (
        "the old conditional config_path branch must be gone; McpHost "
        "construction is no longer gated by lm_chat_local_mcp_discovery_enabled"
    )
