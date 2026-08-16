# SPDX-License-Identifier: Apache-2.0
"""MCP client host — client manager + tool discovery/translation.

Architecture
============
McpHost manages connections to MCP servers on behalf of cloud providers.
LM Studio's own MCP loop is untouched; this host is for cloud-provider
tool use only.

Server configs are bootstrapped from ``~/.lmstudio/mcp.json`` (the same
file IntegrationsService reads) so admin-configured servers are available
automatically without extra config steps.

Security
--------
Stdio children receive a *minimal* env: the MCP SDK's safe defaults
(PATH, HOME, USER, SHELL, TERM, LOGNAME) PLUS whatever the server's own
``env`` block declares.  The app process env — LM_CHAT_SECRET, database
URLs, provider API keys, and every other runtime secret — is never
inherited by stdio subprocesses.

Namespacing
-----------
Each tool name is prefixed ``<server_id>_<tool_name>`` and sanitised to
match the existing validator ``^[a-z][a-z0-9_]{2,63}$`` (same pattern
that lmstudio_streaming_client enforces for native MCP tools).  Non-
conforming characters are replaced with ``_``; names that remain out of
range after sanitisation are skipped with a warning.

Public API
----------
- ``McpHost.connect(server_id)``            – lazy connect one server
- ``McpHost.connect_all_configured()``      – idempotent bulk connect
- ``McpHost.list_tools(server_ids=None)``   – → list[CanonicalTool]
- ``McpHost.call_tool(namespaced_name, arguments)`` – route + execute
- ``McpHost.refresh_tools(server_id)``      – invalidate tool cache
- ``McpHost.shutdown()``                    – clean disconnect all
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import mcp.types as mcp_types
from mcp import ClientSession, stdio_client
from mcp.client.stdio import StdioServerParameters, get_default_environment

from lmchat.lmstudio.types import CanonicalTool
from lmchat.logging import get_logger
from lmchat.mcp.landlock import landlock_available

log = get_logger("lmchat.mcp.host")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default path to LM Studio's MCP config file (same as IntegrationsService).
_DEFAULT_LMSTUDIO_MCP_CONFIG: Path = Path.home() / ".lmstudio" / "mcp.json"  # noqa: E501  # allow-lmstudio-literal: read-only MCP discovery tier (same as IntegrationsService)

#: Validator matching the existing tool-name convention used by
#: lmstudio_streaming_client.
_TOOL_NAME_VALID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

#: Characters that are not [a-z0-9_] → replaced with underscore during
#: sanitisation.
_TOOL_NAME_UNSAFE_RE = re.compile(r"[^a-z0-9_]")

#: Connect timeout per server (seconds).
_CONNECT_TIMEOUT_SEC: float = 30.0

#: Call timeout per tool invocation (seconds). Mirrors
#: settings.lm_chat_mcp_tool_call_timeout_sec — a slow local tool (or one
#: that itself calls a model) shouldn't be cut at a cloud-latency number.
#: Keep this in step with that setting's default: app.py always passes the
#: configured value, so this constant is only the fallback for a directly
#: constructed McpHost, and a stale value here would reintroduce a short cap
#: on exactly the path the setting exists to keep generous.
_CALL_TIMEOUT_SEC: float = 1800.0

#: Grace period to await a session task's clean self-teardown before cancelling.
PROCESS_REAP_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class McpServerConfig:
    """Parsed configuration for one MCP server.

    Attributes:
        server_id:  Stable identifier (the key from ``mcpServers``).
        transport:  ``"stdio"`` or ``"http"`` / ``"sse"``.
        command:    Executable name for stdio transport (e.g. ``"npx"``).
        args:       CLI arguments for the stdio command.
        env:        Extra env vars declared by the server config.  These
                    ALONE (plus PATH/HOME) are passed to the child process.
        url:        Endpoint URL for HTTP/SSE transport.
        headers:    Extra HTTP headers for HTTP/SSE transport (e.g. an
                    ``Authorization: Bearer <token>`` built from a secret).
                    Ignored for stdio.
        sandbox_allow: Extra filesystem paths to grant the Landlock sandbox
                    read+exec access to, beyond the launcher's own default
                    allow-list (e.g. a filesystem-type server's serve root).
                    Ignored when sandboxing isn't active. In-memory only —
                    not yet persisted to the server store/DB.
    """

    server_id: str
    transport: str  # "stdio" | "http" | "sse"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    sandbox_allow: list[str] = field(default_factory=list)


@dataclass
class _ToolListing:
    """Result of a tool-list refresh: translated tools + reverse name map."""

    tools: list[CanonicalTool]
    name_map: dict[str, str]  # namespaced name → original MCP tool name


@dataclass
class _ToolRequest:
    """A request enqueued for a session task to execute.

    ``kind`` is one of ``"call"`` (invoke a tool) or ``"list"`` (refresh the
    tool list).  The session task resolves ``future`` with the result (or an
    exception) — all MCP I/O happens inside the owning task so anyio's
    cancel scopes are never crossed.
    """

    kind: str  # "call" | "list"
    future: asyncio.Future[Any]
    tool_name: str = ""
    arguments: dict[str, Any] | None = None


@dataclass
class _ConnectedServer:
    """Runtime state for one live MCP server connection.

    The transport + session live entirely inside ``task`` (a dedicated
    asyncio.Task).  All interaction with the session goes through
    ``request_queue``; the task resolves each request's future.  This keeps
    every ``stdio_client`` / ``ClientSession`` ``async with`` context owned
    by the single task that entered it, which is what anyio's cancel scopes
    require — closing them from the host's disconnect loop (a different
    task) is what triggered the "exit a cancel scope that isn't the current
    task's" RuntimeError.
    """

    config: McpServerConfig
    task: asyncio.Task[None]
    request_queue: asyncio.Queue[_ToolRequest | None]
    tools: list[CanonicalTool] = field(default_factory=list)
    # namespaced tool name → original MCP tool name (for call routing)
    _name_map: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_child_env(server_env: dict[str, str]) -> dict[str, str]:
    """Build a minimal env for a stdio child process.

    Starts from the MCP SDK's safe-default set (PATH, HOME, USER, SHELL,
    TERM, LOGNAME on POSIX; see ``get_default_environment``), then adds
    only the server's own declared vars.  App secrets are never included.

    Args:
        server_env: The ``env`` block from the server's config entry.

    Returns:
        A dict suitable for passing as ``StdioServerParameters.env``.
    """
    base = get_default_environment()
    # Merge in the server's own declared vars — these are explicitly opted-in
    # by the admin who wrote the config, so they're allowed.
    merged = {**base, **server_env}
    return merged


# ---------------------------------------------------------------------------
# Sandbox (Landlock) — stdio children only
# ---------------------------------------------------------------------------

#: Path to the standalone sandbox launcher, sitting alongside this file.
_LANDLOCK_LAUNCHER = str(Path(__file__).with_name("landlock.py"))

#: Server IDs we've already logged an "unavailable" warning for, so a busy
#: host reconnecting the same server repeatedly doesn't spam the log.
_warned_sandbox_unavailable: set[str] = set()


def _truthy_env(name: str, default: str = "0") -> bool:
    """Parse a boolean-ish env var (``1``/``true``/``yes``/``on``, case-insensitive)."""
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class _SandboxPolicy:
    """Resolved Landlock sandboxing decision for one stdio connect attempt."""

    available: bool
    require: bool


def _resolve_sandbox_policy() -> _SandboxPolicy:
    """Resolve whether Landlock is available and whether it's required.

    Re-checked on every stdio connect rather than cached at import time:
    the check is a single cheap syscall, and recomputing keeps the decision
    unit-testable (via monkeypatching ``landlock_available``) without a
    process-wide cache to reset between tests.
    """
    return _SandboxPolicy(
        available=landlock_available(),
        require=_truthy_env("LM_CHAT_MCP_REQUIRE_SANDBOX"),
    )


def _resolve_command(command: str) -> str:
    """Resolve a stdio server's *command* for THIS runtime environment.

    Configs discovered from a mounted ``~/.lmstudio/mcp.json`` carry the
    *host's* absolute paths (e.g. ``/Users/you/.nvm/.../npx``), which don't
    exist inside a container that has its own runtime at ``/usr/bin/npx``.
    When *command* is an absolute path that isn't present here, fall back to
    its basename resolved on ``PATH``.  Existing absolute paths and bare
    command names (PATH-resolved at exec time) pass through unchanged.
    """
    if os.path.isabs(command) and not os.path.exists(command):
        resolved = shutil.which(os.path.basename(command))
        if resolved:
            return resolved
    return command


def _wrap_with_sandbox(config: McpServerConfig) -> tuple[str, list[str]]:
    """Build the Landlock-launcher-wrapped ``(command, args)`` for *config*.

    The child is re-pointed at ``sys.executable`` running the launcher,
    which applies a default-deny Landlock ruleset (plus any of the config's
    own ``sandbox_allow`` paths) and then execs the server's real command
    (resolved for this environment via :func:`_resolve_command`) — see
    ``lmchat.mcp.landlock`` for the ruleset itself.
    """
    allow_flags = [
        flag for path in config.sandbox_allow for flag in ("--allow", path)
    ]
    wrapped_args = [
        _LANDLOCK_LAUNCHER, *allow_flags, "--",
        _resolve_command(config.command), *config.args,
    ]
    return sys.executable, wrapped_args


#: Bound on how much of a crashed stdio child's stderr we keep for last_error.
_STDERR_TAIL_MAX_CHARS = 4000


def _read_stderr_tail(errbuf: TextIO | None) -> str:
    """Read the trailing ``_STDERR_TAIL_MAX_CHARS`` of a captured stderr buffer."""
    if errbuf is None:
        return ""
    try:
        errbuf.seek(0)
        data = errbuf.read()
    except (OSError, ValueError):
        return ""
    data = data.strip()
    return data[-_STDERR_TAIL_MAX_CHARS:] if len(data) > _STDERR_TAIL_MAX_CHARS else data


def _format_connect_error(exc: BaseException, errbuf: TextIO | None) -> str:
    """Combine an exception's message with a trimmed stderr tail, if any.

    The MCP SDK's own failure text (e.g. "Connection closed") rarely says
    *why* — the useful detail is usually on the crashed server's own stderr
    (e.g. "serverUrl: Required").  Appending it here is what turns a bare
    "connect returned False" into something an admin can act on.
    """
    message = f"{type(exc).__name__}: {exc}"
    tail = _read_stderr_tail(errbuf)
    if tail:
        message = f"{message}\nstderr: {tail}"
    return message


def split_secrets_for_transport(
    transport: str, secrets: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Route decrypted secrets to ``(env, headers)`` by transport.

    stdio servers receive secrets as child-process env vars.  http/sse
    servers have no child process, so the first non-empty secret is sent as
    an ``Authorization: Bearer`` header — the near-universal remote-MCP auth
    scheme (e.g. a self-hosted Crawl4AI token).  A server needing a different
    scheme or multiple custom headers is future work; today one bearer token
    covers the catalog's remote entries.

    Args:
        transport: ``"stdio"`` | ``"http"`` | ``"sse"``.
        secrets:   Decrypted ``{env_key: value}`` dict.

    Returns:
        ``(env, headers)`` — for stdio, env is populated and headers empty;
        for http/sse, env is empty and headers carry the bearer token.
    """
    if transport in ("http", "sse"):
        for value in secrets.values():
            if value:
                return {}, {"Authorization": f"Bearer {value}"}
        return {}, {}
    return dict(secrets), {}


def _sanitise_name(raw: str) -> str:
    """Lowercase + replace non-[a-z0-9_] with ``_``."""
    return _TOOL_NAME_UNSAFE_RE.sub("_", raw.lower())


def _make_namespaced_name(server_id: str, tool_name: str) -> str | None:
    """Produce ``<server_id>_<tool_name>`` in snake_case.

    De-duplication: many MCP servers already prefix their tool names with
    the server id (e.g. firecrawl exposes ``firecrawl_search``, deepwiki
    exposes ``deepwiki_fetch``).  Re-prefixing those would double up to
    ``firecrawl_firecrawl_search`` — diverging from LM Studio's convention.
    So when the (sanitised) tool name already starts with the (sanitised)
    server id, we use the tool's own name as-is instead of re-prefixing.
    Servers whose tools are NOT self-prefixed (e.g. context7's
    ``query_docs``) still get the ``<server>_<tool>`` prefix.

    Returns ``None`` if the result doesn't satisfy the validator (skipped
    with a warning at the call site).
    """
    server_part = _sanitise_name(server_id)
    tool_part = _sanitise_name(tool_name)

    # Self-prefixed: tool name already begins with the server id (either
    # exactly ``<server>`` followed by ``_``, or the bare ``<server>``).
    # Use the tool's own name rather than doubling the prefix.
    if tool_part == server_part or tool_part.startswith(f"{server_part}_"):
        candidate = tool_part
    else:
        candidate = f"{server_part}_{tool_part}"

    if _TOOL_NAME_VALID_RE.fullmatch(candidate):
        return candidate
    # Try trimming to 64 chars and recheck.
    candidate = candidate[:64]
    if _TOOL_NAME_VALID_RE.fullmatch(candidate):
        return candidate
    return None


def _translate_tool(server_id: str, mcp_tool: mcp_types.Tool) -> CanonicalTool | None:
    """Translate one MCP ``Tool`` → ``CanonicalTool``.

    The MCP ``inputSchema`` is already a JSON Schema dict, which is exactly
    what ``CanonicalTool.parameters`` expects (``_tool_to_compat`` in
    ``compat.py`` forwards it as-is to the OpenAI function-call envelope).

    Args:
        server_id:  The server's stable ID (used for namespacing).
        mcp_tool:   The raw MCP tool from ``list_tools()``.

    Returns:
        A ``CanonicalTool`` or ``None`` if the name can't be sanitised.
    """
    namespaced = _make_namespaced_name(server_id, mcp_tool.name)
    if namespaced is None:
        log.warning(
            "mcp.host.tool_name_invalid",
            server_id=server_id,
            raw_name=mcp_tool.name,
            hint="Skipping: cannot produce a valid <server>_<tool> name.",
        )
        return None

    return CanonicalTool(
        name=namespaced,
        description=mcp_tool.description or "",
        parameters=mcp_tool.input_schema,
    )


def _parse_mcp_json(path: Path | None) -> list[McpServerConfig]:
    """Parse ``~/.lmstudio/mcp.json`` → list of ``McpServerConfig``.

    Mirrors ``IntegrationsService._read_local_mcp_config_values`` but also
    extracts command/args/env/url so we can actually launch the servers.

    Silently returns ``[]`` on any I/O or parse error.

    Args:
        path: Path to the MCP JSON config file, or ``None`` to skip.

    Returns:
        Parsed server configs, one per ``mcpServers`` key.
    """
    if path is None:
        return []
    try:
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.info("mcp.host.config_read_failed", error=str(exc))
        return []
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.info("mcp.host.config_parse_failed", error=str(exc))
        return []

    if not isinstance(doc, dict):
        return []
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        return []

    configs: list[McpServerConfig] = []
    for name, entry in servers.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        server_id = name.strip()
        if not server_id:
            continue

        url = entry.get("url", "")
        command = entry.get("command", "")
        args = entry.get("args", [])
        env_raw = entry.get("env", {})

        if not isinstance(args, list):
            args = []
        if not isinstance(env_raw, dict):
            env_raw = {}

        # Coerce env values to str; skip non-str values.
        env: dict[str, str] = {
            k: str(v)
            for k, v in env_raw.items()
            if isinstance(k, str)
        }

        if url:
            transport = "http"
        else:
            transport = "stdio"

        configs.append(
            McpServerConfig(
                server_id=server_id,
                transport=transport,
                command=str(command) if command else "",
                args=[str(a) for a in args],
                env=env,
                url=str(url) if url else "",
            )
        )

    return configs


# ---------------------------------------------------------------------------
# McpHost
# ---------------------------------------------------------------------------


class McpHost:
    """Native MCP client host for cloud-provider tool use.

    Manages a pool of MCP server connections keyed by ``server_id``.
    Connections are lazy: servers are NOT spawned at construction or at
    ``connect_all_configured()`` — they are spawned on the first explicit
    ``await host.connect(server_id)`` call or on ``connect_all_configured()``.
    A failed connect is non-fatal; the host keeps running.

    The host does NOT touch LM Studio's MCP path.  That path is handled
    server-side by LM Studio itself.

    Args:
        config_path: Path to ``~/.lmstudio/mcp.json``.  Pass ``None`` to
                     disable file discovery (useful in tests).
        extra_configs: Additional server configs to register alongside any
                       parsed from the file (e.g. injected by tests or
                       a future MCP-store feature).
        connect_timeout_sec: Per-server connection timeout.
        call_timeout_sec: Per-tool-call timeout.
    """

    def __init__(
        self,
        config_path: Path | None = _DEFAULT_LMSTUDIO_MCP_CONFIG,
        extra_configs: list[McpServerConfig] | None = None,
        *,
        connect_timeout_sec: float = _CONNECT_TIMEOUT_SEC,
        call_timeout_sec: float = _CALL_TIMEOUT_SEC,
    ) -> None:
        self._config_path = config_path
        self._connect_timeout = connect_timeout_sec
        self._call_timeout = call_timeout_sec

        # Build the config registry: file-discovered + extras.
        file_configs = _parse_mcp_json(config_path)
        all_configs = file_configs + (extra_configs or [])
        self._configs: dict[str, McpServerConfig] = {
            c.server_id: c for c in all_configs
        }

        # Pool of live connections, keyed by server_id.
        self._pool: dict[str, _ConnectedServer] = {}
        # Per-server connect lock to prevent duplicate parallel connects.
        self._connect_locks: dict[str, asyncio.Lock] = {}
        # Most recent connect/session failure detail, keyed by server_id.
        # Cleared on a subsequent successful connect; kept after disconnect
        # so the last failure remains visible until the next attempt.
        self._last_errors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Config inspection
    # ------------------------------------------------------------------

    @property
    def configured_server_ids(self) -> list[str]:
        """All server IDs parsed from the config (connected or not)."""
        return list(self._configs.keys())

    @property
    def connected_server_ids(self) -> list[str]:
        """Server IDs that have an active connection in the pool."""
        return list(self._pool.keys())

    def last_error(self, server_id: str) -> str | None:
        """Most recent connect/session failure detail for *server_id*, if any.

        ``None`` means either the server has never failed to connect, or it
        has never been attempted at all — callers that need to distinguish
        those cases should check ``connected_server_ids`` too.
        """
        return self._last_errors.get(server_id)

    def record_credential_error(self, server_id: str, message: str) -> None:
        """Mark *server_id* as failing to load its stored credentials.

        Called by rehydration (``McpServerStore.list_host_configs``) when
        a server's stored secret could not be decrypted — wrong/rotated
        ``LM_CHAT_SECRET``, corrupt ciphertext, etc.  The server is
        deliberately left OUT of ``_configs`` for this reason (never
        rehydrated keyless), so ``GET /api/mcp-store/servers`` shows the
        admin ``connected: false`` + this ``last_error`` instead of a
        silently healthy-looking, tool-less server.

        Uses the same ``_last_errors`` store as connect-time failures, so it
        is read by the same ``last_error()`` accessor the admin route already
        exposes; it is only replaced by a later ``_last_errors`` write (e.g.
        once the admin re-installs the server with a working secret and a
        connect is attempted).
        """
        self._last_errors[server_id] = message

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, server_id: str) -> bool:
        """Connect to *server_id*, idempotent.

        If the server is already connected, returns ``True`` immediately.
        If the server is unknown, logs a warning and returns ``False``.
        Connection errors are caught, logged, and return ``False`` — the
        host stays up regardless of individual server failures.

        Args:
            server_id: The server to connect.

        Returns:
            ``True`` if connected (new or existing), ``False`` on failure.
        """
        if server_id in self._pool:
            return True

        config = self._configs.get(server_id)
        if config is None:
            log.warning("mcp.host.connect_unknown_server", server_id=server_id)
            return False

        # One concurrent connect per server.
        lock = self._connect_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            # Re-check inside the lock (another task may have connected).
            if server_id in self._pool:
                return True
            return await self._do_connect(config)

    async def connect_all_configured(self) -> dict[str, bool]:
        """Attempt to connect every configured server, idempotent.

        Runs connects concurrently.  Returns a mapping of
        ``server_id → True/False`` for each configured server.
        """
        results = await asyncio.gather(
            *(self.connect(sid) for sid in self._configs),
            return_exceptions=False,
        )
        return dict(zip(self._configs.keys(), results, strict=True))

    async def _do_connect(self, config: McpServerConfig) -> bool:
        """Spawn the dedicated session task for *config* and wait for ready.

        The transport + ``ClientSession`` are opened *inside* the task
        (``_session_task``), never here, so their anyio cancel scopes stay
        owned by the task that entered them.  We only wait on a ready
        future the task resolves once the handshake + first tool-list
        complete.  A failed/timed-out handshake leaves nothing in the pool.
        """
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[_ToolListing] = loop.create_future()
        request_queue: asyncio.Queue[_ToolRequest | None] = asyncio.Queue()

        task = asyncio.create_task(
            self._session_task(config, ready, request_queue),
            name=f"mcp_session_{config.server_id}",
        )

        try:
            listing = await asyncio.wait_for(ready, timeout=self._connect_timeout)
        except TimeoutError:
            # _session_task hasn't necessarily hit its own except block yet
            # (it may just be hanging), so record a timeout-specific detail
            # rather than relying on it to have populated _last_errors.
            self._last_errors[config.server_id] = (
                f"TimeoutError: connect timed out after {self._connect_timeout:.0f}s"
            )
            log.warning(
                "mcp.host.connect_timeout",
                server_id=config.server_id,
                timeout_sec=self._connect_timeout,
            )
            await self._abort_task(task, request_queue)
            return False
        except Exception as exc:  # noqa: BLE001
            # _session_task's except block records the detailed (message +
            # stderr tail) version before propagating via `ready` — fall
            # back to the bare exception only if that never happened.
            detail = self._last_errors.setdefault(
                config.server_id, f"{type(exc).__name__}: {exc}"
            )
            log.warning(
                "mcp.host.connect_error",
                server_id=config.server_id,
                error=detail,
            )
            await self._abort_task(task, request_queue)
            return False

        conn = _ConnectedServer(
            config=config,
            task=task,
            request_queue=request_queue,
        )
        conn.tools = listing.tools
        conn._name_map = listing.name_map
        self._pool[config.server_id] = conn
        log.info(
            "mcp.host.connected",
            server_id=config.server_id,
            transport=config.transport,
            tool_count=len(conn.tools),
        )
        return True

    async def _session_task(
        self,
        config: McpServerConfig,
        ready: asyncio.Future[_ToolListing],
        request_queue: asyncio.Queue[_ToolRequest | None],
    ) -> None:
        """Own one MCP connection for its entire lifetime.

        This coroutine enters the transport + ``ClientSession`` contexts and
        services requests from *request_queue* until a ``None`` sentinel is
        received, at which point it falls out of the ``async with`` blocks —
        closing them in THIS task's own cancel scope (the anyio requirement).

        The handshake result (the initial tool listing) is delivered through
        *ready*.  If the handshake fails, *ready* receives the exception and
        the task exits.  After ready resolves successfully, errors servicing
        individual requests are reported on each request's own future and do
        not tear down the connection.

        For stdio transports, the child's stderr is captured to a bounded
        buffer for the lifetime of this task (outside the AsyncExitStack, so
        it's still readable in the except block below after the stack has
        unwound) — that's what lets a crash detail like "serverUrl: Required"
        surface instead of a bare "Connection closed".
        """
        errbuf: TextIO | None = None
        if config.transport == "stdio":
            errbuf = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            async with AsyncExitStack() as stack:
                read_stream, write_stream = await self._open_streams(config, stack, errbuf)
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()
                # First tool-listing is part of "ready": callers can advertise
                # tools immediately after connect() returns.
                listing = await self._list_tools_via_session(
                    config.server_id, session
                )
                if not ready.done():
                    ready.set_result(listing)
                self._last_errors.pop(config.server_id, None)

                # Service requests until the shutdown sentinel arrives.
                await self._serve_requests(config.server_id, session, request_queue)
        except Exception as exc:  # noqa: BLE001
            # Handshake (or an unexpected teardown) failed.  Surface to the
            # waiter if it's still pending; otherwise just log — the
            # connection is gone and disconnect()/shutdown() will reap it.
            self._last_errors[config.server_id] = _format_connect_error(exc, errbuf)
            if not ready.done():
                ready.set_exception(exc)
            else:
                log.warning(
                    "mcp.host.session_task_error",
                    server_id=config.server_id,
                    error=str(exc),
                )
        finally:
            if errbuf is not None:
                try:
                    errbuf.close()
                except OSError:
                    pass
            # Fail any requests still queued/in-flight so callers don't hang.
            self._drain_pending(request_queue, config.server_id)

    async def _open_streams(
        self,
        config: McpServerConfig,
        stack: AsyncExitStack,
        errbuf: TextIO | None = None,
    ) -> tuple[Any, Any]:
        """Open the transport for *config* and return ``(read, write)`` streams.

        The *stack* is the session task's own ``AsyncExitStack`` — entering
        the transport context here keeps its anyio cancel scope owned by the
        session task (the caller of this method), satisfying anyio's
        same-task-exit requirement.

        *errbuf*, when given (stdio only), is passed through to
        ``stdio_client`` as ``errlog`` so the child's stderr is captured
        instead of going to the host process's own stderr — read back by
        the caller on failure to build a useful ``last_error``.
        """
        if config.transport == "stdio":
            if not config.command:
                raise ValueError(
                    f"Server '{config.server_id}' has transport=stdio but no command."
                )

            # Landlock sandboxing: confine the child to a default-deny
            # filesystem view before it ever runs (see lmchat.mcp.landlock).
            # Re-resolved per connect attempt rather than cached at import —
            # see `_resolve_sandbox_policy`.
            policy = _resolve_sandbox_policy()
            if policy.available:
                command, args = _wrap_with_sandbox(config)
                log.info("mcp.sandbox.applied", server_id=config.server_id)
            elif policy.require:
                raise RuntimeError(
                    "MCP sandbox required (LM_CHAT_MCP_REQUIRE_SANDBOX=1) but "
                    "Landlock is unavailable on this kernel"
                )
            else:
                command, args = _resolve_command(config.command), config.args
                if config.server_id not in _warned_sandbox_unavailable:
                    log.warning("mcp.sandbox.unavailable", server_id=config.server_id)
                    _warned_sandbox_unavailable.add(config.server_id)

            params = StdioServerParameters(
                command=command,
                args=args,
                # Pass only the minimal env — never inherit the app env.
                env=_build_child_env(config.env),
            )
            stdio_kwargs: dict[str, Any] = {}
            if errbuf is not None:
                stdio_kwargs["errlog"] = errbuf
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params, **stdio_kwargs)
            )
            return read_stream, write_stream

        if config.transport in ("http", "sse"):
            if not config.url:
                raise ValueError(
                    f"Server '{config.server_id}' has transport={config.transport} "
                    "but no url."
                )
            headers = config.headers or None
            if config.transport == "sse":
                from mcp.client.sse import sse_client  # noqa: PLC0415

                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(config.url, headers=headers)
                )
                return read_stream, write_stream

            from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415
            from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: PLC0415

            # mcp 2.0: streamable_http_client no longer takes `headers=`
            # directly — a pre-configured httpx2.AsyncClient carries them
            # instead. Only build (and own the lifecycle of) one when the
            # server config actually sets custom headers; otherwise let
            # the transport create its own default client.
            http_client = (
                await stack.enter_async_context(create_mcp_http_client(headers=headers))
                if headers
                else None
            )
            read_stream, write_stream = await stack.enter_async_context(
                streamable_http_client(config.url, http_client=http_client)
            )
            return read_stream, write_stream

        raise ValueError(
            f"Server '{config.server_id}': unknown transport '{config.transport}'."
        )

    async def _serve_requests(
        self,
        server_id: str,
        session: ClientSession,
        request_queue: asyncio.Queue[_ToolRequest | None],
    ) -> None:
        """Loop servicing requests for one session until the sentinel arrives.

        A ``None`` item is the shutdown sentinel: it ends the loop, the
        caller falls out of the ``async with`` blocks, and the transport
        closes in this (the owning) task's scope.
        """
        while True:
            req = await request_queue.get()
            if req is None:
                # Shutdown sentinel — stop servicing and let contexts close.
                return
            try:
                if req.kind == "list":
                    result: Any = await self._list_tools_via_session(server_id, session)
                elif req.kind == "call":
                    result = await session.call_tool(req.tool_name, req.arguments)
                else:  # pragma: no cover - defensive
                    raise ValueError(f"Unknown request kind: {req.kind!r}")
            except Exception as exc:  # noqa: BLE001
                if not req.future.done():
                    req.future.set_exception(exc)
            else:
                if not req.future.done():
                    req.future.set_result(result)

    async def _list_tools_via_session(
        self,
        server_id: str,
        session: ClientSession,
    ) -> _ToolListing:
        """Call ``list_tools`` on *session* and translate to CanonicalTools.

        Returns both the translated tools and the namespaced→original-MCP-name
        map, built here from the authoritative ``mcp_tool.name`` (so the
        reverse map is correct even under the de-dup rule, where the
        namespaced name may equal the original MCP name).
        """
        result: mcp_types.ListToolsResult = await session.list_tools()
        canonical: list[CanonicalTool] = []
        name_map: dict[str, str] = {}
        for mcp_tool in result.tools:
            ct = _translate_tool(server_id, mcp_tool)
            if ct is None:
                continue
            canonical.append(ct)
            name_map[ct.name] = mcp_tool.name
        return _ToolListing(tools=canonical, name_map=name_map)

    

    @staticmethod
    def _drain_pending(
        request_queue: asyncio.Queue[_ToolRequest | None],
        server_id: str,
    ) -> None:
        """Fail every still-queued request so no caller hangs after teardown."""
        while True:
            try:
                req = request_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if req is None:
                continue
            if not req.future.done():
                req.future.set_exception(
                    RuntimeError(
                        f"MCP server '{server_id}' disconnected before the "
                        "request completed."
                    )
                )

    async def _abort_task(
        self,
        task: asyncio.Task[None],
        request_queue: asyncio.Queue[_ToolRequest | None],
    ) -> None:
        """Tear down a session task that never reached ready (or timed out).

        Sends the sentinel first so the task can exit its contexts cleanly in
        its own scope; if it doesn't stop promptly, cancels it.  Awaited so
        the transport process is fully reaped before we return.
        """
        if task.done():
            return
        try:
            request_queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover - unbounded queue
            pass
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=PROCESS_REAP_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            # Task already failed/closed — nothing more to do.
            pass

    # ------------------------------------------------------------------
    # Disconnect / shutdown
    # ------------------------------------------------------------------

    async def disconnect(self, server_id: str) -> None:
        """Disconnect one server and remove it from the pool.

        Sends the shutdown sentinel to the session task's queue and awaits
        the task.  The task falls out of its ``async with`` blocks and closes
        the transport + session **in its own task scope** — which is exactly
        what anyio requires and what the previous "aclose from the host
        loop" design violated.  No-op if the server is not connected.
        """
        conn = self._pool.pop(server_id, None)
        if conn is None:
            return
        try:
            # Sentinel → task exits _serve_requests and unwinds its contexts.
            conn.request_queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover - unbounded queue
            pass
        try:
            await asyncio.wait_for(
                asyncio.shield(conn.task), timeout=PROCESS_REAP_TIMEOUT
            )
        except (TimeoutError, asyncio.CancelledError):
            # Task didn't stop in time — cancel and reap it.
            conn.task.cancel()
            try:
                await conn.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "mcp.host.disconnect_error",
                server_id=server_id,
                error=str(exc),
            )
        log.info("mcp.host.disconnected", server_id=server_id)

    async def shutdown(self) -> None:
        """Disconnect all servers cleanly.  Safe to call multiple times."""
        server_ids = list(self._pool.keys())
        for sid in server_ids:
            await self.disconnect(sid)
        log.info("mcp.host.shutdown_complete")

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    async def _fetch_tools(self, conn: _ConnectedServer) -> None:
        """Refresh the tool cache for one connected server.

        Dispatches a ``"list"`` request to the server's session task (so the
        ``list_tools`` call happens inside the task that owns the session)
        and updates ``conn.tools`` + ``conn._name_map`` with the result.
        """
        req = _ToolRequest(
            kind="list", future=asyncio.get_running_loop().create_future()
        )
        try:
            conn.request_queue.put_nowait(req)
            listing: _ToolListing = await req.future
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "mcp.host.list_tools_error",
                server_id=conn.config.server_id,
                error=str(exc),
            )
            return

        conn.tools = listing.tools
        conn._name_map = listing.name_map

    async def refresh_tools(self, server_id: str) -> None:
        """Invalidate and reload the tool cache for one server.

        No-op if the server is not connected.
        """
        conn = self._pool.get(server_id)
        if conn is None:
            log.debug("mcp.host.refresh_tools_not_connected", server_id=server_id)
            return
        await self._fetch_tools(conn)
        log.info(
            "mcp.host.tools_refreshed",
            server_id=server_id,
            tool_count=len(conn.tools),
        )

    def list_tools(
        self,
        server_ids: list[str] | None = None,
    ) -> list[CanonicalTool]:
        """Return the cached tool list for connected servers.

        Args:
            server_ids: If given, restrict to these servers.  Unknown or
                        disconnected IDs are silently ignored.  If ``None``,
                        returns tools from all connected servers.

        Returns:
            Aggregated list of ``CanonicalTool`` in server connection order.
        """
        pool_ids = (
            [sid for sid in server_ids if sid in self._pool]
            if server_ids is not None
            else list(self._pool.keys())
        )
        tools: list[CanonicalTool] = []
        for sid in pool_ids:
            tools.extend(self._pool[sid].tools)
        return tools

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        namespaced_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute a tool by its namespaced name.

        Resolves *namespaced_name* back to the owning server + underlying MCP
        tool name, then dispatches a ``"call"`` request to that server's
        session task (the ``call_tool`` runs inside the task that owns the
        session, never here).  Errors are non-fatal: they are logged and a
        descriptive error string is returned.

        Args:
            namespaced_name: The ``<server>_<tool>`` name as returned by
                             ``list_tools()``.
            arguments: Optional JSON-serialisable argument dict.

        Returns:
            The tool result as a plain string (text content concatenated,
            or JSON-serialised structured content if available).
        """
        # Find which server owns this namespaced name.
        owner_id, orig_name = self._resolve_tool(namespaced_name)
        if owner_id is None or orig_name is None:
            msg = f"Tool '{namespaced_name}' not found in any connected server."
            log.warning("mcp.host.call_tool_not_found", name=namespaced_name)
            return f"[mcp_error] {msg}"

        conn = self._pool.get(owner_id)
        if conn is None:
            # Server was disconnected between discovery and call.
            msg = f"Server '{owner_id}' disconnected before call."
            log.warning("mcp.host.call_tool_server_gone", server_id=owner_id)
            return f"[mcp_error] {msg}"

        req = _ToolRequest(
            kind="call",
            future=asyncio.get_running_loop().create_future(),
            tool_name=orig_name,
            arguments=arguments,
        )
        try:
            conn.request_queue.put_nowait(req)
            result: mcp_types.CallToolResult = await asyncio.wait_for(
                req.future, timeout=self._call_timeout
            )
        except TimeoutError:
            msg = f"Tool '{namespaced_name}' timed out after {self._call_timeout}s."
            log.warning(
                "mcp.host.call_tool_timeout",
                name=namespaced_name,
                timeout_sec=self._call_timeout,
            )
            return f"[mcp_error] {msg}"
        except Exception as exc:  # noqa: BLE001
            msg = f"Tool '{namespaced_name}' raised: {exc}"
            log.warning("mcp.host.call_tool_error", name=namespaced_name, error=str(exc))
            return f"[mcp_error] {msg}"

        if result.is_error:
            # MCP-level tool error — surface as a string so the caller can emit a
            # warning event without crashing the loop.
            content_str = _extract_content_text(result.content)
            msg = f"Tool '{namespaced_name}' returned an MCP error: {content_str}"
            log.warning(
                "mcp.host.call_tool_mcp_error", name=namespaced_name, detail=content_str
            )
            return f"[mcp_error] {msg}"

        # Prefer structuredContent (richer) when available; else text.
        if result.structured_content is not None:
            return json.dumps(result.structured_content)
        return _extract_content_text(result.content)

    def _resolve_tool(self, namespaced_name: str) -> tuple[str | None, str | None]:
        """Return ``(server_id, original_mcp_name)`` for a namespaced name."""
        for sid, conn in self._pool.items():
            orig = conn._name_map.get(namespaced_name)
            if orig is not None:
                return sid, orig
        return None, None

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"McpHost(configured={len(self._configs)}, "
            f"connected={len(self._pool)})"
        )


# ---------------------------------------------------------------------------
# Content extraction helpers
# ---------------------------------------------------------------------------


def _extract_content_text(content: list[mcp_types.ContentBlock]) -> str:
    """Concatenate text from MCP content blocks."""
    parts: list[str] = []
    for block in content:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        elif isinstance(block, mcp_types.ImageContent):
            parts.append(f"[image: {block.mime_type}]")
        else:
            # AudioContent, ResourceLink, EmbeddedResource — represent as
            # a placeholder; the tool result is primarily textual.
            parts.append(f"[{type(block).__name__}]")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Convenience re-export for type stubs
# ---------------------------------------------------------------------------

__all__ = [
    "McpHost",
    "McpServerConfig",
    "_build_child_env",
    "_make_namespaced_name",
    "_parse_mcp_json",
    "_translate_tool",
]
