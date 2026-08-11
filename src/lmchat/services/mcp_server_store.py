# SPDX-License-Identifier: Apache-2.0
"""MCP Server Store — CRUD service for the ``mcp_servers`` table.

Manages installed MCP server configurations, including encrypted secrets.

Encryption: each secret is encrypted independently with
``kind="mcp_server_secret"``, ``record_id=<row id>``, stored as a JSON list
in ``secrets_enc``. ``install()`` needs the row's own PK before it can
encrypt, so it inserts a disabled placeholder, encrypts with the generated
PK, then updates to the real enabled/secrets_enc state — all in ONE
transaction (same pattern as ``ProviderConfigService``). Nothing commits
until that final update, so no reader (including ``list_host_configs``
rehydration) can ever observe an enabled row missing its secrets.

``get()`` logs per-key decrypt failures at ERROR and omits the failed key
from ``secrets`` rather than raising. ``list_host_configs()`` — the path
that feeds the MCP child process's env — is stricter: a row with ANY
failed key is excluded entirely (reported via ``credential_errors``)
rather than rehydrated without a credential it needs.

Views: ``list_all()`` returns safe views (``secrets_set`` key names only,
no cleartext); ``get()`` returns the internal view with decrypted secrets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lmchat.mcp.host import McpServerConfig

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import mcp_servers
from lmchat.logging import get_logger
from lmchat.utils.encryption import decrypt, encrypt

log = get_logger(__name__)

_SECRET_KIND: str = "mcp_server_secret"


# ---------------------------------------------------------------------------
# View dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpServerSecretSpec:
    """Metadata describing one secret expected by an MCP server.

    Sourced from the catalog; never stores a value.
    """

    key: str
    label: str
    required: bool


@dataclass(frozen=True, slots=True)
class McpServerSafeView:
    """Safe (public) view — secret values are never included.

    ``secrets_set`` is a list of key names that have an encrypted value
    stored; the values themselves are not exposed.

    ``connected`` is not persisted; it is populated at read time by
    comparing against the live McpHost's connected_server_ids.

    ``tool_policy`` is the persisted denylist of namespaced tool names.
    Empty list means all tools are advertised (allow-all default).
    """

    id: int
    slug: str
    name: str
    transport: str
    command: str | None
    args: list[str] | None
    url: str | None
    secrets_set: list[str]
    enabled: bool
    source: str
    trust: str
    consented: bool
    connected: bool = False
    tool_policy: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class McpServerInternalView:
    """Internal view with decrypted secret values.

    ``secrets`` maps env var key names to plaintext values — pass directly
    as env dict when spawning the MCP child process.

    Keys that failed decryption are absent from ``secrets``.

    ``tool_policy`` is the persisted denylist of namespaced tool names.
    Empty list means all tools are advertised (allow-all default).
    """

    id: int
    slug: str
    name: str
    transport: str
    command: str | None
    args: list[str] | None
    url: str | None
    secrets: dict[str, str] = field(default_factory=dict)
    secrets_set: list[str] = field(default_factory=list)
    enabled: bool = True
    source: str = "byo"
    trust: str = "byo"
    consented: bool = False
    connected: bool = False
    tool_policy: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class McpServerStore:
    """Async CRUD service for the ``mcp_servers`` table.

    Constructed at application lifespan; depends only on the application
    ``AsyncEngine``. No background tasks.
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    async def install(
        self,
        *,
        slug: str,
        name: str,
        transport: str,
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
        secrets: dict[str, str] | None = None,
        source: str = "byo",
        trust: str = "byo",
        enabled: bool = True,
        tool_policy: list[str] | None = None,
    ) -> McpServerSafeView:
        """Install a new MCP server row. Install is consent — ``consented``
        is always set to ``True``.

        When *secrets* are supplied, the insert and the secrets_enc update
        happen in ONE transaction (see module docstring) so no reader ever
        observes an enabled row with no secrets_enc.

        Args:
            transport:   "stdio" | "http" | "sse".
            tool_policy: Optional denylist of namespaced tool names.
                         Null/empty ⇒ all tools advertised.

        Raises:
            ValueError: When a row with the same slug already exists.
        """
        existing_id = await self._fetch_id(slug)
        if existing_id is not None:
            raise ValueError(f"MCP server already installed: {slug!r}")

        values: dict = {
            "slug": slug,
            "name": name,
            "transport": transport,
            "command": command,
            "args": args,
            "url": url,
            "secrets_enc": None,
            "enabled": enabled,
            "source": source,
            "trust": trust,
            "consented": True,
            "tool_policy": tool_policy or None,
        }

        secrets_set: list[str]
        if not secrets:
            # values already reflects final state — no placeholder needed.
            new_id = await self._insert_row(values)
            secrets_set = []
        else:
            # KDF record_id is the row's own PK, only assigned at INSERT
            # time: insert a disabled placeholder, encrypt with the
            # generated PK, then update to the real enabled/secrets_enc
            # state — all in ONE transaction, so no reader (including
            # list_host_configs rehydration) ever observes an
            # enabled-but-keyless row.
            placeholder_values = {**values, "secrets_enc": None, "enabled": False}

            async def _insert_and_set_secrets() -> int:
                async with self._engine.begin() as conn:
                    row_id = await self._insert_row_in_conn(conn, placeholder_values)
                    secrets_enc = await self._encrypt_secrets(row_id, secrets)
                    await conn.execute(
                        update(mcp_servers)
                        .where(mcp_servers.c.id == row_id)
                        .values(secrets_enc=secrets_enc, enabled=enabled)
                    )
                return row_id

            # Safe to retry whole-closure: encrypt() uses a fresh nonce each
            # call and the prior attempt's txn was rolled back — never two
            # partial rows.
            new_id = await with_write_retry(_insert_and_set_secrets)
            secrets_set = list(secrets.keys())

        return McpServerSafeView(
            id=new_id,
            slug=slug,
            name=name,
            transport=transport,
            command=command,
            args=args,
            url=url,
            secrets_set=secrets_set,
            enabled=enabled,
            source=source,
            trust=trust,
            consented=True,
            connected=False,
            tool_policy=tool_policy or [],
        )

    async def update_enabled(self, slug: str, enabled: bool) -> McpServerSafeView | None:
        """Enable or disable an installed server.

        Returns:
            Updated :class:`McpServerSafeView` or ``None`` when not found.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                update(mcp_servers)
                .where(mcp_servers.c.slug == slug)
                .values(enabled=enabled)
            )
        return await self._get_safe(slug)

    async def update_tool_policy(
        self, slug: str, denied: list[str]
    ) -> McpServerSafeView | None:
        """Replace the tool denylist for an installed server.

        Pass an empty list to clear (allow all).

        Returns:
            Updated :class:`McpServerSafeView` or ``None`` when not found.
        """
        stored: list[str] | None = denied if denied else None
        async with self._engine.begin() as conn:
            await conn.execute(
                update(mcp_servers)
                .where(mcp_servers.c.slug == slug)
                .values(tool_policy=stored)
            )
        return await self._get_safe(slug)

    async def _get_safe(self, slug: str) -> McpServerSafeView | None:
        """Return a safe view for *slug* or ``None`` when not found."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        mcp_servers.c.id,
                        mcp_servers.c.slug,
                        mcp_servers.c.name,
                        mcp_servers.c.transport,
                        mcp_servers.c.command,
                        mcp_servers.c.args,
                        mcp_servers.c.url,
                        mcp_servers.c.secrets_enc,
                        mcp_servers.c.enabled,
                        mcp_servers.c.source,
                        mcp_servers.c.trust,
                        mcp_servers.c.consented,
                        mcp_servers.c.tool_policy,
                    ).where(mcp_servers.c.slug == slug)
                )
            ).first()

        if row is None:
            return None

        enc_list: list[dict] | None = row.secrets_enc
        secrets_set = [e["key"] for e in enc_list] if enc_list else []
        raw_policy: list | None = row.tool_policy
        return McpServerSafeView(
            id=int(row.id),
            slug=row.slug,
            name=row.name,
            transport=row.transport,
            command=row.command,
            args=row.args,
            url=row.url,
            secrets_set=secrets_set,
            enabled=bool(row.enabled),
            source=row.source,
            trust=row.trust,
            consented=bool(row.consented),
            connected=False,
            tool_policy=list(raw_policy) if raw_policy else [],
        )

    async def delete(self, slug: str) -> None:
        """Delete the row for *slug*.

        No-op when the slug does not exist.

        Args:
            slug: Installed server slug to remove.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(mcp_servers).where(mcp_servers.c.slug == slug)
            )

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    async def list_all(self) -> list[McpServerSafeView]:
        """Return safe views of all installed servers, ordered by slug.

        Secret values are never included; ``secrets_set`` lists the key
        names that have a stored value.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        mcp_servers.c.id,
                        mcp_servers.c.slug,
                        mcp_servers.c.name,
                        mcp_servers.c.transport,
                        mcp_servers.c.command,
                        mcp_servers.c.args,
                        mcp_servers.c.url,
                        mcp_servers.c.secrets_enc,
                        mcp_servers.c.enabled,
                        mcp_servers.c.source,
                        mcp_servers.c.trust,
                        mcp_servers.c.consented,
                        mcp_servers.c.tool_policy,
                    ).order_by(mcp_servers.c.slug)
                )
            ).fetchall()

        result = []
        for row in rows:
            enc_list: list[dict] | None = row.secrets_enc
            secrets_set = [e["key"] for e in enc_list] if enc_list else []
            raw_policy: list | None = row.tool_policy
            result.append(
                McpServerSafeView(
                    id=int(row.id),
                    slug=row.slug,
                    name=row.name,
                    transport=row.transport,
                    command=row.command,
                    args=row.args,
                    url=row.url,
                    secrets_set=secrets_set,
                    enabled=bool(row.enabled),
                    source=row.source,
                    trust=row.trust,
                    consented=bool(row.consented),
                    connected=False,
                    tool_policy=list(raw_policy) if raw_policy else [],
                )
            )
        return result

    async def get(self, slug: str) -> McpServerInternalView | None:
        """Return the internal view for *slug*, decrypted secrets included.

        Decryption failures are logged per-key at ERROR; the key is omitted
        from ``secrets`` rather than raising.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        mcp_servers.c.id,
                        mcp_servers.c.slug,
                        mcp_servers.c.name,
                        mcp_servers.c.transport,
                        mcp_servers.c.command,
                        mcp_servers.c.args,
                        mcp_servers.c.url,
                        mcp_servers.c.secrets_enc,
                        mcp_servers.c.enabled,
                        mcp_servers.c.source,
                        mcp_servers.c.trust,
                        mcp_servers.c.consented,
                        mcp_servers.c.tool_policy,
                    ).where(mcp_servers.c.slug == slug)
                )
            ).first()

        if row is None:
            return None

        enc_list: list[dict] | None = row.secrets_enc
        secrets_set = [e["key"] for e in enc_list] if enc_list else []
        secrets = await self._decrypt_secrets(int(row.id), enc_list)
        raw_policy: list | None = row.tool_policy

        return McpServerInternalView(
            id=int(row.id),
            slug=row.slug,
            name=row.name,
            transport=row.transport,
            command=row.command,
            args=row.args,
            url=row.url,
            secrets=secrets,
            secrets_set=secrets_set,
            enabled=bool(row.enabled),
            source=row.source,
            trust=row.trust,
            consented=bool(row.consented),
            connected=False,
            tool_policy=list(raw_policy) if raw_policy else [],
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fetch_id(self, slug: str) -> int | None:
        """Return the ``id`` of the existing row for *slug*, or None."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(mcp_servers.c.id).where(mcp_servers.c.slug == slug)
                )
            ).first()
        return int(row.id) if row else None

    async def _insert_row(self, values: dict) -> int:
        """Insert a new mcp_servers row and return the new PK.

        Opens and commits its own transaction. Use :meth:`_insert_row_in_conn`
        instead when the insert must share a transaction with a follow-up
        write (e.g. a secrets_enc UPDATE that must never be visible to a
        concurrent reader without it — see :meth:`install`).
        """
        async with self._engine.begin() as conn:
            return await self._insert_row_in_conn(conn, values)

    async def _insert_row_in_conn(self, conn: AsyncConnection, values: dict) -> int:
        """Insert a new mcp_servers row on an already-open *conn*.

        Cross-dialect: PostgreSQL uses RETURNING; SQLite uses lastrowid.
        The caller owns the transaction (begin/commit/rollback) around
        *conn* — this method neither begins nor commits.
        """
        dialect = self._engine.dialect.name
        if dialect == "postgresql":
            result = await conn.execute(
                pg_insert(mcp_servers).values(**values).returning(mcp_servers.c.id)
            )
            row = result.first()
            return int(row.id)  # type: ignore[union-attr]
        else:
            result = await conn.execute(mcp_servers.insert().values(**values))
            return int(result.lastrowid)

    async def _encrypt_secrets(
        self, row_id: int, secrets: dict[str, str]
    ) -> list[dict]:
        """Encrypt each secret value independently (row_id is the KDF
        record_id). Returns a list of ``{"key", "enc_value"}`` dicts.
        """
        return [
            {
                "key": k,
                "enc_value": encrypt(
                    v.encode(), kind=_SECRET_KIND, record_id=row_id
                ),
            }
            for k, v in secrets.items()
        ]

    async def _decrypt_secrets(
        self, row_id: int, secrets_enc: list[dict] | None
    ) -> dict[str, str]:
        """Decrypt a stored secrets_enc list.

        Logs at ERROR per failed key and skips it; does not raise. Returns
        ``{key: plaintext}`` for the keys that decrypted successfully.
        """
        if not secrets_enc:
            return {}

        result: dict[str, str] = {}
        for entry in secrets_enc:
            key = entry.get("key", "")
            enc_value = entry.get("enc_value", "")
            if not enc_value:
                continue
            try:
                result[key] = decrypt(
                    enc_value, kind=_SECRET_KIND, record_id=row_id
                ).decode()
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "mcp_server_store.secret_decrypt_failed",
                    slug_row_id=row_id,
                    key=key,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        return result

    async def list_host_configs(
        self, *, credential_errors: dict[str, str] | None = None
    ) -> list[McpServerConfig]:
        """Return host configs for all ENABLED servers, with decrypted secrets as env.

        Used at startup to rehydrate McpHost so store-installed servers
        survive restart. A row where ANY secret fails to decrypt (rotated
        ``LM_CHAT_SECRET``, corrupt ciphertext) is EXCLUDED entirely rather
        than rehydrated keyless — a keyless server would look healthy while
        its tools silently stop working. The failure is logged at ERROR
        and, when *credential_errors* is passed, recorded as
        ``{slug: message}`` so the caller can mark the server errored via
        ``McpHost.record_credential_error``.

        Returns:
            Configs for enabled rows whose secrets ALL decrypted cleanly,
            ordered by slug.
        """
        from lmchat.mcp.host import (  # noqa: PLC0415
            McpServerConfig,
            split_secrets_for_transport,
        )

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        mcp_servers.c.id,
                        mcp_servers.c.slug,
                        mcp_servers.c.transport,
                        mcp_servers.c.command,
                        mcp_servers.c.args,
                        mcp_servers.c.url,
                        mcp_servers.c.secrets_enc,
                    )
                    .where(mcp_servers.c.enabled.is_(True))
                    .order_by(mcp_servers.c.slug)
                )
            ).fetchall()

        configs: list[McpServerConfig] = []
        for row in rows:
            try:
                enc_list: list[dict] | None = row.secrets_enc
                expected_keys = [e["key"] for e in enc_list] if enc_list else []
                secrets = await self._decrypt_secrets(int(row.id), enc_list)
                failed_keys = [k for k in expected_keys if k not in secrets]
                if failed_keys:
                    message = (
                        "Credential decryption failed for "
                        f"{', '.join(failed_keys)} — re-enter the secret."
                    )
                    log.error(
                        "mcp_server_store.credential_decrypt_incomplete",
                        slug=row.slug,
                        failed_keys=failed_keys,
                    )
                    if credential_errors is not None:
                        credential_errors[row.slug] = message
                    continue

                env, headers = split_secrets_for_transport(row.transport, secrets)
                configs.append(
                    McpServerConfig(
                        server_id=row.slug,
                        transport=row.transport,
                        command=row.command or "",
                        args=list(row.args) if isinstance(row.args, list) else [],
                        env=env,
                        url=row.url or "",
                        headers=headers,
                    )
                )
            except Exception as _exc:  # noqa: BLE001
                log.error(
                    "mcp_server_store.host_config_skip_failed",
                    slug=getattr(row, "slug", "<unknown>"),
                    error=str(_exc),
                )
                continue
        return configs
