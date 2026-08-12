# SPDX-License-Identifier: Apache-2.0
"""App-level admin settings resolver.

Promotes five env-only config flags to runtime admin overrides stored
in the ``server_lm_studio_default`` singleton row (id=1).

Resolver chain for each flag
-----------------------------
1. Read the column from ``server_lm_studio_default`` (id=1).
2. If the value is non-NULL → return it (explicit admin override).
3. If the value is NULL → fall back to ``get_settings().<flag>``.
4. Any error (no row, DB error, etc.) → fail-soft to the config default.

``repeat_warning_cut_k`` additionally sits under a PER-CHAT override
(``chats.settings.repeat_warning_cut_k``) that this module knows nothing
about — the full effective-K resolution chain (per-chat -> this global
admin default -> config default) is assembled by the caller
(``streaming_service.stream_chat``); this module only resolves the
global-admin half.

Admin endpoints
---------------
- ``GET /api/settings/app`` — returns the 5 resolved values with
  ``is_override`` flags.
- ``PATCH /api/settings/app`` — admin-only; sets or clears overrides.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.config import get_settings
from lmchat.db.schema import server_lm_studio_default
from lmchat.logging import get_logger

log = get_logger(__name__)


# ─── Internal helpers ─────────────────────────────────────────────────────────


async def _get_admin_row(engine: AsyncEngine) -> dict[str, Any] | None:
    """Fetch the admin singleton row (id=1).

    Returns None on any error (no row, DB error, etc.).
    """
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                select(
                    server_lm_studio_default.c.memory_distillation_enabled,
                    server_lm_studio_default.c.subsession_memory_distillation_enabled,
                    server_lm_studio_default.c.web_search_provider,
                    server_lm_studio_default.c.searxng_url,
                    server_lm_studio_default.c.repeat_warning_cut_k,
                ).where(server_lm_studio_default.c.id == 1)
            )
            row = result.fetchone()
            if row is None:
                return None
            return dict(row._mapping)
    except Exception:  # noqa: BLE001
        log.warning("app_settings.row_fetch_failed", exc_info=True)
        return None


async def _set_admin_column(engine: AsyncEngine, column: str, value: Any) -> None:
    """Set a single column on the admin singleton row (id=1).

    Uses an UPSERT pattern so the row is created if it doesn't exist.
    ``value=None`` clears the column (sets NULL).
    """
    try:
        async with engine.begin() as conn:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            dialect_name = conn.dialect.name  # type: ignore[attr-defined]
            insert_cls = pg_insert if dialect_name == "postgresql" else sqlite_insert

            # First, upsert the row (id=1) with the new value.
            # We need to preserve existing values for other columns.
            # Read existing row first.
            existing = await conn.execute(
                select(server_lm_studio_default).where(server_lm_studio_default.c.id == 1)
            )
            existing_row = existing.fetchone()

            if existing_row is None:
                # Create the row with only the specified column set.
                await conn.execute(
                    insert_cls(server_lm_studio_default).values(id=1, **{column: value})
                )
            else:
                # Update only the specified column.
                await conn.execute(
                    update(server_lm_studio_default)
                    .where(server_lm_studio_default.c.id == 1)
                    .values(**{column: value})
                )
    except Exception:  # noqa: BLE001
        log.warning("app_settings.set_column_failed", column=column, exc_info=True)
        raise


# ─── Resolver functions ───────────────────────────────────────────────────────


async def resolve_memory_distillation_enabled(engine: AsyncEngine) -> bool:
    """Return the effective memory distillation flag.

    DB override (non-NULL) → that value.
    Otherwise → ``get_settings().lm_chat_memory_distillation_enabled``.
    On error → config default.
    """
    row = await _get_admin_row(engine)
    if row is not None and row.get("memory_distillation_enabled") is not None:
        return bool(row["memory_distillation_enabled"])
    try:
        return get_settings().lm_chat_memory_distillation_enabled
    except Exception:  # noqa: BLE001
        return True  # Settings default


async def resolve_subsession_memory_distillation_enabled(
    engine: AsyncEngine,
) -> bool:
    """Return the effective subsession memory distillation flag.

    DB override (non-NULL) → that value.
    Otherwise → ``get_settings().lm_chat_subsession_memory_distillation_enabled``.
    On error → config default.
    """
    row = await _get_admin_row(engine)
    if row is not None and row.get("subsession_memory_distillation_enabled") is not None:
        return bool(row["subsession_memory_distillation_enabled"])
    try:
        return get_settings().lm_chat_subsession_memory_distillation_enabled
    except Exception:  # noqa: BLE001
        return False  # Settings default


async def resolve_web_search_provider(engine: AsyncEngine) -> str:
    """Return the effective web search provider.

    DB override (non-NULL) → that value.
    Otherwise → ``get_settings().lm_chat_web_search_provider``.
    On error → config default.
    """
    row = await _get_admin_row(engine)
    if row is not None and row.get("web_search_provider") is not None:
        return str(row["web_search_provider"])
    try:
        return get_settings().lm_chat_web_search_provider
    except Exception:  # noqa: BLE001
        return "searxng"  # Settings default


async def resolve_searxng_url(engine: AsyncEngine) -> str:
    """Return the effective SearXNG URL.

    DB override (non-NULL) → that value.
    Otherwise → ``get_settings().lm_chat_searxng_url``.
    On error → config default.
    """
    row = await _get_admin_row(engine)
    if row is not None and row.get("searxng_url") is not None:
        return str(row["searxng_url"])
    try:
        return get_settings().lm_chat_searxng_url
    except Exception:  # noqa: BLE001
        return "https://searx.be"  # Settings default


async def resolve_repeat_warning_cut_k(engine: AsyncEngine) -> int:
    """Return the effective GLOBAL tool-call repeat-loop cut threshold (K).

    This is the global-admin half of the resolution chain only — callers
    that need the full per-chat -> global -> config chain (i.e.
    ``streaming_service.stream_chat``) check ``chats.settings.
    repeat_warning_cut_k`` first and fall back to this resolver.

    DB override (non-NULL) → that value.
    Otherwise → ``get_settings().lm_chat_repeat_warning_cut_k``.
    On error → config default (16).
    """
    row = await _get_admin_row(engine)
    if row is not None and row.get("repeat_warning_cut_k") is not None:
        return int(row["repeat_warning_cut_k"])
    try:
        return get_settings().lm_chat_repeat_warning_cut_k
    except Exception:  # noqa: BLE001
        return 16  # Settings default


# ─── Admin write helpers ──────────────────────────────────────────────────────


async def set_memory_distillation_enabled(engine: AsyncEngine, value: bool | None) -> None:
    """Set or clear the memory distillation override."""
    await _set_admin_column(engine, "memory_distillation_enabled", value)


async def set_subsession_memory_distillation_enabled(
    engine: AsyncEngine, value: bool | None
) -> None:
    """Set or clear the subsession memory distillation override."""
    await _set_admin_column(engine, "subsession_memory_distillation_enabled", value)


async def set_web_search_provider(engine: AsyncEngine, value: str | None) -> None:
    """Set or clear the web search provider override."""
    await _set_admin_column(engine, "web_search_provider", value)


async def set_searxng_url(engine: AsyncEngine, value: str | None) -> None:
    """Set or clear the SearXNG URL override."""
    await _set_admin_column(engine, "searxng_url", value)


async def set_repeat_warning_cut_k(engine: AsyncEngine, value: int | None) -> None:
    """Set or clear the global repeat-loop cut threshold (K) override."""
    await _set_admin_column(engine, "repeat_warning_cut_k", value)
