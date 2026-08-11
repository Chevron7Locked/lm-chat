# SPDX-License-Identifier: Apache-2.0
"""Cloud-provider credential persistence service.

Manages the ``provider_configs`` table: one row per external LLM provider
(openai / openrouter / groq / custom). CRUD only — routing lives elsewhere.

Encryption: API keys use the ``enc$v1$`` envelope with
``kind="provider_api_key"``, ``record_id=<row id>``. On decrypt failure
(secret rotated since save) the service logs ERROR and returns
``api_key=None`` — callers must treat that as "not available", not crash.

Views: ``list_all()`` returns safe views (``api_key_set`` bool, no
cleartext); ``get()`` returns the internal view with the decrypted key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import provider_configs
from lmchat.logging import get_logger
from lmchat.utils.encryption import decrypt, encrypt

log = get_logger(__name__)

_API_KEY_KIND: str = "provider_api_key"


# ---------------------------------------------------------------------------
# View dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderConfigSafeView:
    """Safe (public) view — API key is never included.

    Returned by :meth:`ProviderConfigService.list_all`.
    """

    provider: str
    base_url: str
    default_model: str | None
    extra_headers: dict[str, Any] | None
    enabled: bool
    api_key_set: bool
    allowed_models: list[str] | None = None


@dataclass(frozen=True, slots=True)
class ProviderConfigInternalView:
    """Internal view with decrypted API key.

    Returned by :meth:`ProviderConfigService.get`.
    ``api_key=None`` when no key is stored OR when decryption fails (secret
    rotation); callers must treat None as "not available".
    """

    provider: str
    base_url: str
    default_model: str | None
    extra_headers: dict[str, Any] | None
    enabled: bool
    api_key: str | None
    api_key_set: bool
    allowed_models: list[str] | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProviderConfigService:
    """Async CRUD service for the ``provider_configs`` table.

    Constructed at application lifespan; depends only on the application
    ``AsyncEngine``. No background tasks.
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    async def add_or_update(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str | None = None,
        default_model: str | None = None,
        extra_headers: dict[str, Any] | None = None,
        enabled: bool = True,
        allowed_models: list[str] | None = None,
    ) -> None:
        """Upsert a provider config row — full replace of an existing row.

        The API key, when provided, is encrypted with the ``enc$v1$``
        envelope before writing. Omitting ``api_key`` (``None``) on an
        update preserves the existing encrypted key rather than clearing it.

        Args:
            provider:       Slug: ``"openai"``, ``"openrouter"``, ``"groq"``,
                            or custom.
            api_key:        Plaintext bearer token; ``None`` = no key.
            allowed_models: Restricts the model picker to these ids.
                            ``None``/``[]`` = all models visible. Does NOT
                            block dispatch.
        """
        # Need the row's PK as the KDF record_id; fetch it first (may not
        # exist yet for a new row).
        existing_id = await self._fetch_id(provider)
        api_key_enc: str | None = None

        if api_key is not None and existing_id is not None:
            # Update path: encrypt using the stable existing id.
            api_key_enc = encrypt(
                api_key.encode(),
                kind=_API_KEY_KIND,
                record_id=existing_id,
            )

        values: dict[str, Any] = {
            "provider": provider,
            "base_url": base_url,
            "api_key_enc": api_key_enc,
            "default_model": default_model,
            "extra_headers": extra_headers,
            "enabled": enabled,
            "allowed_models": allowed_models if allowed_models else None,
        }

        if existing_id is None:
            if api_key is None:
                # No key to encrypt — values already reflect final state,
                # a single insert is enough.
                await self._insert_row(values)
            else:
                # The KDF record_id is the row's own PK, only assigned at
                # INSERT time — insert a disabled placeholder, encrypt with
                # the generated PK, then update to the real enabled/
                # api_key_enc state, all in ONE transaction. No reader
                # (e.g. registry.refresh()) can ever observe an enabled row
                # with a NULL api_key_enc.
                placeholder_values = {**values, "api_key_enc": None, "enabled": False}

                async def _insert_and_set_key() -> None:
                    async with self._engine.begin() as conn:
                        new_id = await self._insert_row_in_conn(conn, placeholder_values)
                        api_key_enc = encrypt(
                            api_key.encode(),
                            kind=_API_KEY_KIND,
                            record_id=new_id,
                        )
                        await conn.execute(
                            update(provider_configs)
                            .where(provider_configs.c.id == new_id)
                            .values(api_key_enc=api_key_enc, enabled=enabled)
                        )

                # Safe to retry whole-closure: encrypt() uses a fresh nonce
                # each call and the prior attempt's txn was rolled back, so
                # a retry can't leave two partial rows.
                await with_write_retry(_insert_and_set_key)
        else:
            # Omitting api_key (None) means "keep existing key", not "clear
            # it" — only an explicit api_key replaces api_key_enc.
            update_values = {k: v for k, v in values.items() if k != "api_key_enc"}
            if api_key is not None:
                update_values["api_key_enc"] = api_key_enc
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(provider_configs)
                    .where(provider_configs.c.provider == provider)
                    .values(**update_values)
                )

    async def delete(self, provider: str) -> None:
        """Delete the config row for *provider*.

        No-op when the provider does not exist.

        Args:
            provider: Provider slug to delete.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(provider_configs).where(
                    provider_configs.c.provider == provider
                )
            )

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    async def list_all(self) -> list[ProviderConfigSafeView]:
        """Return safe views of all provider configs, ordered by slug.

        The API key is never included; ``api_key_set`` is ``True`` when an
        encrypted key blob is present.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        provider_configs.c.provider,
                        provider_configs.c.base_url,
                        provider_configs.c.default_model,
                        provider_configs.c.extra_headers,
                        provider_configs.c.enabled,
                        provider_configs.c.api_key_enc,
                        provider_configs.c.allowed_models,
                    ).order_by(provider_configs.c.provider)
                )
            ).fetchall()
        return [
            ProviderConfigSafeView(
                provider=row.provider,
                base_url=row.base_url,
                default_model=row.default_model,
                extra_headers=row.extra_headers,
                enabled=bool(row.enabled),
                api_key_set=bool(row.api_key_enc),
                allowed_models=row.allowed_models or None,
            )
            for row in rows
        ]

    async def get(self, provider: str) -> ProviderConfigInternalView | None:
        """Return the internal view for *provider*, decrypted key included.

        On decryption failure (e.g. secret rotation) logs ERROR and returns
        ``api_key=None`` instead of raising — treat ``None`` as "not
        available" and prompt re-entry.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        provider_configs.c.id,
                        provider_configs.c.provider,
                        provider_configs.c.base_url,
                        provider_configs.c.api_key_enc,
                        provider_configs.c.default_model,
                        provider_configs.c.extra_headers,
                        provider_configs.c.enabled,
                        provider_configs.c.allowed_models,
                    ).where(provider_configs.c.provider == provider)
                )
            ).first()
        if row is None:
            return None

        api_key: str | None = None
        if row.api_key_enc:
            try:
                api_key = decrypt(
                    row.api_key_enc,
                    kind=_API_KEY_KIND,
                    record_id=int(row.id),
                ).decode()
            except Exception as exc:  # noqa: BLE001
                # InvalidTag or similar — secret likely rotated. Log the
                # exception type so root-cause is clear without reproducing.
                log.error(
                    "provider_config.api_key_decrypt_failed",
                    provider=provider,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        return ProviderConfigInternalView(
            provider=row.provider,
            base_url=row.base_url,
            default_model=row.default_model,
            extra_headers=row.extra_headers,
            enabled=bool(row.enabled),
            api_key=api_key,
            api_key_set=bool(row.api_key_enc),
            allowed_models=row.allowed_models or None,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fetch_id(self, provider: str) -> int | None:
        """Return the ``id`` of the existing row for *provider*, or None."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(provider_configs.c.id).where(
                        provider_configs.c.provider == provider
                    )
                )
            ).first()
        return int(row.id) if row else None

    async def _insert_row(self, values: dict[str, Any]) -> int:
        """Insert a new provider_configs row and return the new PK.

        Opens and commits its own transaction — use
        :meth:`_insert_row_in_conn` when the insert must share a
        transaction with a follow-up write (see :meth:`add_or_update`).
        """
        async with self._engine.begin() as conn:
            return await self._insert_row_in_conn(conn, values)

    async def _insert_row_in_conn(
        self, conn: AsyncConnection, values: dict[str, Any]
    ) -> int:
        """Insert a new provider_configs row on an already-open *conn*.

        Cross-dialect: SQLite uses ``lastrowid``; PostgreSQL uses
        RETURNING. Caller owns the transaction — this method neither
        begins nor commits.
        """
        dialect = self._engine.dialect.name
        if dialect == "postgresql":
            result = await conn.execute(
                pg_insert(provider_configs)
                .values(**values)
                .returning(provider_configs.c.id)
            )
            row = result.first()
            return int(row.id)  # type: ignore[union-attr]
        else:
            result = await conn.execute(provider_configs.insert().values(**values))
            return int(result.lastrowid)
