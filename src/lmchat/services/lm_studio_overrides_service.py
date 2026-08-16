# SPDX-License-Identifier: Apache-2.0
"""LM Studio config overrides service.

Three-tier fallback chain for the LM Studio connection parameters:

    1. Per-user override (``user_lm_studio_overrides`` row).
    2. Server-admin default (``server_lm_studio_default`` row, id=1).
    3. Env-var default (``Settings.lm_studio_*``).

The API key is stored on both rows under the ``enc$v1$``
envelope.  The fallback chain applies field-by-field.

Probe semantics
---------------
``probe`` uses a **fresh** ``httpx.AsyncClient`` (NOT the
lifespan-shared client) so the "Test connection" button never mutates
the application-wide upstream state.  The probe issues
``GET {base_url}/api/v1/models`` with a short timeout and reports
the model count or the error string.

Empty-string handling
---------------------
A NULL on any column means "fall through to the next tier".  The
service treats env-var ``lm_studio_api_key=""`` as "no api key set"
(matches ``Settings`` default).  The route layer rejects empty strings
on writes — clients must use ``clear`` to wipe a field.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlparse

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.config import Settings
from lmchat.db.schema import server_lm_studio_default, user_lm_studio_overrides
from lmchat.logging import get_logger
from lmchat.utils.encryption import decrypt, encrypt
from lmchat.utils.task_lifetime import spawn_background_task

log = get_logger(__name__)

_ADMIN_RECORD_ID: Final[int] = 1
_PROBE_TIMEOUT_SECONDS: Final[float] = 5.0
_API_KEY_KIND_USER: Final[str] = "lm_studio_api_key"
_API_KEY_KIND_ADMIN: Final[str] = "lm_studio_admin_default"

# Valid values for lm_studio_endpoint_mode (see set_endpoint_mode /
# fetch_endpoint_mode / resolve_lm_studio_endpoint_mode below).
_VALID_ENDPOINT_MODES: Final[frozenset[str]] = frozenset({"native", "openai_compat"})

# Grace period before closing the old http client after a rewire.
# 180 s gives long-running generations (up to 600 s read timeout) adequate
# drain time — bumped from an initial 60 s proposal.
OLD_CLIENT_GRACE_SECONDS: Final[float] = 180.0

# Embeddings path — mirrors EmbeddingClient._EMBEDDINGS_PATH; kept here
# so rewire_singletons can rebuild _endpoint without importing EmbeddingClient.
# LM Studio only exposes the OpenAI-compat /v1/embeddings path.
EMBEDDINGS_PATH_CONST: Final[str] = "/v1/embeddings"

ClearableField = str  # "base_url" | "api_key" | "default_model"
_CLEARABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"base_url", "api_key", "default_model"}
)

@dataclass(frozen=True, slots=True)
class ResolvedLmStudioConfig:
    """Per-request resolved LM Studio connection parameters.

    ``source`` reports which tier supplied each field; the
    chain is field-by-field (a user may override base_url but not the
    default_model, which then falls through to env).
    """

    base_url: str
    api_key: str  # plaintext (decrypted); "" when unset
    default_model: str
    source_base_url: str  # "user" | "server_admin" | "unset"
    source_api_key: str
    source_default_model: str
    api_key_set: bool


def _normalise_probed_model(item: dict[str, Any]) -> ProbedModel:
    """Pull (id, name, loaded, is_embedding) out of one upstream entry.

    Tolerates the field-name variance across LM Studio's three model
    response shapes (native ``key``/``display_name``/``loaded_instances``,
    OpenAI-compat ``id``/``object``, legacy v0). Falls back to the
    id as the display name when no human-readable label is present.
    """
    raw_id = (
        item.get("key")
        or item.get("id")
        or item.get("model")
        or ""
    )
    name = (
        item.get("display_name")
        or item.get("displayName")
        or raw_id
    )
    loaded_field = item.get("loaded_instances")
    if isinstance(loaded_field, list):
        loaded = len(loaded_field) > 0
    else:
        loaded = bool(item.get("loaded", False))
    item_type = item.get("type") or item.get("object") or ""
    caps = item.get("capabilities")
    is_embedding = (
        str(item_type).lower() == "embedding"
        or (isinstance(caps, dict) and bool(caps.get("embedding")))
    )
    return ProbedModel(
        id=str(raw_id),
        name=str(name),
        loaded=loaded,
        is_embedding=is_embedding,
    )


@dataclass(frozen=True, slots=True)
class ProbedModel:
    """Single entry from a probe's normalised model list.

    The probe returns its own light-weight view of the upstream model
    catalogue so the SetupLmStudio page can populate its dropdown
    immediately after a probe — without round-tripping ``/api/models``,
    which still reads the shared (pre-save) HTTP client and would come
    back empty until the override is persisted.
    """

    id: str
    name: str
    loaded: bool
    is_embedding: bool


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Result of a one-shot ``/api/v1/models`` probe."""

    ok: bool
    model_count: int | None
    error: str | None
    models: list[ProbedModel] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _StoredOverride:
    """Internal raw row view (encrypted)."""

    base_url: str | None
    api_key_enc: str | None
    default_model: str | None


class LmStudioOverridesService:
    """Service that owns the per-user + admin-default override storage.

    Constructed once at lifespan; depends only on the application
    ``AsyncEngine`` and the ``Settings`` instance.  No background tasks.
    """

    def __init__(self, *, engine: AsyncEngine, settings: Settings) -> None:
        """Initialise the service.

        Args:
            engine:   Application-global ``AsyncEngine``.
            settings: Validated ``Settings`` instance (env-tier fallback
                      source for ``base_url`` / ``api_key`` /
                      ``default_model``).
        """
        self._engine = engine
        self._settings = settings

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    async def resolve(self, user_id: int) -> ResolvedLmStudioConfig:
        """Apply the three-tier fallback chain for ``user_id``.

        Performs at most two SELECTs (user row + admin row).  Decrypts
        the chosen ``api_key_enc`` envelope; on tag-verification
        failure logs at ERROR and falls through to the next tier.

        Args:
            user_id: Authenticated user id from ``require_user``.

        Returns:
            A :class:`ResolvedLmStudioConfig` with the active values
            and per-field source discriminators.
        """
        user_row = await self._fetch_user_override(user_id)
        admin_row = await self._fetch_admin_default()

        # Env values are NO LONGER part of the active-config fallback chain
        # They remain available via
        # `get_env_suggestion()` so the Settings UI can pre-fill the form
        # as a non-active suggestion, but every user must explicitly accept
        # values into the user_override row before any probe runs.
        base_url, source_base_url = self._pick(
            field="base_url",
            user_value=user_row.base_url if user_row else None,
            admin_value=admin_row.base_url if admin_row else None,
        )
        api_key, source_api_key = self._pick_api_key(
            user_id=user_id,
            user_row=user_row,
            admin_row=admin_row,
        )
        default_model, source_default_model = self._pick(
            field="default_model",
            user_value=user_row.default_model if user_row else None,
            admin_value=admin_row.default_model if admin_row else None,
        )

        return ResolvedLmStudioConfig(
            base_url=base_url,
            api_key=api_key,
            default_model=default_model,
            source_base_url=source_base_url,
            source_api_key=source_api_key,
            source_default_model=source_default_model,
            api_key_set=bool(api_key),
        )

    def get_env_suggestion(self) -> dict[str, str | bool]:
        """Return the env-derived LM Studio values for UI pre-fill.

        Reference-only: these values are NOT applied to any user's active
        config. The Settings UI fetches
        them when both the user override and admin default are empty so
        the form can be pre-populated as a starting suggestion the admin
        must explicitly Save before any probe runs.

        Returns:
            ``{"base_url": str, "api_key_set": bool, "default_model": str}``
            — api_key cleartext is NEVER returned; only its presence.
        """
        return {
            "base_url": self._settings.lm_studio_base_url or "",
            "api_key_set": bool(self._settings.lm_studio_api_key),
            "default_model": self._settings.lm_studio_default_model or "",
        }

    @staticmethod
    def _pick(
        *,
        field: str,
        user_value: str | None,
        admin_value: str | None,
    ) -> tuple[str, str]:
        """Return ``(value, source)`` per the fallback chain.

        Env values are explicitly NOT part of this chain — see
        ``resolve()`` docstring for the rationale.
        Returns ``("", "unset")`` when neither user nor admin has set
        the field; consumers (models_service, adapter) must treat empty
        base_url as "not configured" and skip probes accordingly.
        """
        del field  # reserved for future per-field logging
        if user_value:
            return user_value, "user"
        if admin_value:
            return admin_value, "server_admin"
        return "", "unset"

    def _pick_api_key(
        self,
        *,
        user_id: int,
        user_row: _StoredOverride | None,
        admin_row: _StoredOverride | None,
    ) -> tuple[str, str]:
        """Resolve + decrypt the api_key envelope per the chain."""
        if user_row and user_row.api_key_enc:
            try:
                return (
                    decrypt(
                        user_row.api_key_enc,
                        kind=_API_KEY_KIND_USER,
                        record_id=user_id,
                    ).decode(),
                    "user",
                )
            except Exception as exc:  # noqa: BLE001
                # Some decrypt failures surface with an empty str(exc)
                # (e.g. cryptography's InvalidTag.__str__ is ""), so the
                # earlier "error=str(exc)" line printed nothing useful
                # and the root-cause was invisible. Include
                # the exception class name so the source is identifiable
                # without re-running the failure.
                log.error(
                    "lm_studio_overrides.user_api_key_decrypt_failed",
                    user_id=user_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        if admin_row and admin_row.api_key_enc:
            try:
                return (
                    decrypt(
                        admin_row.api_key_enc,
                        kind=_API_KEY_KIND_ADMIN,
                        record_id=_ADMIN_RECORD_ID,
                    ).decode(),
                    "server_admin",
                )
            except Exception as exc:  # noqa: BLE001
                # See the matching user-side log above for the
                # "empty str(exc)" rationale — include error_type so
                # the underlying exception class is identifiable.
                log.error(
                    "lm_studio_overrides.admin_api_key_decrypt_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        # Env api_key is NO LONGER an auto-fallback. Return empty + "unset" so consumers
        # know the user must explicitly accept env values via get_env_suggestion().
        return "", "unset"

    async def _fetch_user_override(self, user_id: int) -> _StoredOverride | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        user_lm_studio_overrides.c.base_url,
                        user_lm_studio_overrides.c.api_key_enc,
                        user_lm_studio_overrides.c.default_model,
                    ).where(user_lm_studio_overrides.c.user_id == user_id)
                )
            ).first()
        if row is None:
            return None
        return _StoredOverride(
            base_url=row.base_url,
            api_key_enc=row.api_key_enc,
            default_model=row.default_model,
        )

    async def prune_unusable_api_keys(self) -> int:
        """Clear ``api_key_enc`` blobs that can't be decrypted with the
        current ``LM_CHAT_SECRET``.

        Runs at lifespan boot. When the deployment's signing/encryption
        secret rotates (e.g. a dev restart that re-generates it,
        or a redeploy without ``LM_CHAT_SECRET`` set in env), every
        previously-saved api_key envelope becomes garbage — decrypt
        fails silently and ``resolve()`` reports ``api_key_set=false``.
        The chat shell then sends unauthenticated requests to LM Studio,
        which returns 401, leaving the model list empty and the status
        badge red. From the admin's perspective it looks like LM Studio
        broke — but the saved key is just unusable.

        This sweeps both tables, attempts to decrypt each present blob,
        and NULLs out the field when decryption fails. The user/admin
        re-enters the key via Settings; everything else (URL, model)
        survives. Idempotent.

        Returns:
            Number of api_key_enc fields cleared.
        """
        cleared = 0

        # Admin row first — single fixed PK.
        admin_row = await self._fetch_admin_default()
        if admin_row is not None and admin_row.api_key_enc:
            try:
                decrypt(
                    admin_row.api_key_enc,
                    kind=_API_KEY_KIND_ADMIN,
                    record_id=_ADMIN_RECORD_ID,
                )
            except Exception as exc:  # noqa: BLE001
                async with self._engine.begin() as conn:
                    await conn.execute(
                        update(server_lm_studio_default)
                        .where(server_lm_studio_default.c.id == _ADMIN_RECORD_ID)
                        .values(api_key_enc=None)
                    )
                cleared += 1
                log.warning(
                    "lm_studio_overrides.admin_api_key_pruned",
                    reason=str(exc),
                    hint="LM_CHAT_SECRET appears to have rotated since the "
                         "admin API key was saved; the admin must re-enter it.",
                )

        # User overrides — sweep every row.
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        user_lm_studio_overrides.c.user_id,
                        user_lm_studio_overrides.c.api_key_enc,
                    ).where(user_lm_studio_overrides.c.api_key_enc.is_not(None))
                )
            ).fetchall()
        for row in rows:
            try:
                decrypt(
                    row.api_key_enc,
                    kind=_API_KEY_KIND_USER,
                    record_id=int(row.user_id),
                )
            except Exception:  # noqa: BLE001
                async with self._engine.begin() as conn:
                    await conn.execute(
                        update(user_lm_studio_overrides)
                        .where(
                            user_lm_studio_overrides.c.user_id == row.user_id
                        )
                        .values(api_key_enc=None)
                    )
                cleared += 1
                log.warning(
                    "lm_studio_overrides.user_api_key_pruned",
                    user_id=int(row.user_id),
                )

        return cleared

    async def _fetch_admin_default(self) -> _StoredOverride | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        server_lm_studio_default.c.base_url,
                        server_lm_studio_default.c.api_key_enc,
                        server_lm_studio_default.c.default_model,
                    ).where(server_lm_studio_default.c.id == _ADMIN_RECORD_ID)
                )
            ).first()
        if row is None:
            return None
        return _StoredOverride(
            base_url=row.base_url,
            api_key_enc=row.api_key_enc,
            default_model=row.default_model,
        )

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    async def set_user_override(
        self,
        *,
        user_id: int,
        base_url: str | None,
        api_key: str | None,
        default_model: str | None,
        clear: Sequence[str] | None,
    ) -> None:
        """Apply patch semantics to ``user_lm_studio_overrides``.

        - ``None`` on a field means "leave unchanged".
        - A non-None value writes the new value (api_key gets encrypted).
        - ``clear`` lists fields to set back to NULL.

        Row is UPSERTed on the user_id PK.

        Raises:
            ValueError: on unknown ``clear`` field, or on empty-string
                        value (use ``clear`` to wipe).
        """
        _validate_clear(clear)
        _reject_empty_values(base_url=base_url, api_key=api_key,
                             default_model=default_model)

        clear_set = set(clear or [])
        current = await self._fetch_user_override(user_id)

        new_base_url = _apply_field(
            current_value=current.base_url if current else None,
            new_value=base_url,
            clear=clear_set,
            field="base_url",
        )
        new_default_model = _apply_field(
            current_value=current.default_model if current else None,
            new_value=default_model,
            clear=clear_set,
            field="default_model",
        )
        if "api_key" in clear_set:
            new_api_key_enc: str | None = None
        elif api_key is not None:
            new_api_key_enc = encrypt(
                api_key.encode(),
                kind=_API_KEY_KIND_USER,
                record_id=user_id,
            )
        else:
            new_api_key_enc = current.api_key_enc if current else None

        await self._upsert_user_row(
            user_id=user_id,
            base_url=new_base_url,
            api_key_enc=new_api_key_enc,
            default_model=new_default_model,
        )

    async def set_admin_default(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        default_model: str | None,
        clear: Sequence[str] | None,
    ) -> None:
        """Apply patch semantics to ``server_lm_studio_default``.

        Same shape as :meth:`set_user_override` but targets the
        singleton row at id=1.
        """
        _validate_clear(clear)
        _reject_empty_values(base_url=base_url, api_key=api_key,
                             default_model=default_model)

        clear_set = set(clear or [])
        current = await self._fetch_admin_default()

        new_base_url = _apply_field(
            current_value=current.base_url if current else None,
            new_value=base_url,
            clear=clear_set,
            field="base_url",
        )
        new_default_model = _apply_field(
            current_value=current.default_model if current else None,
            new_value=default_model,
            clear=clear_set,
            field="default_model",
        )
        if "api_key" in clear_set:
            new_api_key_enc: str | None = None
        elif api_key is not None:
            new_api_key_enc = encrypt(
                api_key.encode(),
                kind=_API_KEY_KIND_ADMIN,
                record_id=_ADMIN_RECORD_ID,
            )
        else:
            new_api_key_enc = current.api_key_enc if current else None

        await self._upsert_admin_row(
            base_url=new_base_url,
            api_key_enc=new_api_key_enc,
            default_model=new_default_model,
        )

    async def set_preferred_embedding_model(
        self, model_key: str | None
    ) -> None:
        """Write (or clear) ``preferred_embedding_model_id`` in the DB.

        This is a DEDICATED setter that ONLY updates the
        ``preferred_embedding_model_id`` column on the singleton admin row.
        It does NOT touch ``base_url``, ``api_key_enc``, or
        ``default_model``, and it MUST NOT trigger
        :meth:`rewire_singletons` — the embedding-model preference has an
        entirely different lifecycle from the LM Studio connection settings.

        Args:
            model_key: The catalog key to persist, or ``None`` to clear the
                preference (returns auto-pick to the deterministic
                lexicographic sort).
        """
        dialect = self._engine.dialect.name
        async with self._engine.begin() as conn:
            if dialect == "postgresql":
                stmt = pg_insert(server_lm_studio_default).values(
                    id=_ADMIN_RECORD_ID,
                    preferred_embedding_model_id=model_key,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "preferred_embedding_model_id": stmt.excluded.preferred_embedding_model_id
                    },
                )
                await conn.execute(stmt)
            elif dialect == "sqlite":
                stmt = sqlite_insert(server_lm_studio_default).values(
                    id=_ADMIN_RECORD_ID,
                    preferred_embedding_model_id=model_key,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "preferred_embedding_model_id": stmt.excluded.preferred_embedding_model_id
                    },
                )
                await conn.execute(stmt)
            else:  # pragma: no cover — generic fallback
                existing = (
                    await conn.execute(
                        select(server_lm_studio_default.c.id).where(
                            server_lm_studio_default.c.id == _ADMIN_RECORD_ID
                        )
                    )
                ).first()
                if existing is None:
                    await conn.execute(
                        insert(server_lm_studio_default).values(
                            id=_ADMIN_RECORD_ID,
                            preferred_embedding_model_id=model_key,
                        )
                    )
                else:
                    await conn.execute(
                        update(server_lm_studio_default)
                        .where(server_lm_studio_default.c.id == _ADMIN_RECORD_ID)
                        .values(preferred_embedding_model_id=model_key)
                    )

    async def fetch_preferred_embedding_model(self) -> str | None:
        """Return the current ``preferred_embedding_model_id`` column value.

        Returns ``None`` when the row does not exist or the column is NULL
        (i.e. auto-pick is in effect). Operates on the GLOBAL
        ``server_lm_studio_default`` admin row (id=1), never a per-user row, so
        it correctly carries no user_id filter; kept next to
        ``set_preferred_embedding_model`` to keep the admin-row methods grouped.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        server_lm_studio_default.c.preferred_embedding_model_id
                    ).where(server_lm_studio_default.c.id == _ADMIN_RECORD_ID)
                )
            ).first()
        if row is None:
            return None
        return row.preferred_embedding_model_id or None

    async def set_preferred_background_model(
        self, model_key: str | None
    ) -> None:
        """Write (or clear) ``preferred_background_model_id`` in the DB.

        Dedicated setter that ONLY updates the
        ``preferred_background_model_id`` column on the singleton admin row.
        It does NOT touch ``base_url``, ``api_key_enc``, ``default_model`` or
        ``preferred_embedding_model_id``, and it MUST NOT trigger
        :meth:`rewire_singletons` — the background-model preference has an
        entirely different lifecycle from the LM Studio connection settings.

        Args:
            model_key: The catalog key to persist, or ``None`` to clear the
                preference (background tasks return to "Same as chat model").
        """
        dialect = self._engine.dialect.name
        async with self._engine.begin() as conn:
            if dialect == "postgresql":
                stmt = pg_insert(server_lm_studio_default).values(
                    id=_ADMIN_RECORD_ID,
                    preferred_background_model_id=model_key,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "preferred_background_model_id": stmt.excluded.preferred_background_model_id
                    },
                )
                await conn.execute(stmt)
            elif dialect == "sqlite":
                stmt = sqlite_insert(server_lm_studio_default).values(
                    id=_ADMIN_RECORD_ID,
                    preferred_background_model_id=model_key,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "preferred_background_model_id": stmt.excluded.preferred_background_model_id
                    },
                )
                await conn.execute(stmt)
            else:  # pragma: no cover — generic fallback
                existing = (
                    await conn.execute(
                        select(server_lm_studio_default.c.id).where(
                            server_lm_studio_default.c.id == _ADMIN_RECORD_ID
                        )
                    )
                ).first()
                if existing is None:
                    await conn.execute(
                        insert(server_lm_studio_default).values(
                            id=_ADMIN_RECORD_ID,
                            preferred_background_model_id=model_key,
                        )
                    )
                else:
                    await conn.execute(
                        update(server_lm_studio_default)
                        .where(server_lm_studio_default.c.id == _ADMIN_RECORD_ID)
                        .values(preferred_background_model_id=model_key)
                    )

    async def fetch_preferred_background_model(self) -> str | None:
        """Return the current ``preferred_background_model_id`` column value.

        Returns ``None`` when the row does not exist or the column is NULL
        (i.e. background tasks reuse the chat model — the default). Operates on
        the GLOBAL ``server_lm_studio_default`` admin row (id=1), never a
        per-user row, so it carries no user_id filter; kept next to
        ``set_preferred_background_model`` to keep the admin-row methods grouped.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        server_lm_studio_default.c.preferred_background_model_id
                    ).where(server_lm_studio_default.c.id == _ADMIN_RECORD_ID)
                )
            ).first()
        if row is None:
            return None
        return row.preferred_background_model_id or None


    async def set_endpoint_mode(self, mode: str) -> None:
        """Write ``lm_studio_endpoint_mode`` on the singleton admin row.

        This is a DEDICATED setter that ONLY updates the
        ``lm_studio_endpoint_mode`` column on the singleton admin row.  It
        does NOT touch ``base_url``, ``api_key_enc``, or ``default_model``,
        and it MUST NOT trigger :meth:`rewire_singletons` — the endpoint
        mode is independent of the LM Studio connection lifecycle (same
        rationale as :meth:`set_preferred_embedding_model`).

        Args:
            mode: ``"native"`` or ``"openai_compat"``.

        Raises:
            ValueError: If ``mode`` is not one of the two valid values.
        """
        if mode not in _VALID_ENDPOINT_MODES:
            raise ValueError(
                f"lm_studio_endpoint_mode must be one of "
                f"{sorted(_VALID_ENDPOINT_MODES)}, got {mode!r}"
            )
        # NULL stores "native" (the default) — mirrors the NULL=auto
        # convention used by the sibling preference columns on this row.
        stored = None if mode == "native" else mode
        dialect = self._engine.dialect.name
        async with self._engine.begin() as conn:
            if dialect == "postgresql":
                stmt = pg_insert(server_lm_studio_default).values(
                    id=_ADMIN_RECORD_ID, lm_studio_endpoint_mode=stored,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "lm_studio_endpoint_mode": stmt.excluded.lm_studio_endpoint_mode
                    },
                )
                await conn.execute(stmt)
            elif dialect == "sqlite":
                stmt = sqlite_insert(server_lm_studio_default).values(
                    id=_ADMIN_RECORD_ID, lm_studio_endpoint_mode=stored,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "lm_studio_endpoint_mode": stmt.excluded.lm_studio_endpoint_mode
                    },
                )
                await conn.execute(stmt)
            else:  # pragma: no cover — generic fallback
                existing = (
                    await conn.execute(
                        select(server_lm_studio_default.c.id).where(
                            server_lm_studio_default.c.id == _ADMIN_RECORD_ID
                        )
                    )
                ).first()
                if existing is None:
                    await conn.execute(
                        insert(server_lm_studio_default).values(
                            id=_ADMIN_RECORD_ID, lm_studio_endpoint_mode=stored,
                        )
                    )
                else:
                    await conn.execute(
                        update(server_lm_studio_default)
                        .where(server_lm_studio_default.c.id == _ADMIN_RECORD_ID)
                        .values(lm_studio_endpoint_mode=stored)
                    )

    async def fetch_endpoint_mode(self) -> str:
        """Return the current endpoint mode: ``"native"`` or ``"openai_compat"``.

        NULL/unset → ``"native"`` (the default).  Any unrecognized stored
        value (should not happen outside manual DB edits) also falls back
        to ``"native"`` — fail-soft, since native is always a safe,
        currently-working mode.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(server_lm_studio_default.c.lm_studio_endpoint_mode).where(
                        server_lm_studio_default.c.id == _ADMIN_RECORD_ID
                    )
                )
            ).first()
        if row is None or not row.lm_studio_endpoint_mode:
            return "native"
        if row.lm_studio_endpoint_mode not in _VALID_ENDPOINT_MODES:
            return "native"
        return row.lm_studio_endpoint_mode

    async def seed_admin_default_from_env(
        self,
        *,
        base_url: str,
        api_key: str,
    ) -> bool:
        """Seed ``server_lm_studio_default`` from env vars on first boot.

        Called by the app lifespan when no DB tier has a saved api_key
        and a valid env-provided key exists.  Gate per-field: seeds
        ``api_key_enc`` only when the admin row has none; seeds
        ``base_url`` only when the admin row has none.  Never overrides
        an already-set tier, never touches ``default_model``.

        The caller is responsible for validation (non-empty URL, not a
        filesystem path) and for probing LM Studio with the candidate —
        this method only writes to the DB when called.

        Args:
            base_url:  The env-provided LM Studio base URL.
            api_key:   The env-provided LM Studio API key (plaintext).

        Returns:
            ``True`` if at least one field was written, ``False`` if
            both fields already exist in the admin row (no-op).
        """
        current = await self._fetch_admin_default()
        current_base_url = current.base_url if current else None
        current_api_key_enc = current.api_key_enc if current else None

        needs_base_url = not current_base_url and bool(base_url)
        needs_api_key = not current_api_key_enc and bool(api_key)

        if not needs_base_url and not needs_api_key:
            return False

        new_base_url = base_url if needs_base_url else current_base_url
        new_api_key_enc: str | None = None
        if needs_api_key:
            new_api_key_enc = encrypt(
                api_key.encode(),
                kind=_API_KEY_KIND_ADMIN,
                record_id=_ADMIN_RECORD_ID,
            )
        else:
            new_api_key_enc = current_api_key_enc

        await self._upsert_admin_row(
            base_url=new_base_url,
            api_key_enc=new_api_key_enc,
            default_model=current.default_model if current else None,
        )
        return True

    async def _upsert_user_row(
        self,
        *,
        user_id: int,
        base_url: str | None,
        api_key_enc: str | None,
        default_model: str | None,
    ) -> None:
        """Cross-dialect UPSERT on user_lm_studio_overrides.user_id."""
        dialect = self._engine.dialect.name
        values = {
            "user_id": user_id,
            "base_url": base_url,
            "api_key_enc": api_key_enc,
            "default_model": default_model,
        }
        async with self._engine.begin() as conn:
            if dialect == "postgresql":
                stmt = pg_insert(user_lm_studio_overrides).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={
                        "base_url": stmt.excluded.base_url,
                        "api_key_enc": stmt.excluded.api_key_enc,
                        "default_model": stmt.excluded.default_model,
                    },
                )
                await conn.execute(stmt)
            elif dialect == "sqlite":
                stmt = sqlite_insert(user_lm_studio_overrides).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={
                        "base_url": stmt.excluded.base_url,
                        "api_key_enc": stmt.excluded.api_key_enc,
                        "default_model": stmt.excluded.default_model,
                    },
                )
                await conn.execute(stmt)
            else:  # pragma: no cover — only sqlite + postgresql supported
                existing = (
                    await conn.execute(
                        select(user_lm_studio_overrides.c.user_id).where(
                            user_lm_studio_overrides.c.user_id == user_id
                        )
                    )
                ).first()
                if existing is None:
                    await conn.execute(insert(user_lm_studio_overrides).values(**values))
                else:
                    await conn.execute(
                        update(user_lm_studio_overrides)
                        .where(user_lm_studio_overrides.c.user_id == user_id)
                        .values(
                            base_url=base_url,
                            api_key_enc=api_key_enc,
                            default_model=default_model,
                        )
                    )

    async def _upsert_admin_row(
        self,
        *,
        base_url: str | None,
        api_key_enc: str | None,
        default_model: str | None,
    ) -> None:
        """Cross-dialect UPSERT on server_lm_studio_default.id=1."""
        dialect = self._engine.dialect.name
        values = {
            "id": _ADMIN_RECORD_ID,
            "base_url": base_url,
            "api_key_enc": api_key_enc,
            "default_model": default_model,
        }
        async with self._engine.begin() as conn:
            if dialect == "postgresql":
                stmt = pg_insert(server_lm_studio_default).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "base_url": stmt.excluded.base_url,
                        "api_key_enc": stmt.excluded.api_key_enc,
                        "default_model": stmt.excluded.default_model,
                    },
                )
                await conn.execute(stmt)
            elif dialect == "sqlite":
                stmt = sqlite_insert(server_lm_studio_default).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "base_url": stmt.excluded.base_url,
                        "api_key_enc": stmt.excluded.api_key_enc,
                        "default_model": stmt.excluded.default_model,
                    },
                )
                await conn.execute(stmt)
            else:  # pragma: no cover
                existing = (
                    await conn.execute(
                        select(server_lm_studio_default.c.id).where(
                            server_lm_studio_default.c.id == _ADMIN_RECORD_ID
                        )
                    )
                ).first()
                if existing is None:
                    await conn.execute(insert(server_lm_studio_default).values(**values))
                else:
                    await conn.execute(
                        update(server_lm_studio_default)
                        .where(server_lm_studio_default.c.id == _ADMIN_RECORD_ID)
                        .values(
                            base_url=base_url,
                            api_key_enc=api_key_enc,
                            default_model=default_model,
                        )
                    )

    # ------------------------------------------------------------------
    # SSRF guard (defense in depth)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_ssrf_target(url: str) -> None:
        """Reject *url* if its scheme is not ``http`` or ``https``.

        For a local admin-only app, private/loopback/LAN hosts are the
        LEGITIMATE targets (LM Studio lives there).  The admin-gate on
        the probe route is the real protection — the admin is trusted.
        This validator only rejects non-HTTP schemes (``file://``,
        ``gopher://``, ``dict://``, ``ftp://``, etc.) which are never
        valid LM Studio URLs.

        Raises:
            ValueError: If the URL scheme is not http or https.
        """
        parsed = urlparse(url)
        scheme = parsed.scheme or ""
        if scheme not in ("http", "https"):
            raise ValueError(
                f"base_url scheme {scheme!r} is not allowed — "
                "only http:// and https:// URLs are valid LM Studio targets"
            )

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    async def probe(
        self,
        *,
        base_url: str,
        api_key: str | None,
    ) -> ProbeResult:
        """Issue a one-shot ``/api/v1/models`` probe.

        Uses a **private** ``httpx.AsyncClient`` — the lifespan-shared
        client is never touched (so a failed probe cannot poison
        upstream credentials for the running app).

        Args:
            base_url: Caller-supplied LM Studio base URL to probe.
            api_key:  Optional bearer token; sent as
                      ``Authorization: Bearer <api_key>`` when set.

Returns:
            A :class:`ProbeResult`.  ``ok=True`` only when the response
            is HTTP 200 AND the JSON body parses to a list of models.

Raises:
              ValueError: If *base_url* uses a non-HTTP scheme (SSRF guard —
                          only ``http://`` and ``https://`` are allowed).
          """
          # SSRF guard: reject non-http schemes before any network call.
        self._validate_ssrf_target(base_url)
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = base_url.rstrip("/") + "/api/v1/models"
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return ProbeResult(ok=False, model_count=None, error=str(exc))
        if resp.status_code != 200:
            return ProbeResult(
                ok=False,
                model_count=None,
                error=f"upstream returned HTTP {resp.status_code}",
            )
        try:
            body = resp.json()
        except ValueError as exc:
            return ProbeResult(ok=False, model_count=None, error=f"non-json: {exc}")
        # All three LM Studio response shapes are tolerated:
        #   /v1/models          → {"data": [...], "object": "list"}    (OpenAI-compat)
        #   /api/v0/models      → {"data": [...], "object": "list"}    (LM Studio legacy)
        #   /api/v1/models      → {"models": [...]}                    (LM Studio native, current)
        # Plus the bare-list fallback for older/custom servers.
        items: list[Any] | None = None
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            for key in ("data", "models"):
                candidate = body.get(key)
                if isinstance(candidate, list):
                    items = candidate
                    break
        if items is None:
            return ProbeResult(
                ok=False,
                model_count=None,
                error=(
                    "unrecognised response shape: "
                    f"keys={list(body.keys()) if isinstance(body, dict) else type(body).__name__}"
                ),
            )
        return ProbeResult(
            ok=True,
            model_count=len(items),
            error=None,
            models=[_normalise_probed_model(m) for m in items if isinstance(m, dict)],
        )

    async def resolve_admin_tier_only(self) -> ResolvedLmStudioConfig:
        """Resolve the effective admin/env-tier config without user override.

        Used by the lifespan boot-time rewire path so the saved admin
        default takes effect on container restart without requiring a
        fake user_id (the previous ``resolve(user_id=0)`` hack relied on
        no real user having ID 0).

        Returns the same :class:`ResolvedLmStudioConfig` shape as
        :meth:`resolve` but with ``source_base_url`` and
        ``source_api_key`` ∈ ``{"server_admin", "env"}`` only (never
        ``"user"``).
        """
        admin_row = await self._fetch_admin_default()

        base_url, source_base_url = self._pick(
            field="base_url",
            user_value=None,
            admin_value=admin_row.base_url if admin_row else None,
        )
        default_model, source_default_model = self._pick(
            field="default_model",
            user_value=None,
            admin_value=admin_row.default_model if admin_row else None,
        )

        # Resolve the api_key using the admin/env path only (no user row).
        if admin_row and admin_row.api_key_enc:
            try:
                api_key = decrypt(
                    admin_row.api_key_enc,
                    kind=_API_KEY_KIND_ADMIN,
                    record_id=_ADMIN_RECORD_ID,
                ).decode()
                source_api_key = "server_admin"
            except Exception as exc:  # noqa: BLE001
                # See the matching user-side log above for the
                # "empty str(exc)" rationale — include error_type so
                # the underlying exception class is identifiable.
                log.error(
                    "lm_studio_overrides.admin_api_key_decrypt_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                # Env api_key is no longer an auto-fallback.
                api_key = ""
                source_api_key = "unset"
        else:
            # Env api_key is no longer an auto-fallback. Admin must promote env values explicitly.
            api_key = ""
            source_api_key = "unset"

        return ResolvedLmStudioConfig(
            base_url=base_url,
            api_key=api_key,
            default_model=default_model,
            source_base_url=source_base_url,
            source_api_key=source_api_key,
            source_default_model=source_default_model,
            api_key_set=bool(api_key),
        )

    async def rewire_singletons(
        self,
        app_state: Any,  # FastAPI app.state — typed as Any to avoid import cycle
        *,
        new_base_url: str,
        new_api_key: str,
    ) -> None:
        """Atomically swap the six singletons to a new httpx.AsyncClient.

        Constructs a fresh ``httpx.AsyncClient`` pointed at ``new_base_url``
        with ``new_api_key``, then — inside ``app_state.rewire_lock`` — mutates
        all five singleton references in a single asyncio scheduling point (no
        ``await`` between assignments).  Also invalidates ``ModelsService``'s
        model cache so the next request sees the new URL.

        The old client is scheduled for close after ``OLD_CLIENT_GRACE_SECONDS``
        (180 s) so in-flight streams can drain.

        LOCK ORDERING INVARIANT:
          ``rewire_lock`` → ``_cache_lock`` is the ONLY permitted acquisition
          order.  Code that holds ``_cache_lock`` must NEVER attempt to acquire
          ``rewire_lock``.  Reversing this order would deadlock.

        Args:
            app_state:    ``app.state`` from the FastAPI application — holds
                          all singleton references.
            new_base_url: The new LM Studio base URL.  Trailing slash is
                          normalised away (matches ``LmstudioAdapter.__init__``
                          behaviour).
            new_api_key:  The new bearer token.  Empty string means no auth.
        """
        # Normalise trailing slash (admin may save "http://foo:1234/"
        # which would produce "//api/v1/chat" in URL construction
        # without this step).
        new_base_url = new_base_url.rstrip("/")

        old_http_client = app_state.http_client
        auth_headers = (
            {"Authorization": f"Bearer {new_api_key}"} if new_api_key else {}
        )
        new_http_client = httpx.AsyncClient(
            base_url=new_base_url,
            headers=auth_headers,
            # read must track lmstudio_adapter.CHAT_TIMEOUT's read leg (1800 s
            # / 30 min, local-first) — this client replaces app_state.http_client,
            # so a stale short value here would silently reintroduce a hard cap
            # on every chat request after the next admin URL rewire.
            timeout=httpx.Timeout(connect=10.0, read=1800.0, write=60.0, pool=10.0),
        )

        # rewire_lock → _cache_lock (invariant documented above).
        async with app_state.rewire_lock:
            # The attribute assignments are atomic from any other
            # coroutine's perspective in asyncio's single-threaded event
            # loop. The `async with _cache_lock` below IS an await
            # point — see auth_failed reset placement note after the
            # cache invalidation.
            app_state.http_client = new_http_client
            app_state.http = new_http_client  # backward-compat alias
            app_state.lmstudio_adapter._http_client = new_http_client
            app_state.lmstudio_adapter._base_url = new_base_url
            app_state.models_service._http_client = new_http_client
            app_state.models_service._base_url = new_base_url
            app_state.embedding_client._http = new_http_client
            app_state.embedding_client._base_url = new_base_url
            app_state.embedding_client._endpoint = (
                f"{new_base_url}{EMBEDDINGS_PATH_CONST}"
            )
            # WebSearchService rebinding (old _http would leak after
            # OLD_CLIENT_GRACE_SECONDS, causing RuntimeError on probe).
            app_state.web_search_service.reconfigure(http_client=new_http_client)
            # ProviderRegistry rebinding (sixth singleton).
            # Cloud providers built by ProviderRegistry.refresh() capture
            # self._http_client at construction time.  Without this rebind,
            # they hold the old (eventually-closed) client after a rewire,
            # causing RuntimeError on /api/providers/status probes.
            # getattr guard: unit-test stubs may not set provider_registry.
            _provider_registry = getattr(app_state, "provider_registry", None)
            if _provider_registry is not None:
                await _provider_registry.reconfigure_http_client(new_http_client)

            # Invalidate models cache so the next list_loaded() call fetches
            # from the new URL.
            async with app_state.models_service._cache_lock:
                app_state.models_service._cache = None

            # The auth_failed reset MUST live AFTER the `async with
            # _cache_lock` block, not before it. Placement-after-await
            # is the only race-free position.
            #
            # Why: if reset is placed before `_cache_lock` is acquired,
            # the suspension at the `async with _cache_lock` above lets
            # an in-flight refresh on the OLD client run, receive a
            # 401, set `_auth_failed=True` via
            # models_service.refresh()'s except branch, and release
            # _cache_lock; rewire then resumes, invalidates the cache,
            # and exits — leaving the new client paired with a stale
            # `_auth_failed=True` flag. Exact re-introduction of the
            # "added the key, says connected, no models populate" symptom.
            #
            # The new client and base_url have already been swapped in
            # above (no await between the assignments), so by the time
            # we reach this reset, every reader sees the new client.
            # Resetting the flag here pins it to the new credentials.
            app_state.models_service._auth_failed = False
            app_state.models_service._auth_failed_at = 0.0
            # Mirror flag the FE banner reads via /api/lm-studio/status.
            # Without this, the banner would stay "auth_failed=true" for
            # up to ``models_cache_refresh_interval_seconds`` (default
            # 30 minutes) after a correct save — the periodic loop is
            # the only other writer of this mirror.
            app_state.lm_studio_auth_failed = False

        # Schedule old client close OUTSIDE the lock so concurrent reads
        # cannot see a momentarily-closed client.
        # spawn_background_task holds a strong ref — a bare create_task()
        # here is only weakly referenced by the loop, so under GC pressure
        # the close could be silently dropped, leaking the old client's
        # socket pool for the rest of the process lifetime.
        spawn_background_task(
            _delayed_close(old_http_client, OLD_CLIENT_GRACE_SECONDS),
            name="lmstudio_old_client_close",
        )

        # Warm-up refresh so the next /api/models call returns real data
        # instead of triggering a cold cache-miss probe. Without this the
        # admin waits up to 25s for the FE poll cycle to refill the cache.
        #
        # Two structural requirements:
        #
        # 1. The mirror flag (``app_state.lm_studio_auth_failed``) is
        #    written ONLY here and by the 30-min periodic loop. If the
        #    warm-up 401s (e.g. key rotated again between probe and
        #    rewire), `_auth_failed` re-sets True on the service but
        #    the mirror stays False → FE banner says "connected" while
        #    models stay empty for up to 30 minutes. Sync the mirror
        #    inside the wrapper after refresh completes.
        #
        # 2. Bare `asyncio.create_task` returns a Task that only the
        #    event loop holds via a weak reference — under memory
        #    pressure it can be collected mid-flight, and any
        #    exception surfaces only at GC time as "Task exception
        #    was never retrieved". Hold the reference on app.state.
        async def _warmup_and_sync_mirror() -> None:
            try:
                await app_state.models_service.refresh()
            finally:
                # Mirror always tracks truth — even if refresh raised
                # something its own except branches didn't catch
                # (currently impossible per refresh's catch list, but
                # belt-and-braces).
                app_state.lm_studio_auth_failed = (
                    app_state.models_service.auth_failed
                )

        app_state.lmstudio_warmup_refresh_task = asyncio.create_task(
            _warmup_and_sync_mirror(),
            name="lmstudio_rewire_warmup_refresh",
        )

        log.info(
            "lm_studio_overrides.rewire_complete",
            new_base_url=new_base_url,
            api_key_set=bool(new_api_key),
        )


async def resolve_lm_studio_endpoint_mode(*, engine: AsyncEngine) -> str:
    """Read ``lm_studio_endpoint_mode`` directly, for hot-path callers.

    Free-function mirror of :meth:`LmStudioOverridesService.fetch_endpoint_mode`
    for callers (``streaming_service.py``) that only hold an ``engine``
    reference, not a full service instance — same pattern as
    ``models_service.resolve_background_model_id``.

    FAIL-SOFT: any DB error falls back to ``"native"`` (the always-safe,
    currently-working mode) so a read hiccup never breaks the user's turn.

    Args:
        engine: Async SQLAlchemy engine for reading the admin row.

    Returns:
        ``"native"`` or ``"openai_compat"``.
    """
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(server_lm_studio_default.c.lm_studio_endpoint_mode).where(
                        server_lm_studio_default.c.id == _ADMIN_RECORD_ID
                    )
                )
            ).first()
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "lm_studio_endpoint_mode.resolve_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return "native"
    if row is None or not row.lm_studio_endpoint_mode:
        return "native"
    if row.lm_studio_endpoint_mode not in _VALID_ENDPOINT_MODES:
        return "native"
    return row.lm_studio_endpoint_mode


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _validate_clear(clear: Sequence[str] | None) -> None:
    """Reject unknown field names in ``clear``."""
    if clear is None:
        return
    unknown = [f for f in clear if f not in _CLEARABLE_FIELDS]
    if unknown:
        raise ValueError(f"unknown clear field(s): {unknown!r}")


def _reject_empty_values(
    *,
    base_url: str | None,
    api_key: str | None,
    default_model: str | None,
) -> None:
    """Empty strings are not a valid write value (use ``clear`` to wipe)."""
    for name, value in (
        ("base_url", base_url),
        ("api_key", api_key),
        ("default_model", default_model),
    ):
        if value is not None and value == "":
            raise ValueError(
                f"{name!r}: empty string is not a valid write value; "
                f"use the 'clear' field to wipe."
            )


def _apply_field(
    *,
    current_value: str | None,
    new_value: str | None,
    clear: set[str],
    field: str,
) -> str | None:
    """Compute the next value for a patch field.

    Rules:
      - field in clear → None.
      - new_value is None → preserve current_value.
      - otherwise → new_value.
    """
    if field in clear:
        return None
    if new_value is None:
        return current_value
    return new_value


async def _delayed_close(client: httpx.AsyncClient, delay_sec: float) -> None:
    """Close ``client`` after ``delay_sec`` seconds.

    Scheduled via ``asyncio.create_task`` immediately after a rewire so
    in-flight streams can drain on the old connection pool before it is
    torn down.  Errors on ``aclose`` are logged at WARNING and swallowed —
    the close is best-effort (the process will GC the client eventually).
    """
    await asyncio.sleep(delay_sec)
    try:
        await client.aclose()
    except Exception as exc:  # noqa: BLE001
        log.warning("lm_studio_overrides.old_client_aclose_failed", error=str(exc))
