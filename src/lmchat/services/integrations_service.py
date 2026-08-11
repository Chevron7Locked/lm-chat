# SPDX-License-Identifier: Apache-2.0
"""Admin-supplied MCP integrations list service.

Closes the integrations picker gap and mitigates the risk of a stale
integrations list.

WHY this exists
---------------
LM Studio does not expose an HTTP endpoint for listing configured MCP
servers (``/api/v1/integrations``, ``/api/v1/mcp``, etc. all return 404 —
all candidate endpoints return 404).
The only path to MCP server enumeration is the LM Studio desktop UI.

This service provides a workaround: the admin manually maintains the list
of available integration IDs (e.g. ``mcp/searxng``, ``mcp/filesystem``).
The list is stored in one of two ways (DB wins if both are present):

1. **Environment variable** (deployment-baked): ``LM_CHAT_AVAILABLE_INTEGRATIONS``
   as a comma-separated string, parsed into ``Settings.lm_chat_available_integrations``.
2. **DB table** ``mcp_integrations_list`` (admin-editable via the Admin UI):
   takes precedence when any rows exist.

Sync workflow
-------------
When the admin installs or removes an MCP server in LM Studio's desktop UI,
they must also update lm-chat's list (either by editing the env var in the
deployment config or by using ``PUT /api/integrations/available`` via the
Admin: Integrations page). See docs/deployment.md "MCP integrations list".
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.logging import get_logger

log = get_logger(__name__)

# Default location of LM Studio's MCP config file on a same-host
# deployment. LM Studio writes it; we only read it. The wipe-incident
# rule (feedback_lm_chat_no_lm_studio_state_management_2026_05_21) is
# about WRITES — read-only consumption is fine and is how we provide
# zero-config discovery when same-host. Override via the constructor
# arg in tests or to disable file discovery entirely (pass None).
#
# The check_no_lmstudio_fs gate requires the `allow-lmstudio-literal:` marker
# to sit on the SAME line as the `.lmstudio` reference, which pushes the line
# past the 100-col limit — hence the paired E501 suppression. Do NOT move the
# marker onto a preceding comment line; the gate only honours it inline.
_DEFAULT_LMSTUDIO_MCP_CONFIG: Final[Path] = Path.home() / ".lmstudio" / "mcp.json"  # noqa: E501  # allow-lmstudio-literal: read-only MCP discovery tier (HTTP→file→manual fallback)

# Reuse the IntegrationSetEntry allowlist regex so the file-discovered
# IDs go through the same sanity gate as admin-written values. mcp.json
# is a local file but it's still untrusted input — somebody could land
# an arbitrary string in mcpServers and we don't want to leak it into
# downstream payloads unchecked.
_INTEGRATION_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_\-./:]{1,256}")

# ---------------------------------------------------------------------------
# Table definition (mirrors migration 0009)
# ---------------------------------------------------------------------------

_TABLE_NAME: Final[str] = "mcp_integrations_list"

_table = sa.Table(
    _TABLE_NAME,
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("value", sa.String(255), nullable=False, unique=True),
    sa.Column("sort_order", sa.Integer, nullable=False, default=0),
    # Admin-set default-seed flag (migration 0014).
    sa.Column(
        "enabled_by_default",
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
        default=0,
    ),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class IntegrationEntry(BaseModel):
    """Wire shape for a single integration list entry (response)."""

    model_config = ConfigDict(from_attributes=False)

    id: int
    value: str
    sort_order: int
    # Admin-set default-seed flag.  When True, the chat composer
    # pre-selects this integration on a fresh chat session.  When False,
    # the integration is available but opt-in (the user must toggle it
    # on per-message via the composer chip-row).  Defaults to False for
    # env-default entries that have never been admin-edited.
    enabled_by_default: bool = False
    created_at: datetime
    updated_at: datetime
    # Which MCP system this entry belongs to: "lmstudio" = LM Studio's own
    # mcp.json servers (dispatched server-side, native mode only); "store" =
    # LMChat's MCP Store (client-side agentic loop). Defaults to "lmstudio"
    # so curated entries stay correct without touching their construction
    # sites; store-sourced entries override this explicitly.
    source: Literal["lmstudio", "store"] = "lmstudio"


class IntegrationSetEntry(BaseModel):
    """Input shape for a single entry when setting the list (admin write).

    ``value`` is validated against a conservative allowlist regex that permits
    only the characters used in real MCP integration IDs (e.g.
    ``mcp/searxng``, ``lmstudio-default``).  Script tags, shell metachars,
    path traversal, null bytes, CRLF, and oversized values are all rejected
    with a Pydantic ``ValidationError`` (HTTP 422 at the route layer).

    LLM07 fix: input validation closes XSS / log-injection / smuggling
    vectors on the admin-writable integration list.
    """

    value: str
    sort_order: int = 0
    # Optional admin-set default-seed flag.  Defaults to False so
    # existing PUT callers (from before this flag existed) don't
    # accidentally flip anything on.
    enabled_by_default: bool = False

    @field_validator("value")
    @classmethod
    def _validate_value(cls, v: str) -> str:
        """Reject dangerous preset names; accept conservative allowlist only."""
        import re as _re

        if not v or not v.strip():
            raise ValueError(
                "integration value must not be empty or whitespace-only"
            )
        if len(v) > 256:
            raise ValueError(
                f"integration value must be ≤256 characters (got {len(v)})"
            )
        # Block control bytes first (null, newline, CR, tab).
        for ch in ("\x00", "\n", "\r", "\t"):
            if ch in v:
                raise ValueError(
                    "integration value must not contain control characters "
                    "(null bytes, newlines, carriage returns, or tabs)"
                )
        # Allowlist: alphanumerics + underscore, hyphen, dot, colon, slash.
        # This is deliberately conservative — it covers all known MCP ID
        # formats while blocking XSS, shell metachars, and path traversal.
        if not _re.fullmatch(r"[A-Za-z0-9_\-./:]{1,256}", v):
            raise ValueError(
                "integration value may only contain letters, digits, "
                f"and the characters _ - . / : (got {v!r})"
            )
        # Block absolute paths and traversal sequences.
        if v.startswith("/") or "../" in v:
            raise ValueError(
                "integration value must not be an absolute path or contain "
                "path traversal sequences (../)"
            )
        return v


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class IntegrationsService:
    """Manages the admin-supplied MCP integrations list.

    Three-tier precedence (highest first):

      1. **DB rows** (admin-curated via Admin → Integrations).
      2. **Local LM Studio config** — ``~/.lmstudio/mcp.json``, read
         only when no DB rows exist. Same-host deployment assumption;
         we extract the ``mcpServers`` keys and prefix them with
         ``mcp/`` to match the integration-ID format the chat payload
         uses. Auto-discovered integrations are NOT seeded with
         ``enabled_by_default=True`` by default — the user picks
         per-message (discover-but-off; see ``synthetic_enabled_by_
         default`` / ``Settings.lm_chat_default_integrations_enabled_
         by_default`` to restore pre-selection).
      3. **Env default** — ``Settings.lm_chat_available_integrations``,
         for deployments where LMChat and LM Studio run on different
         machines (mcp.json absent).

    LM Studio does not expose an HTTP MCP discovery endpoint as of
    2026-06-02 (all `/api/v1/integrations`, `/api/v1/mcp`,
    `/api/v1/mcp/servers` 404). If/when one ships, slot it in as a
    fourth tier ABOVE the file read (HTTP is more authoritative than
    a possibly-stale local file).

    Args:
        engine:           SQLAlchemy async engine (shared app engine).
        env_default:      List of integration IDs from the env var,
                          already parsed by Pydantic-Settings.
        local_mcp_config: Path to LM Studio's ``mcp.json``. Defaults to
                          ``~/.lmstudio/mcp.json``. Pass ``None`` to
                          disable file discovery (e.g. in tests).
    """

    def __init__(
        self,
        engine: AsyncEngine,
        env_default: list[str],
        local_mcp_config: Path | None = _DEFAULT_LMSTUDIO_MCP_CONFIG,
        *,
        synthetic_enabled_by_default: bool = False,
    ) -> None:
        self._engine = engine
        self._env_default = list(env_default)
        self._local_mcp_config = local_mcp_config
        # When True, synthetic (tier 2 + tier 3) entries are surfaced
        # with ``enabled_by_default=True`` so the chat composer pre-
        # selects them on a fresh session. Defaults to False (discover-
        # but-off): discovered/env servers are listed and available for
        # the user to pick per-message, but nothing is pre-armed on a
        # fresh install. DB-row tier always carries the admin-chosen
        # value, untouched by this flag.
        self._synthetic_enabled_by_default = synthetic_enabled_by_default

    async def list_available(self) -> list[IntegrationEntry]:
        """Return the current integrations list.

        If the DB table has entries, returns them sorted by ``sort_order`` ASC,
        carrying each row's :attr:`IntegrationEntry.enabled_by_default` flag.
        Otherwise returns the env-default list as synthetic ``IntegrationEntry``
        objects (id=stable negative hash, created_at/updated_at = epoch, sort
        order matches insertion order, ``enabled_by_default`` taken from
        ``self._synthetic_enabled_by_default`` — False by default, so a fresh
        install discovers servers without pre-arming any of them).

        Returns:
            List of :class:`IntegrationEntry` objects.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(_table).order_by(_table.c.sort_order.asc(), _table.c.id.asc())
                )
            ).fetchall()

        if rows:
            return [
                IntegrationEntry(
                    id=row.id,
                    value=row.value,
                    sort_order=row.sort_order,
                    enabled_by_default=bool(
                        getattr(row, "enabled_by_default", 0)
                    ),
                    created_at=_ensure_tz(row.created_at),
                    updated_at=_ensure_tz(row.updated_at),
                )
                for row in rows
            ]

        # No DB rows → try the local LM Studio config (tier 2). Returns
        # an empty list if the file is absent, unreadable, or contains
        # no valid mcpServers entries — falling through cleanly to env.
        file_values = self._read_local_mcp_config_values()
        source_values = file_values if file_values else self._env_default

        # Deduplicate while preserving order; epoch timestamps for
        # synthetic entries.
        #
        # Synthetic IDs (formerly all `-1`) now use stable negative
        # hashes of the value so the UI can sort, key React rows on
        # them, and a future PUT round-trip can carry them without
        # collisions. The PUT path still treats negative ids as
        # synthetic and replaces them with a real autoincrement id at
        # insert time; the negative space exists to distinguish
        # "saved" from "fallback" without changing the wire shape —
        # every fallback row used to carry the same `id: -1`, which
        # broke React's key uniqueness.
        seen: set[str] = set()
        result: list[IntegrationEntry] = []
        _epoch = datetime(1970, 1, 1, tzinfo=UTC)
        for i, value in enumerate(source_values):
            stripped = value.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                result.append(
                    IntegrationEntry(
                        id=_synthetic_id(stripped),
                        value=stripped,
                        sort_order=i,
                        enabled_by_default=self._synthetic_enabled_by_default,
                        created_at=_epoch,
                        updated_at=_epoch,
                    )
                )
        return result

    def live_serveable_ids(self) -> set[str]:
        """Public accessor for the LIVE ``mcp/<name>`` set LM Studio can
        actually serve right now (same-host discovery via its mcp.json).

        Thin wrapper around :meth:`_read_local_mcp_config_values`, which
        stays private (it's an implementation detail of
        :meth:`list_available`'s tier-2 fallback). This accessor is the
        sanctioned way for OTHER callers — notably the streaming routes'
        pre-flight filter (resilience-fix "Layer 2": avoid round-tripping
        to LM Studio just to learn a plugin is dead) — to consult the live
        set directly, independent of :meth:`list_available`'s DB-first
        resolution order (an admin-curated/DB-configured entry can name a
        server LM Studio itself no longer serves, e.g. a removed/renamed
        MCP server still lingering as ``enabled_by_default``).

        Returns:
            The live ``mcp/<name>`` set, or an empty set when LM Studio's
            mcp.json is absent, unreadable, not JSON, or has no
            ``mcpServers`` section (e.g. a remote/non-same-host deploy).
            Callers MUST treat an empty result as "unknown", never as
            "nothing is live" — skip filtering in that case rather than
            stripping the user's tools based on an unknown live set.
        """
        return set(self._read_local_mcp_config_values())

    @property
    def local_mcp_config_path(self) -> Path | None:
        """The mcp.json path this service's file-discovery tier reads, or
        ``None`` when file discovery is disabled. Exposed for the startup
        diagnostic so a mis-mounted/missing file surfaces in the logs."""
        return self._local_mcp_config

    def _read_local_mcp_config_values(self) -> list[str]:
        """Return ``mcp/<name>`` IDs derived from LM Studio's mcp.json.

        Same-host discovery — LM Studio writes this file; we only
        read it. Skipped silently when the file is missing, unreadable,
        not JSON, or has no ``mcpServers`` section. Only the server
        names are extracted; secrets/commands/env in the file are
        NEVER read past the keys (and never echoed downstream).

        Returns:
            Sorted list of ``mcp/<name>`` strings, or ``[]``.
        """
        path = self._local_mcp_config
        if path is None:
            return []
        try:
            if not path.is_file():
                return []
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            # Permission denied, broken symlink, etc. — log and move on.
            log.info("integrations.local_config.read_failed", error=str(exc))
            return []
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.info("integrations.local_config.parse_failed", error=str(exc))
            return []
        if not isinstance(doc, dict):
            return []
        servers = doc.get("mcpServers")
        if not isinstance(servers, dict):
            return []
        out: list[str] = []
        for name in servers.keys():
            if not isinstance(name, str):
                continue
            candidate = f"mcp/{name.strip()}"
            # Same gates as IntegrationSetEntry — local file is still
            # untrusted input. Reject control chars (already excluded
            # by the allowlist regex), path traversal sequences, and
            # absolute paths.
            if (
                _INTEGRATION_VALUE_RE.fullmatch(candidate) is None
                or "../" in candidate
                or candidate.startswith("/")
            ):
                log.info(
                    "integrations.local_config.rejected_id",
                    candidate=candidate,
                )
                continue
            out.append(candidate)
        out.sort()
        return out

    async def set_available(self, entries: list[IntegrationSetEntry]) -> list[IntegrationEntry]:
        """Atomically replace the DB list with *entries*.

        Uses a single transaction: DELETE all existing rows, INSERT new rows.
        Per-row ``enabled_by_default`` is persisted as an integer (0/1).
        Returns the new state via :meth:`list_available`.

        Args:
            entries: New list of ``{value, sort_order, enabled_by_default}``
                items.  ``enabled_by_default`` defaults to ``False`` when
                omitted by the caller, preserving backwards compatibility
                with older PUT clients that predate this flag.

        Returns:
            The new list as returned by :meth:`list_available`.
        """
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            await conn.execute(sa.delete(_table))
            if entries:
                await conn.execute(
                    sa.insert(_table),
                    [
                        {
                            "value": e.value.strip(),
                            "sort_order": e.sort_order,
                            "enabled_by_default": 1 if e.enabled_by_default else 0,
                            "created_at": now,
                            "updated_at": now,
                        }
                        for e in entries
                        if e.value.strip()
                    ],
                )
        log.info(
            "integrations_service.set_available",
            count=len(entries),
        )
        return await self.list_available()

    async def add(
        self,
        entry: str,
        *,
        sort_order: int | None = None,
        enabled_by_default: bool = False,
    ) -> IntegrationEntry:
        """Add a single integration ID to the DB list (admin convenience).

        If the value already exists, the existing row is returned unchanged.

        Args:
            entry:               Integration ID to add (e.g. ``"mcp/searxng"``).
            sort_order:          Optional position hint; defaults to max(sort_order)+1.
            enabled_by_default:  Admin-set default-seed flag; persisted
                                 as 0/1 in the DB.  Defaults to ``False``.

        Returns:
            The inserted (or existing) :class:`IntegrationEntry`.
        """
        value = entry.strip()
        now = datetime.now(UTC)

        async with self._engine.begin() as conn:
            # Check for existing row.
            existing = (
                await conn.execute(
                    sa.select(_table).where(_table.c.value == value)
                )
            ).fetchone()
            if existing is not None:
                return IntegrationEntry(
                    id=existing.id,
                    value=existing.value,
                    sort_order=existing.sort_order,
                    enabled_by_default=bool(
                        getattr(existing, "enabled_by_default", 0)
                    ),
                    created_at=_ensure_tz(existing.created_at),
                    updated_at=_ensure_tz(existing.updated_at),
                )

            if sort_order is None:
                max_row = (
                    await conn.execute(
                        sa.select(sa.func.max(_table.c.sort_order))
                    )
                ).scalar()
                sort_order = (max_row or 0) + 1

            result = await conn.execute(
                sa.insert(_table).values(
                    value=value,
                    sort_order=sort_order,
                    enabled_by_default=1 if enabled_by_default else 0,
                    created_at=now,
                    updated_at=now,
                ).returning(_table)
            )
            row = result.fetchone()

        assert row is not None  # insert succeeded
        return IntegrationEntry(
            id=row.id,
            value=row.value,
            sort_order=row.sort_order,
            enabled_by_default=bool(getattr(row, "enabled_by_default", 0)),
            created_at=_ensure_tz(row.created_at),
            updated_at=_ensure_tz(row.updated_at),
        )

    async def remove(self, entry: str) -> None:
        """Remove an integration ID from the DB list (admin convenience).

        No-op if the value does not exist.

        Args:
            entry: Integration ID to remove.
        """
        value = entry.strip()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.delete(_table).where(_table.c.value == value)
            )
        log.info("integrations_service.remove", value=value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_tz(dt: datetime | None) -> datetime:
    """Return *dt* with UTC timezone; replace None with epoch."""
    if dt is None:
        return datetime(1970, 1, 1, tzinfo=UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _synthetic_id(value: str) -> int:
    """Stable negative integer id derived from *value*.

    Synthetic entries (from `~/.lmstudio/mcp.json` or the env list) used
    to share a single `-1` id, which made them collide in React keying,
    UI sorts, and any future PUT round-trip that carried the id back.
    Deriving a value-keyed hash keeps:

    * stability across restarts (same `value` → same id), so the UI
      sorts and keys stay consistent
    * uniqueness within a single deployment's discovered set
    * negativity (we shift to ``-2`` minimum) so consumers can still
      distinguish "synthetic" from "DB-backed" via `id < 0`

    `-1` is reserved as the legacy synthetic sentinel; we avoid it so
    older code paths that explicitly checked ``id == -1`` keep working
    until they're migrated to ``id < 0``.
    """
    import hashlib

    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=4).digest()
    unsigned = int.from_bytes(digest, "big")
    return -2 - (unsigned % (2**31 - 2))
