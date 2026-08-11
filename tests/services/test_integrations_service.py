# SPDX-License-Identifier: Apache-2.0
"""Unit tests for IntegrationsService.

Covers:
- env-only fallback (DB empty)
- DB-only (env empty)
- DB overrides env when DB has rows
- set_available atomicity (replaces list)
- add convenience (insert + idempotent existing)
- remove convenience (no-op on missing)
- sort_order preserved on retrieval
- empty list returns env fallback
- deduplication in env fallback
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.services.integrations_service import (
    IntegrationSetEntry,
    IntegrationsService,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def engine(tmp_path: Path) -> AsyncEngine:
    """Create a fresh in-memory SQLite engine with the mcp_integrations_list table."""
    import sqlalchemy as sa

    from lmchat.services.integrations_service import _TABLE_NAME

    db_url = f"sqlite+aiosqlite:///{tmp_path}/int_svc_test.db"
    eng = create_async_engine(db_url)
    async with eng.begin() as conn:
        await conn.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    value TEXT NOT NULL UNIQUE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    enabled_by_default INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        await conn.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_mcp_integrations_list_sort_order "
                f"ON {_TABLE_NAME}(sort_order)"
            )
        )
    return eng


@pytest_asyncio.fixture()
async def svc_env_only(engine: AsyncEngine) -> IntegrationsService:
    """Service with env defaults; DB is empty.

    `local_mcp_config=None` disables the file-discovery tier so the dev
    machine's ~/.lmstudio/mcp.json doesn't bleed into hermetic tests
    that only exercise the DB→env precedence path.
    """
    return IntegrationsService(
        engine=engine,
        env_default=["mcp/searxng", "mcp/filesystem"],
        local_mcp_config=None,
    )


@pytest_asyncio.fixture()
async def svc_no_env(engine: AsyncEngine) -> IntegrationsService:
    """Service with no env defaults; DB starts empty.

    File discovery disabled — see ``svc_env_only``.
    """
    return IntegrationsService(
        engine=engine,
        env_default=[],
        local_mcp_config=None,
    )


# ---------------------------------------------------------------------------
# env-only fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_available_env_fallback(svc_env_only: IntegrationsService) -> None:
    """list_available returns env defaults when DB is empty."""
    result = await svc_env_only.list_available()
    assert len(result) == 2
    assert result[0].value == "mcp/searxng"
    assert result[1].value == "mcp/filesystem"
    # Synthetic (fallback) entries carry negative ids — formerly the
    # literal sentinel -1; now stable per-value hashes in [-(2**31), -2]
    # so React keys and UI sorts don't collide. The "is fallback" check
    # remains `id < 0`.
    assert all(e.id < 0 for e in result)
    # Stable IDs: re-fetching produces the same id per value.
    second = await svc_env_only.list_available()
    assert [e.id for e in result] == [e.id for e in second]


@pytest.mark.asyncio
async def test_list_available_entries_are_all_lmstudio_source(
    svc_env_only: IntegrationsService,
) -> None:
    """Every curated entry from IntegrationsService.list_available() is
    ``source=="lmstudio"`` — this service only ever surfaces LM Studio's
    own mcp.json-backed servers; store=="store" entries are appended
    later, at the route layer (routes/integrations.py), not here.
    """
    result = await svc_env_only.list_available()
    assert result, "expected non-empty env-fallback result"
    assert all(e.source == "lmstudio" for e in result)


@pytest.mark.asyncio
async def test_list_available_empty_no_env(svc_no_env: IntegrationsService) -> None:
    """list_available returns empty list when DB is empty and env is empty."""
    result = await svc_no_env.list_available()
    assert result == []


# ---------------------------------------------------------------------------
# DB-only (env empty, DB populated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_available_db_only(engine: AsyncEngine) -> None:
    """list_available returns DB rows when populated, ignoring env default."""
    svc = IntegrationsService(
        engine=engine,
        env_default=["mcp/should-not-appear"],
        local_mcp_config=None,
    )
    await svc.set_available([
        IntegrationSetEntry(value="mcp/custom", sort_order=0),
        IntegrationSetEntry(value="mcp/another", sort_order=1),
    ])

    result = await svc.list_available()
    assert len(result) == 2
    values = [e.value for e in result]
    assert "mcp/custom" in values
    assert "mcp/another" in values
    # env default MUST NOT appear
    assert "mcp/should-not-appear" not in values
    # All entries should have real DB ids (> 0)
    assert all(e.id > 0 for e in result)


# ---------------------------------------------------------------------------
# DB overrides env
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_overrides_env(svc_env_only: IntegrationsService) -> None:
    """DB list overrides env default when DB has rows."""
    await svc_env_only.set_available([
        IntegrationSetEntry(value="mcp/db-entry", sort_order=0),
    ])
    result = await svc_env_only.list_available()
    assert len(result) == 1
    assert result[0].value == "mcp/db-entry"
    # env defaults should not appear
    assert all(e.value != "mcp/searxng" for e in result)


# ---------------------------------------------------------------------------
# set_available atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_available_replaces_list(svc_no_env: IntegrationsService) -> None:
    """set_available atomically replaces the list."""
    # Set initial list.
    await svc_no_env.set_available([
        IntegrationSetEntry(value="mcp/old-a", sort_order=0),
        IntegrationSetEntry(value="mcp/old-b", sort_order=1),
    ])
    # Replace with a different list.
    result = await svc_no_env.set_available([
        IntegrationSetEntry(value="mcp/new-only", sort_order=0),
    ])
    assert len(result) == 1
    assert result[0].value == "mcp/new-only"
    # Verify old entries are gone.
    fresh = await svc_no_env.list_available()
    assert len(fresh) == 1
    assert fresh[0].value == "mcp/new-only"


@pytest.mark.asyncio
async def test_set_available_empty_clears_db(svc_env_only: IntegrationsService) -> None:
    """set_available([]) clears DB, falling back to env default."""
    # First populate DB.
    await svc_env_only.set_available([
        IntegrationSetEntry(value="mcp/temporary", sort_order=0),
    ])
    # Then clear it.
    await svc_env_only.set_available([])
    # Now env default should be in effect.
    result = await svc_env_only.list_available()
    values = [e.value for e in result]
    assert "mcp/searxng" in values
    assert "mcp/filesystem" in values


# ---------------------------------------------------------------------------
# sort_order preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sort_order_preserved(svc_no_env: IntegrationsService) -> None:
    """list_available returns entries sorted by sort_order ASC."""
    await svc_no_env.set_available([
        IntegrationSetEntry(value="mcp/z", sort_order=10),
        IntegrationSetEntry(value="mcp/a", sort_order=0),
        IntegrationSetEntry(value="mcp/m", sort_order=5),
    ])
    result = await svc_no_env.list_available()
    assert [e.value for e in result] == ["mcp/a", "mcp/m", "mcp/z"]
    assert [e.sort_order for e in result] == [0, 5, 10]


# ---------------------------------------------------------------------------
# add convenience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_inserts_new(svc_no_env: IntegrationsService) -> None:
    """add inserts a new entry and returns it."""
    entry = await svc_no_env.add("mcp/new-one", sort_order=0)
    assert entry.value == "mcp/new-one"
    assert entry.sort_order == 0
    assert entry.id > 0

    result = await svc_no_env.list_available()
    assert any(e.value == "mcp/new-one" for e in result)


@pytest.mark.asyncio
async def test_add_idempotent_existing(svc_no_env: IntegrationsService) -> None:
    """add returns existing row when value already present."""
    first = await svc_no_env.add("mcp/existing", sort_order=0)
    second = await svc_no_env.add("mcp/existing", sort_order=99)
    assert first.id == second.id
    assert second.sort_order == first.sort_order  # unchanged


@pytest.mark.asyncio
async def test_add_auto_sort_order(svc_no_env: IntegrationsService) -> None:
    """add without sort_order uses max(sort_order)+1."""
    await svc_no_env.add("mcp/first", sort_order=5)
    entry = await svc_no_env.add("mcp/second")
    assert entry.sort_order == 6


# ---------------------------------------------------------------------------
# remove convenience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_deletes_entry(svc_no_env: IntegrationsService) -> None:
    """remove deletes the given entry from the DB."""
    await svc_no_env.add("mcp/to-delete", sort_order=0)
    await svc_no_env.remove("mcp/to-delete")
    result = await svc_no_env.list_available()
    assert all(e.value != "mcp/to-delete" for e in result)


@pytest.mark.asyncio
async def test_remove_noop_on_missing(svc_no_env: IntegrationsService) -> None:
    """remove does not raise when entry is not in DB."""
    await svc_no_env.remove("mcp/does-not-exist")  # must not raise


# ---------------------------------------------------------------------------
# env deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_deduplication(engine: AsyncEngine) -> None:
    """Duplicate values in env_default appear only once in the fallback list."""
    svc = IntegrationsService(
        engine=engine,
        env_default=["mcp/dupe", "mcp/unique", "mcp/dupe"],
        local_mcp_config=None,
    )
    result = await svc.list_available()
    values = [e.value for e in result]
    assert values.count("mcp/dupe") == 1
    assert "mcp/unique" in values


# ---------------------------------------------------------------------------
# local mcp.json discovery (tier 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_mcp_config_supplies_values_when_db_empty(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """When DB is empty and mcp.json exists, server names become ``mcp/<name>``."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({
            "mcpServers": {
                "context7": {"command": "x"},
                "searxng": {"command": "y"},
            }
        }),
        encoding="utf-8",
    )
    svc = IntegrationsService(
        engine=engine,
        env_default=["mcp/should-not-appear"],
        local_mcp_config=cfg,
    )
    result = await svc.list_available()
    values = [e.value for e in result]
    assert values == ["mcp/context7", "mcp/searxng"]
    # Synthetic entries carry NEGATIVE stable ids (formerly all -1,
    # now per-value hashes). ``enabled_by_default`` defaults to False —
    # discover-but-off, the public-launch-safe posture: discovered MCP
    # servers are listed and available for the user to pick per-message,
    # but nothing is pre-armed on a fresh install (no explicit
    # ``synthetic_enabled_by_default`` passed here, so this exercises
    # the service's own built-in default, which mirrors
    # ``Settings.lm_chat_default_integrations_enabled_by_default``'s
    # default of False).
    assert all(e.id < 0 and e.enabled_by_default is False for e in result)
    # And the streaming route's default-injection filter (routes/streaming.py:
    # ``[e.value for e in entries if e.enabled_by_default]``) therefore
    # resolves to an empty set — a fresh install injects no tools into a
    # new chat unless the user explicitly opts in per-message.
    assert [e.value for e in result if e.enabled_by_default] == []


@pytest.mark.asyncio
async def test_local_mcp_config_pre_selected_when_opted_in(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """``synthetic_enabled_by_default=True`` restores the old pre-selected
    behavior (the admin opt-in lever, e.g. same-host single-admin
    deployments that want zero-click tool availability)."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"context7": {"command": "x"}}}),
        encoding="utf-8",
    )
    svc = IntegrationsService(
        engine=engine,
        env_default=[],
        local_mcp_config=cfg,
        synthetic_enabled_by_default=True,
    )
    result = await svc.list_available()
    assert result and all(e.enabled_by_default is True for e in result)


@pytest.mark.asyncio
async def test_local_mcp_config_skipped_when_missing(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """No mcp.json → env fallback runs without raising."""
    missing = tmp_path / "no-such.json"
    svc = IntegrationsService(
        engine=engine,
        env_default=["mcp/env-only"],
        local_mcp_config=missing,
    )
    result = await svc.list_available()
    assert [e.value for e in result] == ["mcp/env-only"]


@pytest.mark.asyncio
async def test_local_mcp_config_skipped_when_malformed(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Garbage in mcp.json doesn't propagate — env fallback wins."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{ not valid json", encoding="utf-8")
    svc = IntegrationsService(
        engine=engine,
        env_default=["mcp/env-only"],
        local_mcp_config=cfg,
    )
    result = await svc.list_available()
    assert [e.value for e in result] == ["mcp/env-only"]


@pytest.mark.asyncio
async def test_local_mcp_config_rejects_unsafe_server_names(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Server names that fail the allowlist gate are dropped, not echoed."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({
            "mcpServers": {
                "ok-name": {},
                "../escape": {},
                "has spaces": {},
                "<script>": {},
            }
        }),
        encoding="utf-8",
    )
    svc = IntegrationsService(
        engine=engine,
        env_default=[],
        local_mcp_config=cfg,
    )
    result = await svc.list_available()
    assert [e.value for e in result] == ["mcp/ok-name"]


@pytest.mark.asyncio
async def test_db_rows_still_win_over_local_mcp_config(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """DB precedence is preserved — file is only read when DB is empty."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"file-only": {}}}),
        encoding="utf-8",
    )
    svc = IntegrationsService(
        engine=engine,
        env_default=[],
        local_mcp_config=cfg,
    )
    await svc.set_available([
        IntegrationSetEntry(value="mcp/db-wins", sort_order=0),
    ])
    result = await svc.list_available()
    assert [e.value for e in result] == ["mcp/db-wins"]
