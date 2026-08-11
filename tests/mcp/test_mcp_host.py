# SPDX-License-Identifier: Apache-2.0
"""Unit + e2e tests for McpHost.

Covers:
- Config parsing: stdio, http, missing file, bad JSON, field coercion
- Namespacing: <server>_<tool> snake_case + sanitisation + length trimming +
  de-dup of self-prefixed MCP tool names
- inputSchema → CanonicalTool translation (table of shapes)
- Env sanitisation: assert app secrets NOT in child env
- Pool idempotency: double-connect returns True without re-connecting
- Connection lifecycle: dedicated session task, clean connect→call→
  shutdown with no anyio cancel-scope crash; multi-server shutdown
- De-dup routing: self-prefixed names still route to the real MCP tool name
- call_tool routing: correct server/name lookup, error paths
- E2E: npx @modelcontextprotocol/server-filesystem (skipped if npx unavailable)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import mcp.types as mcp_types
import pytest

from lmchat.lmstudio.types import CanonicalTool
from lmchat.mcp.host import (
    McpHost,
    McpServerConfig,
    _build_child_env,
    _format_connect_error,
    _make_namespaced_name,
    _parse_mcp_json,
    _read_stderr_tail,
    _translate_tool,
    _wrap_with_sandbox,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mcp_tool(
    name: str,
    description: str = "A tool",
    schema: dict | None = None,
) -> mcp_types.Tool:
    if schema is None:
        schema = {"type": "object", "properties": {}}
    return mcp_types.Tool(name=name, description=description, input_schema=schema)


def _make_list_tools_result(tools: list[mcp_types.Tool]) -> mcp_types.ListToolsResult:
    return mcp_types.ListToolsResult(tools=tools)


def _make_call_tool_result(
    text: str,
    *,
    is_error: bool = False,
    structured: dict | None = None,
) -> mcp_types.CallToolResult:
    content: list[mcp_types.ContentBlock] = [mcp_types.TextContent(type="text", text=text)]
    return mcp_types.CallToolResult(
        content=content,
        is_error=is_error,
        structured_content=structured,
    )

class _MockSessionCM:
    """Async-context-manager wrapper so a mock session works with
    ``stack.enter_async_context(ClientSession(...))``."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        return None


async def _connect_mock_server(
    host: McpHost,
    server_id: str,
    *,
    tools: list[mcp_types.Tool] | None = None,
    session: AsyncMock | None = None,
) -> AsyncMock:
    """Connect *server_id* through the REAL lifecycle, backed by a mock session.

    Patches ``_open_streams`` (so no real subprocess is spawned) and
    ``ClientSession`` (so the dedicated session task drives a mock).  The
    full connect → _session_task → _serve_requests path runs for real, which
    is what the lifecycle/routing tests need to exercise.  Returns the mock
    session so the caller can assert on / configure ``call_tool`` etc.
    """
    if session is None:
        # Plain AsyncMock — every method call (initialize/list_tools/call_tool)
        # returns an awaitable.  (A `spec=[...]` list would make children plain
        # MagicMocks, which aren't awaitable.)
        session = AsyncMock()
        session.initialize = AsyncMock(return_value=None)
        session.list_tools = AsyncMock(
            return_value=_make_list_tools_result(tools or [])
        )
        session.call_tool = AsyncMock()
    elif tools is not None:
        session.list_tools = AsyncMock(return_value=_make_list_tools_result(tools))

    # Register the config if not already present (host built with config_path=None).
    if server_id not in host._configs:
        host._configs[server_id] = McpServerConfig(
            server_id=server_id, transport="stdio", command="npx"
        )

    async def fake_open_streams(
        config: McpServerConfig, stack: Any, errbuf: Any = None
    ) -> tuple[Any, Any]:
        return AsyncMock(), AsyncMock()

    captured = session

    def fake_client_session(read: Any, write: Any) -> _MockSessionCM:
        return _MockSessionCM(captured)

    with (
        patch.object(host, "_open_streams", side_effect=fake_open_streams),
        patch("lmchat.mcp.host.ClientSession", side_effect=fake_client_session),
    ):
        ok = await host.connect(server_id)
    assert ok is True, f"mock connect for '{server_id}' failed"
    return session


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestParseMcpJson:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = _parse_mcp_json(tmp_path / "nonexistent.json")
        assert result == []

    def test_none_path_returns_empty(self) -> None:
        result = _parse_mcp_json(None)
        assert result == []

    def test_bad_json_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "mcp.json"
        p.write_text("not json", encoding="utf-8")
        result = _parse_mcp_json(p)
        assert result == []

    def test_missing_mcp_servers_key(self, tmp_path: Path) -> None:
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps({"other": {}}), encoding="utf-8")
        result = _parse_mcp_json(p)
        assert result == []

    def test_stdio_server_parsed(self, tmp_path: Path) -> None:
        doc = {
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "env": {"FOO": "bar"},
                }
            }
        }
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        configs = _parse_mcp_json(p)
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.server_id == "filesystem"
        assert cfg.transport == "stdio"
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        assert cfg.env == {"FOO": "bar"}
        assert cfg.url == ""

    def test_http_server_parsed(self, tmp_path: Path) -> None:
        doc = {
            "mcpServers": {
                "remote": {
                    "url": "https://mcp.example.com/",
                }
            }
        }
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        configs = _parse_mcp_json(p)
        assert len(configs) == 1
        assert configs[0].transport == "http"
        assert configs[0].url == "https://mcp.example.com/"
        assert configs[0].command == ""

    def test_multiple_servers(self, tmp_path: Path) -> None:
        doc = {
            "mcpServers": {
                "a": {"command": "uvx", "args": ["tool-a"]},
                "b": {"url": "http://localhost:9000"},
            }
        }
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        configs = _parse_mcp_json(p)
        assert len(configs) == 2
        ids = {c.server_id for c in configs}
        assert ids == {"a", "b"}

    def test_non_str_env_values_coerced(self, tmp_path: Path) -> None:
        doc = {
            "mcpServers": {
                "s": {
                    "command": "node",
                    "args": [],
                    "env": {"PORT": 3000, "DEBUG": True},
                }
            }
        }
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        configs = _parse_mcp_json(p)
        assert configs[0].env == {"PORT": "3000", "DEBUG": "True"}

    def test_empty_name_skipped(self, tmp_path: Path) -> None:
        doc = {"mcpServers": {"": {"command": "node"}}}
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        configs = _parse_mcp_json(p)
        # Empty-name entry should be skipped.
        assert configs == []


# ---------------------------------------------------------------------------
# Namespacing and sanitisation
# ---------------------------------------------------------------------------


class TestNamespacing:
    def test_simple_names(self) -> None:
        result = _make_namespaced_name("filesystem", "read_file")
        assert result == "filesystem_read_file"

    def test_uppercase_lowercased(self) -> None:
        result = _make_namespaced_name("MyServer", "ReadFile")
        assert result == "myserver_readfile"

    def test_hyphens_replaced(self) -> None:
        result = _make_namespaced_name("my-server", "do-thing")
        assert result == "my_server_do_thing"

    def test_dots_replaced(self) -> None:
        result = _make_namespaced_name("server.v2", "tools.search")
        assert result == "server_v2_tools_search"

    def test_too_short_name_fails(self) -> None:
        # "a" + "_" + "b" = "a_b" — length 3, starts with a → valid
        result = _make_namespaced_name("a", "b")
        assert result == "a_b"
        assert result is not None

    def test_invalid_result_returns_none(self) -> None:
        # A name that starts with a digit after sanitisation fails the regex.
        # Force that by making server_id start with a digit.
        result = _make_namespaced_name("1bad", "tool")
        # After sanitisation: "1bad_tool" — starts with "1", not [a-z] → None
        assert result is None

    def test_long_name_trimmed(self) -> None:
        long_tool = "t" * 70
        result = _make_namespaced_name("srv", long_tool)
        # "srv_ttttt..." trimmed to 64 chars; must still match the regex.
        assert result is not None
        assert len(result) <= 64

    # --- De-dup cases: self-prefixed MCP tool names ----------------

    def test_dedup_tool_already_prefixed_with_underscore(self) -> None:
        # firecrawl exposes "firecrawl_search" — must NOT become
        # "firecrawl_firecrawl_search".
        result = _make_namespaced_name("firecrawl", "firecrawl_search")
        assert result == "firecrawl_search"

    def test_dedup_deepwiki(self) -> None:
        result = _make_namespaced_name("deepwiki", "deepwiki_fetch")
        assert result == "deepwiki_fetch"

    def test_dedup_tool_equals_server_id(self) -> None:
        # Tool name == server id exactly (length-permitting): use as-is.
        result = _make_namespaced_name("github", "github")
        assert result == "github"

    def test_no_dedup_when_not_self_prefixed(self) -> None:
        # context7 exposes "query_docs" — gets the normal prefix.
        result = _make_namespaced_name("context7", "query_docs")
        assert result == "context7_query_docs"

    def test_no_dedup_on_partial_prefix_collision(self) -> None:
        # Server "fire", tool "firewall_status" starts with "fire" but NOT
        # "fire_" and is not equal → still prefixed (no false-positive dedup).
        result = _make_namespaced_name("fire", "firewall_status")
        assert result == "fire_firewall_status"

    def test_dedup_case_insensitive(self) -> None:
        result = _make_namespaced_name("Firecrawl", "Firecrawl_Search")
        assert result == "firecrawl_search"


# ---------------------------------------------------------------------------
# inputSchema → CanonicalTool translation
# ---------------------------------------------------------------------------


SCHEMA_TABLE: list[tuple[str, dict[str, Any]]] = [
    (
        "empty_object",
        {"type": "object", "properties": {}},
    ),
    (
        "with_required",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
            },
            "required": ["path"],
        },
    ),
    (
        "nested",
        {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {"recursive": {"type": "boolean"}},
                }
            },
        },
    ),
    (
        "array_param",
        {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
]


class TestToolTranslation:
    @pytest.mark.parametrize("label,schema", SCHEMA_TABLE)
    def test_schema_passthrough(self, label: str, schema: dict) -> None:
        """inputSchema should be passed through unchanged as parameters."""
        mcp_tool = _make_mcp_tool("do_thing", schema=schema)
        ct = _translate_tool("myserver", mcp_tool)
        assert ct is not None, f"Translation failed for {label}"
        assert ct.parameters == schema, f"Schema mismatch for {label}"

    def test_name_namespaced(self) -> None:
        ct = _translate_tool("firecrawl", _make_mcp_tool("search"))
        assert ct is not None
        assert ct.name == "firecrawl_search"

    def test_description_passed(self) -> None:
        ct = _translate_tool("s", _make_mcp_tool("t", description="Does X"))
        assert ct is not None
        assert ct.description == "Does X"

    def test_none_description_becomes_empty(self) -> None:
        tool = mcp_types.Tool(name="t", description=None, input_schema={})
        ct = _translate_tool("s", tool)
        assert ct is not None
        assert ct.description == ""

    def test_invalid_name_returns_none(self) -> None:
        # Server id starting with digit → namespaced name starts with digit → rejected.
        ct = _translate_tool("1bad", _make_mcp_tool("thing"))
        assert ct is None

    def test_returns_canonical_tool_type(self) -> None:
        ct = _translate_tool("server", _make_mcp_tool("my_tool"))
        assert isinstance(ct, CanonicalTool)


# ---------------------------------------------------------------------------
# Env sanitisation
# ---------------------------------------------------------------------------


class TestEnvSanitisation:
    """Assert that app secrets are NOT in the env passed to stdio children."""

    # Variables that must NEVER appear in the child env.
    _FORBIDDEN = [
        "LM_CHAT_SECRET",
        "LM_CHAT_DB_URL",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "DATABASE_URL",
        "AUTH_SECRET",
        "AWS_SECRET_ACCESS_KEY",
        "JWT_SECRET",
    ]

    def _set_secrets_in_os_env(self) -> None:
        for key in self._FORBIDDEN:
            os.environ[key] = "super_secret_value"

    def _unset_secrets_from_os_env(self) -> None:
        for key in self._FORBIDDEN:
            os.environ.pop(key, None)

    def test_app_secrets_not_in_child_env(self) -> None:
        self._set_secrets_in_os_env()
        try:
            child_env = _build_child_env({})
            for key in self._FORBIDDEN:
                assert key not in child_env, (
                    f"Secret '{key}' leaked into stdio child env!"
                )
        finally:
            self._unset_secrets_from_os_env()

    def test_server_declared_env_is_included(self) -> None:
        server_env = {"GITHUB_TOKEN": "tok_abc123", "SOME_SERVER_VAR": "value"}
        child_env = _build_child_env(server_env)
        assert child_env["GITHUB_TOKEN"] == "tok_abc123"
        assert child_env["SOME_SERVER_VAR"] == "value"

    def test_path_is_included(self) -> None:
        child_env = _build_child_env({})
        assert "PATH" in child_env

    def test_server_env_cannot_inject_app_secrets_via_rename(self) -> None:
        # Server tries to inject LM_CHAT_SECRET via its own env block.
        server_env = {"LM_CHAT_SECRET": "injected!"}
        # The build function includes it verbatim — the admin explicitly
        # declared it.  The important invariant is that OS-level app secrets
        # are NOT auto-inherited; a server that declares its own copy is
        # technically allowed (it's the admin's own config file).
        # This test documents the actual behaviour, not a security hole.
        child_env = _build_child_env(server_env)
        # The value comes from the server's own env block, not from os.environ.
        assert child_env.get("LM_CHAT_SECRET") == "injected!"
        # The OS-level value (if different) is NOT used instead.
        os.environ["LM_CHAT_SECRET"] = "real_secret"
        try:
            fresh_env = _build_child_env({"LM_CHAT_SECRET": "injected!"})
            assert fresh_env["LM_CHAT_SECRET"] == "injected!"
        finally:
            del os.environ["LM_CHAT_SECRET"]


# ---------------------------------------------------------------------------
# Pool idempotency
# ---------------------------------------------------------------------------


class TestPoolIdempotency:
    """Connect twice → second call must be a no-op (no duplicate session)."""

    async def _make_connected_host(self, tmp_path: Path) -> McpHost:
        """Build a host with one server connected via the real lifecycle."""
        doc = {"mcpServers": {"myserver": {"command": "npx", "args": []}}}
        cfg_path = tmp_path / "mcp.json"
        cfg_path.write_text(json.dumps(doc))

        host = McpHost(config_path=cfg_path)
        await _connect_mock_server(host, "myserver")
        return host

    @pytest.mark.asyncio
    async def test_double_connect_idempotent(self, tmp_path: Path) -> None:
        host = await self._make_connected_host(tmp_path)
        initial_pool_size = len(host._pool)
        initial_task = host._pool["myserver"].task

        # Second connect — must return True without adding another entry or
        # replacing the live session task.
        result = await host.connect("myserver")
        assert result is True
        assert len(host._pool) == initial_pool_size
        assert host._pool["myserver"].task is initial_task

        await host.shutdown()

    @pytest.mark.asyncio
    async def test_unknown_server_returns_false(self, tmp_path: Path) -> None:
        host = McpHost(config_path=None)
        result = await host.connect("does_not_exist")
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_all_configured_idempotent(self, tmp_path: Path) -> None:
        host = await self._make_connected_host(tmp_path)
        # Call connect_all_configured a second time.
        with patch.object(host, "_do_connect", new_callable=AsyncMock) as mock_dc:
            results = await host.connect_all_configured()
        # _do_connect must NOT be called because all servers already connected.
        mock_dc.assert_not_called()
        assert results == {"myserver": True}

        await host.shutdown()


# ---------------------------------------------------------------------------
# call_tool routing
# ---------------------------------------------------------------------------


class TestCallToolRouting:
    async def _make_host_with_server(
        self,
        server_id: str = "fs",
        tool_names: list[str] | None = None,
    ) -> tuple[McpHost, AsyncMock]:
        if tool_names is None:
            tool_names = ["read_file"]
        host = McpHost(config_path=None)
        mcp_tools = [_make_mcp_tool(t) for t in tool_names]
        session = await _connect_mock_server(host, server_id, tools=mcp_tools)
        return host, session

    @pytest.mark.asyncio
    async def test_call_routes_to_correct_server(self) -> None:
        host, mock_session = await self._make_host_with_server("fs", ["read_file"])
        mock_session.call_tool.return_value = _make_call_tool_result("file contents")

        result = await host.call_tool("fs_read_file", {"path": "/tmp/x"})
        assert result == "file contents"
        mock_session.call_tool.assert_called_once_with("read_file", {"path": "/tmp/x"})
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_call_unknown_tool_returns_error_string(self) -> None:
        host, _ = await self._make_host_with_server("fs", ["read_file"])
        result = await host.call_tool("fs_nonexistent", {})
        assert "[mcp_error]" in result
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_call_mcp_error_result_returns_error_string(self) -> None:
        host, mock_session = await self._make_host_with_server("fs", ["bad_tool"])
        mock_session.call_tool.return_value = _make_call_tool_result(
            "something broke", is_error=True
        )
        result = await host.call_tool("fs_bad_tool", {})
        assert "[mcp_error]" in result
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_call_tool_exception_returns_error_string(self) -> None:
        host, mock_session = await self._make_host_with_server("fs", ["explode"])
        mock_session.call_tool.side_effect = RuntimeError("boom")
        result = await host.call_tool("fs_explode", {})
        assert "[mcp_error]" in result
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_call_tool_structured_content_json(self) -> None:
        host, mock_session = await self._make_host_with_server("fs", ["list_dir"])
        mock_session.call_tool.return_value = _make_call_tool_result(
            "", structured={"files": ["a.txt", "b.txt"]}
        )
        result = await host.call_tool("fs_list_dir", {})
        parsed = json.loads(result)
        assert parsed["files"] == ["a.txt", "b.txt"]
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_call_tool_timeout_returns_error_string(self) -> None:
        host, mock_session = await self._make_host_with_server("fs", ["slow"])

        async def slow(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(999)

        mock_session.call_tool.side_effect = slow

        host._call_timeout = 0.05  # 50 ms for test speed
        result = await host.call_tool("fs_slow", {})
        assert "[mcp_error]" in result
        assert "timed out" in result
        await host.shutdown()


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


class TestListTools:
    async def _host_with_tools(
        self, server_id: str, tool_names: list[str]
    ) -> McpHost:
        host = McpHost(config_path=None)
        mcp_tools = [_make_mcp_tool(t) for t in tool_names]
        await _connect_mock_server(host, server_id, tools=mcp_tools)
        return host

    @pytest.mark.asyncio
    async def test_list_all(self) -> None:
        host = await self._host_with_tools("fs", ["read_file", "write_file"])
        tools = host.list_tools()
        assert len(tools) == 2
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_list_filtered(self) -> None:
        host = await self._host_with_tools("fs", ["read_file"])
        # Add a second connected server.
        await _connect_mock_server(host, "gh", tools=[_make_mcp_tool("create_issue")])

        tools = host.list_tools(server_ids=["fs"])
        assert all(t.name.startswith("fs_") for t in tools)
        assert len(tools) == 1
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_list_unknown_server_ignored(self) -> None:
        host = await self._host_with_tools("fs", ["read_file"])
        tools = host.list_tools(server_ids=["does_not_exist"])
        assert tools == []
        await host.shutdown()


# ---------------------------------------------------------------------------
# E2E: real npx server (skipped if npx unavailable)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Connection lifecycle: dedicated session task, clean teardown
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_connect_call_shutdown_clean(self) -> None:
        """Full connect → call → shutdown with a mocked session task.

        The session task must own the (mock) session contexts and tear them
        down in its own scope on shutdown — no cross-task cancel-scope error.
        """
        host = McpHost(config_path=None)
        session = await _connect_mock_server(
            host, "fs", tools=[_make_mcp_tool("read_file")]
        )
        session.call_tool.return_value = _make_call_tool_result("data")

        # The dedicated session task is alive and registered.
        assert "fs" in host.connected_server_ids
        task = host._pool["fs"].task
        assert not task.done()

        # A call routes through the task and returns.
        result = await host.call_tool("fs_read_file", {"path": "/x"})
        assert result == "data"

        # Shutdown must complete cleanly and reap the task.
        await host.shutdown()
        assert host.connected_server_ids == []
        assert task.done()
        # The task finished normally (not via an unhandled exception).
        assert task.exception() is None

    @pytest.mark.asyncio
    async def test_shutdown_multiple_servers_clean(self) -> None:
        """Shutdown with several connected servers — no cancel-scope crash.

        This is the exact multi-server scenario that triggered the anyio
        'exit a cancel scope that isn't the current task's' RuntimeError in
        the original AsyncExitStack-closed-from-host design.
        """
        host = McpHost(config_path=None)
        tasks = []
        for sid in ("a", "b", "c"):
            await _connect_mock_server(host, sid, tools=[_make_mcp_tool("t_one")])
            tasks.append(host._pool[sid].task)

        assert sorted(host.connected_server_ids) == ["a", "b", "c"]

        # Must NOT raise.
        await host.shutdown()

        assert host.connected_server_ids == []
        for task in tasks:
            assert task.done()
            assert task.exception() is None

    @pytest.mark.asyncio
    async def test_disconnect_single_server(self) -> None:
        host = McpHost(config_path=None)
        await _connect_mock_server(host, "a", tools=[_make_mcp_tool("t_one")])
        await _connect_mock_server(host, "b", tools=[_make_mcp_tool("t_one")])

        await host.disconnect("a")
        assert "a" not in host.connected_server_ids
        assert "b" in host.connected_server_ids
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_disconnect_unknown_is_noop(self) -> None:
        host = McpHost(config_path=None)
        # Should not raise.
        await host.disconnect("nope")
        assert host.connected_server_ids == []

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self) -> None:
        host = McpHost(config_path=None)
        await _connect_mock_server(host, "a", tools=[_make_mcp_tool("t_one")])
        await host.shutdown()
        # Second shutdown is a no-op and must not raise.
        await host.shutdown()
        assert host.connected_server_ids == []

    @pytest.mark.asyncio
    async def test_refresh_tools_after_connect(self) -> None:
        """refresh_tools must re-list via the session task and update cache."""
        host = McpHost(config_path=None)
        session = await _connect_mock_server(
            host, "fs", tools=[_make_mcp_tool("read_file")]
        )
        assert {t.name for t in host.list_tools()} == {"fs_read_file"}

        # Server now reports a different tool set.
        session.list_tools.return_value = _make_list_tools_result(
            [_make_mcp_tool("read_file"), _make_mcp_tool("write_file")]
        )
        await host.refresh_tools("fs")
        assert {t.name for t in host.list_tools()} == {
            "fs_read_file",
            "fs_write_file",
        }
        await host.shutdown()


# ---------------------------------------------------------------------------
# De-dup routing: self-prefixed names still route to the real tool
# ---------------------------------------------------------------------------


class TestDedupRouting:
    @pytest.mark.asyncio
    async def test_self_prefixed_tool_routes_to_original(self) -> None:
        """firecrawl_search (deduped) must call the MCP tool 'firecrawl_search'."""
        host = McpHost(config_path=None)
        session = await _connect_mock_server(
            host, "firecrawl", tools=[_make_mcp_tool("firecrawl_search")]
        )
        session.call_tool.return_value = _make_call_tool_result("hits")

        # The advertised name is deduped (no double prefix).
        names = {t.name for t in host.list_tools()}
        assert names == {"firecrawl_search"}

        # Calling it must route to the ORIGINAL MCP name 'firecrawl_search',
        # not a stripped 'search'.
        result = await host.call_tool("firecrawl_search", {"q": "x"})
        assert result == "hits"
        session.call_tool.assert_called_once_with("firecrawl_search", {"q": "x"})
        await host.shutdown()

    @pytest.mark.asyncio
    async def test_non_prefixed_tool_routes_to_original(self) -> None:
        """context7 query_docs → advertised context7_query_docs → calls 'query_docs'."""
        host = McpHost(config_path=None)
        session = await _connect_mock_server(
            host, "context7", tools=[_make_mcp_tool("query_docs")]
        )
        session.call_tool.return_value = _make_call_tool_result("docs")

        names = {t.name for t in host.list_tools()}
        assert names == {"context7_query_docs"}

        result = await host.call_tool("context7_query_docs", {})
        assert result == "docs"
        session.call_tool.assert_called_once_with("query_docs", {})
        await host.shutdown()


@pytest.mark.asyncio
async def test_e2e_filesystem_server(tmp_path: Path) -> None:
    """Connect a real npx MCP filesystem server, list tools, call list_allowed_directories."""
    npx = shutil.which("npx")
    if npx is None:
        pytest.skip("npx not available — skipping e2e MCP server test")

    # Create a temp dir the server is allowed to serve.
    serve_dir = tmp_path / "served"
    serve_dir.mkdir()
    (serve_dir / "hello.txt").write_text("hello world")

    cfg = McpServerConfig(
        server_id="fs",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", str(serve_dir)],
        env={},
    )
    host = McpHost(config_path=None, extra_configs=[cfg])

    ok = await host.connect("fs")
    if not ok:
        pytest.skip(
            "npx @modelcontextprotocol/server-filesystem failed to connect — "
            "network/npx issue; skipping e2e test"
        )

    try:
        tools = host.list_tools()
        assert len(tools) > 0, "Expected at least one tool from filesystem server"

        # All tools must be CanonicalTool instances with valid names.
        for t in tools:
            assert isinstance(t, CanonicalTool)
            assert t.name.startswith("fs_"), f"Expected 'fs_' prefix, got '{t.name}'"

        # Call list_allowed_directories (always available on this server).
        result = await host.call_tool("fs_list_allowed_directories", {})
        assert isinstance(result, str)
        assert str(serve_dir) in result or "[mcp_error]" not in result

    finally:
        await host.shutdown()


# ---------------------------------------------------------------------------
# Landlock sandbox wiring: _open_streams wraps/unwraps stdio children
# ---------------------------------------------------------------------------


class _FakeStdioCM:
    """Async-context-manager stand-in for ``stdio_client`` — never spawns."""

    async def __aenter__(self) -> tuple[Any, Any]:
        return AsyncMock(), AsyncMock()

    async def __aexit__(self, *exc: object) -> None:
        return None


class TestWrapWithSandbox:
    """Pure-function coverage of the launcher-args builder."""

    def test_no_extra_allow(self) -> None:
        from lmchat.mcp.host import _LANDLOCK_LAUNCHER

        config = McpServerConfig(
            server_id="s", transport="stdio", command="npx", args=["-y", "pkg"]
        )
        command, args = _wrap_with_sandbox(config)
        assert command == sys.executable
        assert args == [_LANDLOCK_LAUNCHER, "--", "npx", "-y", "pkg"]

    def test_with_extra_allow(self) -> None:
        from lmchat.mcp.host import _LANDLOCK_LAUNCHER

        config = McpServerConfig(
            server_id="s",
            transport="stdio",
            command="npx",
            args=["-y", "pkg"],
            sandbox_allow=["/a", "/b"],
        )
        _, args = _wrap_with_sandbox(config)
        assert args == [
            _LANDLOCK_LAUNCHER, "--allow", "/a", "--allow", "/b", "--", "npx", "-y", "pkg"
        ]


class TestResolveCommand:
    """Host-path resolution for configs discovered from a mounted mcp.json.

    A ``~/.lmstudio/mcp.json`` mounted into a container carries the host's
    absolute paths (e.g. ``/Users/you/.nvm/.../npx``); those must fall back to
    the container's own runtime on PATH, while bare names and existing
    absolute paths pass through untouched.
    """

    def test_bare_command_passes_through(self) -> None:
        from lmchat.mcp.host import _resolve_command

        assert _resolve_command("npx") == "npx"

    def test_existing_absolute_passes_through(self) -> None:
        from lmchat.mcp.host import _resolve_command

        # sys.executable is an absolute path that exists on this system.
        assert _resolve_command(sys.executable) == sys.executable

    def test_missing_absolute_falls_back_to_basename_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lmchat.mcp import host as host_mod

        monkeypatch.setattr(
            host_mod.shutil,
            "which",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )
        got = host_mod._resolve_command(
            "/Users/someone/.nvm/versions/node/v24.15.0/bin/npx"
        )
        assert got == "/usr/bin/npx"

    def test_missing_absolute_unresolvable_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lmchat.mcp import host as host_mod

        monkeypatch.setattr(host_mod.shutil, "which", lambda name: None)
        path = "/nonexistent/dir/weirdcmd"
        assert host_mod._resolve_command(path) == path


class TestSandboxOpenStreamsWiring:
    """`_open_streams` must wrap stdio children with the Landlock launcher
    when sandboxing is available, spawn unwrapped (with a one-time warning)
    when it isn't and isn't required, and refuse to spawn at all when it's
    required but unavailable. ``stdio_client`` is patched throughout so
    nothing real is ever spawned.
    """

    def _config(self, server_id: str, **overrides: Any) -> McpServerConfig:
        base: dict[str, Any] = dict(
            server_id=server_id, transport="stdio", command="npx", args=["-y", "thing"]
        )
        base.update(overrides)
        return McpServerConfig(**base)

    @pytest.mark.asyncio
    async def test_wraps_with_launcher_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lmchat.mcp.host import _LANDLOCK_LAUNCHER

        monkeypatch.setattr("lmchat.mcp.host.landlock_available", lambda: True)
        monkeypatch.delenv("LM_CHAT_MCP_REQUIRE_SANDBOX", raising=False)

        captured: dict[str, Any] = {}

        def fake_stdio_client(params: Any, **kwargs: Any) -> _FakeStdioCM:
            captured["params"] = params
            return _FakeStdioCM()

        host = McpHost(config_path=None)
        config = self._config("srv-wrap")

        with patch("lmchat.mcp.host.stdio_client", side_effect=fake_stdio_client):
            async with AsyncExitStack() as stack:
                await host._open_streams(config, stack)

        params = captured["params"]
        assert params.command == sys.executable
        assert params.args == [_LANDLOCK_LAUNCHER, "--", "npx", "-y", "thing"]

    @pytest.mark.asyncio
    async def test_sandbox_allow_paths_become_allow_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lmchat.mcp.host import _LANDLOCK_LAUNCHER

        monkeypatch.setattr("lmchat.mcp.host.landlock_available", lambda: True)
        monkeypatch.delenv("LM_CHAT_MCP_REQUIRE_SANDBOX", raising=False)

        captured: dict[str, Any] = {}

        def fake_stdio_client(params: Any, **kwargs: Any) -> _FakeStdioCM:
            captured["params"] = params
            return _FakeStdioCM()

        host = McpHost(config_path=None)
        config = self._config("srv-allow", sandbox_allow=["/data/served"])

        with patch("lmchat.mcp.host.stdio_client", side_effect=fake_stdio_client):
            async with AsyncExitStack() as stack:
                await host._open_streams(config, stack)

        params = captured["params"]
        assert params.args == [
            _LANDLOCK_LAUNCHER, "--allow", "/data/served", "--", "npx", "-y", "thing"
        ]

    @pytest.mark.asyncio
    async def test_unwrapped_when_unavailable_and_not_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("lmchat.mcp.host.landlock_available", lambda: False)
        monkeypatch.delenv("LM_CHAT_MCP_REQUIRE_SANDBOX", raising=False)

        captured: dict[str, Any] = {}

        def fake_stdio_client(params: Any, **kwargs: Any) -> _FakeStdioCM:
            captured["params"] = params
            return _FakeStdioCM()

        host = McpHost(config_path=None)
        config = self._config("srv-unwrapped")

        with patch("lmchat.mcp.host.stdio_client", side_effect=fake_stdio_client):
            async with AsyncExitStack() as stack:
                await host._open_streams(config, stack)

        params = captured["params"]
        assert params.command == "npx"
        assert params.args == ["-y", "thing"]

    @pytest.mark.asyncio
    async def test_require_sandbox_raises_when_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("lmchat.mcp.host.landlock_available", lambda: False)
        monkeypatch.setenv("LM_CHAT_MCP_REQUIRE_SANDBOX", "1")

        host = McpHost(config_path=None)
        config = self._config("srv-required")

        with patch("lmchat.mcp.host.stdio_client") as mock_stdio_client:
            async with AsyncExitStack() as stack:
                with pytest.raises(RuntimeError, match="LM_CHAT_MCP_REQUIRE_SANDBOX"):
                    await host._open_streams(config, stack)
        mock_stdio_client.assert_not_called()


# ---------------------------------------------------------------------------
# Connect-error surfacing: real stderr tail beats a bare "Connection closed"
# ---------------------------------------------------------------------------


class TestConnectErrorFormatting:
    """Unit coverage of the stderr-tail-capture helpers."""

    def test_read_stderr_tail_none_buffer(self) -> None:
        assert _read_stderr_tail(None) == ""

    def test_read_stderr_tail_reads_and_trims(self) -> None:
        buf = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            buf.write("line one\nline two\n")
            assert _read_stderr_tail(buf) == "line one\nline two"
        finally:
            buf.close()

    def test_format_connect_error_without_stderr(self) -> None:
        msg = _format_connect_error(ValueError("bad config"), None)
        assert msg == "ValueError: bad config"

    def test_format_connect_error_with_stderr_tail(self) -> None:
        buf = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            buf.write("serverUrl: Required\n")
            msg = _format_connect_error(RuntimeError("Connection closed"), buf)
        finally:
            buf.close()
        assert "RuntimeError: Connection closed" in msg
        assert "serverUrl: Required" in msg


class TestConnectErrorSurfacing:
    """End-to-end: a stdio child that crashes and prints to stderr must have
    its real crash reason land in ``McpHost.last_error``, not just a bare SDK
    failure like "Connection closed" — this is the searxng-mcp scenario from
    the field (a server that dies on startup printing "serverUrl: Required").
    """

    @pytest.mark.asyncio
    async def test_last_error_captures_child_stderr_on_crash(self) -> None:
        cfg = McpServerConfig(
            server_id="crashy",
            transport="stdio",
            command="sh",
            args=["-c", "echo 'serverUrl: Required' 1>&2; exit 1"],
        )
        host = McpHost(config_path=None, extra_configs=[cfg], connect_timeout_sec=5.0)

        ok = await host.connect("crashy")
        assert ok is False
        assert "crashy" not in host.connected_server_ids

        detail = host.last_error("crashy")
        assert detail is not None
        assert "serverUrl: Required" in detail

    @pytest.mark.asyncio
    async def test_last_error_none_before_any_attempt(self) -> None:
        host = McpHost(config_path=None)
        assert host.last_error("never-connected") is None

    @pytest.mark.asyncio
    async def test_record_credential_error_sets_last_error(self) -> None:
        """Silent-failure regression: McpServerStore.list_host_configs
        calls this when a stored secret fails to decrypt, so the admin
        route's ``last_error`` field reflects it instead of the server
        silently coming back without its credentials.
        """
        host = McpHost(config_path=None)
        assert host.last_error("keyless-server") is None

        message = "Credential decryption failed for API_KEY — re-enter the secret."
        host.record_credential_error("keyless-server", message)

        assert host.last_error("keyless-server") == message
        # Recording the error never adds the server to the pool/config
        # registry — it must not look "connected" or "configured".
        assert "keyless-server" not in host.connected_server_ids
        assert "keyless-server" not in host.configured_server_ids


# ---------------------------------------------------------------------------
# split_secrets_for_transport: stdio → env, http/sse → Authorization: Bearer
# ---------------------------------------------------------------------------


def test_split_secrets_stdio_goes_to_env() -> None:
    from lmchat.mcp.host import split_secrets_for_transport

    env, headers = split_secrets_for_transport("stdio", {"FOO": "bar"})
    assert env == {"FOO": "bar"}
    assert headers == {}


def test_split_secrets_sse_becomes_bearer_header() -> None:
    from lmchat.mcp.host import split_secrets_for_transport

    env, headers = split_secrets_for_transport("sse", {"CRAWL4AI_API_TOKEN": "tok123"})
    assert env == {}
    assert headers == {"Authorization": "Bearer tok123"}


def test_split_secrets_http_becomes_bearer_header() -> None:
    from lmchat.mcp.host import split_secrets_for_transport

    _env, headers = split_secrets_for_transport("http", {"K": "v"})
    assert headers == {"Authorization": "Bearer v"}


def test_split_secrets_sse_no_or_empty_secret_yields_no_header() -> None:
    from lmchat.mcp.host import split_secrets_for_transport

    assert split_secrets_for_transport("sse", {}) == ({}, {})
    assert split_secrets_for_transport("sse", {"K": ""}) == ({}, {})
