# SPDX-License-Identifier: Apache-2.0
"""Per-preset model/provider default service (W5).

Stores and retrieves the admin's per-preset model defaults on the
``user_prefs.preset_models`` JSON column added in migration 0031.

Shape on disk::

    {
        "general":    {"provider": "lmstudio", "model_id": "phi-4"},
        "research":   {"provider": "openrouter", "model_id": "qwen/..."},
        ...
    }

``NULL`` / ``{}`` → no per-preset defaults (fall back to the caller's
top-bar model, which is today's behavior).

Provider validation
-------------------
``set_preset_models`` validates each entry's provider slug against the
live ``ProviderRegistry``.  Entries whose provider is not registered
(and is not the implicit ``"lmstudio"`` built-in) are **dropped** with a
warning rather than hard-rejecting the whole payload — this matches the
pattern in the providers route and avoids stranding a half-configured
admin on a save failure.  Unknown providers in the existing DB value are
never surfaced to callers; they are filtered out at write time.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import user_prefs as user_prefs_table
from lmchat.logging import get_logger
from lmchat.services._user_prefs_upsert import user_prefs_upsert

log = get_logger(__name__)

# Sentinel used to distinguish "drop empty dict" from a true parse failure.
_EMPTY: dict[str, Any] = {}

# The built-in provider that is always valid even without a registry entry.
_LMSTUDIO_SLUG: str = "lmstudio"


class PresetModelsService:
    """CRUD for per-preset model/provider defaults.

    Args:
        engine: Async SQLAlchemy engine connected to the application DB.
        provider_registry: The live ``ProviderRegistry`` used to validate
            provider slugs at write time.  May be ``None`` in test
            environments without a registry; when ``None``, all provided
            slugs are accepted without validation.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        provider_registry: object | None = None,
    ) -> None:
        self._engine = engine
        self._provider_registry = provider_registry

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_preset_models(self, user_id: int) -> dict[str, Any]:
        """Return the preset→{provider, model_id} mapping for *user_id*.

        Returns an empty dict when the row does not exist or the column
        is NULL / empty.

        Args:
            user_id: Owning user's PK.

        Returns:
            Mapping of preset id → ``{"provider": str, "model_id": str}``.
            Empty dict when no defaults have been configured.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(user_prefs_table.c.preset_models).where(
                        user_prefs_table.c.user_id == user_id
                    )
                )
            ).fetchone()

        if row is None or row[0] is None:
            return {}

        raw = row[0]
        return _parse_mapping(raw)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def set_preset_models(
        self, user_id: int, mapping: dict[str, Any]
    ) -> dict[str, Any]:
        """Upsert *mapping* as the preset model defaults for *user_id*.

        Unknown providers (not in the registry and not ``"lmstudio"``)
        are silently dropped.  An empty or all-dropped mapping clears the
        column (sets to ``NULL``).

        Args:
            user_id: Owning user's PK.
            mapping: Raw preset→entry dict from the caller.  Each entry
                should have ``"provider"`` and ``"model_id"`` string keys.

        Returns:
            The sanitised mapping that was persisted.
        """
        sanitised = self._sanitise(mapping)
        value: dict[str, Any] | None = sanitised if sanitised else None

        async def _do() -> None:
            async with self._engine.begin() as conn:
                # Only this caller's own row is ever affected: `folders`
                # is set to `[]` on first-row creation (this service
                # never touches an existing `folders` value — only the
                # `preset_models` column is ever updated on conflict).
                await user_prefs_upsert(
                    conn,
                    user_id,
                    insert_extra={"folders": [], "preset_models": value},
                    update_values={"preset_models": value},
                )

        await with_write_retry(_do)
        log.info(
            "preset_models.set",
            user_id=user_id,
            count=len(sanitised),
        )
        return sanitised

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sanitise(self, mapping: dict[str, Any]) -> dict[str, Any]:
        """Validate and filter *mapping*.

        Each value must be a dict with at least a ``"model_id"`` key.
        The ``"provider"`` key defaults to ``"lmstudio"`` when absent.
        Entries with an unknown provider are dropped.

        Args:
            mapping: Raw caller-supplied preset→entry dict.

        Returns:
            Filtered mapping containing only valid entries.
        """
        out: dict[str, Any] = {}
        for preset_id, entry in mapping.items():
            if not isinstance(entry, dict):
                log.warning(
                    "preset_models.entry_not_dict",
                    preset_id=preset_id,
                    type=type(entry).__name__,
                )
                continue
            provider = str(entry.get("provider") or _LMSTUDIO_SLUG)
            model_id = entry.get("model_id")
            if not model_id or not str(model_id).strip():
                log.warning(
                    "preset_models.entry_missing_model_id",
                    preset_id=preset_id,
                )
                continue
            if not self._provider_known(provider):
                log.warning(
                    "preset_models.unknown_provider_dropped",
                    preset_id=preset_id,
                    provider=provider,
                )
                continue
            out[preset_id] = {
                "provider": provider,
                "model_id": str(model_id).strip(),
            }
        return out

    def _provider_known(self, slug: str) -> bool:
        """Return ``True`` if *slug* is a valid/known provider."""
        if slug == _LMSTUDIO_SLUG:
            return True
        if self._provider_registry is None:
            # No registry in test environments — accept all slugs.
            return True
        return self._provider_registry.get(slug) is not None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_mapping(raw: Any) -> dict[str, Any]:  # noqa: ANN401
    """Parse *raw* (from the DB JSON column) into a clean dict.

    Handles both pre-decoded dicts (SQLAlchemy JSON type decodes for us
    on most dialects) and raw JSON strings (SQLite without JSON1 ext).

    Args:
        raw: Raw value from ``user_prefs.preset_models``.

    Returns:
        Validated dict; empty dict on any parse failure.
    """
    import json as _json

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = _json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except (ValueError, TypeError):
            return {}
    return {}
