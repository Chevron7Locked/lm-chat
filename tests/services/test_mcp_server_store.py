# SPDX-License-Identifier: Apache-2.0
"""Tests for McpServerStore (Workstream C — MCP Store backend).

Covers:
- install → list_all returns the new entry.
- install duplicate slug → ValueError.
- install with secrets → get() returns decrypted plaintext values.
- delete → list_all returns empty.
- Multiple secrets round-trip: all values survive encrypt/decrypt.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata

_SECRET: Final[str] = "test-secret-32-bytes-of-entropy!!"

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _seed_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure LM_CHAT_SECRET is set; encryption requires it."""
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SECRET", _SECRET)
    get_settings.cache_clear()


async def _make_engine(tmp_path: Path) -> AsyncEngine:
    """Return a temp SQLite engine with the current metadata schema."""
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/mcp_store_test.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_and_list(tmp_path: Path) -> None:
    """install() creates a row; list_all() returns it."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        view = await store.install(
            slug="github",
            name="GitHub",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            source="official",
            trust="curated",
        )
        assert view.slug == "github"
        assert view.name == "GitHub"
        assert view.transport == "stdio"
        assert view.source == "official"
        assert view.trust == "curated"

        all_views = await store.list_all()
        assert len(all_views) == 1
        assert all_views[0].slug == "github"
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_install_duplicate_raises(tmp_path: Path) -> None:
    """install() with the same slug twice raises ValueError."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(slug="github", name="GitHub", transport="stdio")
        with pytest.raises(ValueError, match="already installed"):
            await store.install(slug="github", name="GitHub 2", transport="stdio")
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_install_with_secrets_rolls_back_fully_when_encryption_fails(
    tmp_path: Path,
) -> None:
    """RED-ON-REVERT: a failure between INSERT and the secrets_enc UPDATE
    must never leave a keyless-but-enabled row behind (#19).

    install() used to insert the row (with the caller's real `enabled`
    value and secrets_enc=NULL) in its OWN committed transaction, then
    encrypt the secrets and UPDATE them in a SEPARATE transaction. A crash
    or error between those two committed transactions left the
    already-committed insert stuck around permanently — an enabled server
    with no secrets that ``list_host_configs()`` (B4 rehydration) would
    silently pick up and connect keyless, with nothing surfaced to the
    admin.

    The fix runs insert-then-set-secrets as ONE transaction, so injecting
    a failure into the encryption step must roll back the insert too: no
    row should exist afterwards, and it must not appear in
    list_host_configs() either.
    """
    import lmchat.services.mcp_server_store as store_module
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)

        def _boom(*args: object, **kwargs: object) -> bytes:
            raise RuntimeError("simulated encryption failure")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store_module, "encrypt", _boom)
            with pytest.raises(RuntimeError, match="simulated encryption failure"):
                await store.install(
                    slug="github",
                    name="GitHub",
                    transport="stdio",
                    command="npx",
                    args=["-y", "@modelcontextprotocol/server-github"],
                    secrets={"GITHUB_TOKEN": "ghp_test_token_abc123"},
                    enabled=True,
                )

        # No partial row must survive the rollback.
        view = await store.get("github")
        assert view is None, (
            "install() left a row behind after secret encryption failed -- "
            "this is the keyless-enabled-row bug: a concurrent reader "
            "(or B4 rehydration on restart) could observe an enabled "
            "server with no secrets_enc"
        )

        # And it must not silently rehydrate as a keyless host config.
        configs = await store.list_host_configs()
        assert configs == [], (
            "a partially-installed row must not be rehydrated by "
            "list_host_configs() as a keyless server"
        )
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_install_with_secrets_retries_on_transient_lock_error(
    tmp_path: Path,
) -> None:
    """RED-ON-REVERT: a transient SQLITE_BUSY during insert+encrypt+update
    must be retried, not surfaced as an immediate failure.

    The insert-then-set-secrets sequence (see
    test_install_with_secrets_rolls_back_fully_when_encryption_fails above)
    runs inside ONE transaction, but that transaction was not wrapped in
    ``with_write_retry`` — the same helper every other write path in this
    codebase uses for transient "database is locked" errors. Without the
    wrapper, a single transient lock error would propagate straight out of
    install() instead of being retried.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    import lmchat.services.mcp_server_store as store_module
    from lmchat.db.schema import mcp_servers
    from lmchat.services.mcp_server_store import McpServerStore
    from lmchat.utils.encryption import encrypt as real_encrypt

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)

        calls: list[int] = []

        def _flaky_encrypt(*args: object, **kwargs: object) -> str:
            calls.append(1)
            if len(calls) == 1:
                # A realistic SQLite "database is locked" error — the exact
                # sentinel with_write_retry matches on (see tests/db/test_retry.py).
                raise OperationalError(
                    "statement",
                    {},
                    Exception("(sqlite3.OperationalError) database is locked"),
                )
            return real_encrypt(*args, **kwargs)  # type: ignore[arg-type]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store_module, "encrypt", _flaky_encrypt)
            # Must NOT raise: with_write_retry retries the whole closure
            # (a fresh engine.begin() transaction) after the transient lock
            # error on the first attempt.
            view = await store.install(
                slug="github",
                name="GitHub",
                transport="stdio",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                secrets={"GITHUB_TOKEN": "ghp_test_token_abc123"},
                enabled=True,
            )

        assert len(calls) == 2, (
            "expected exactly 2 encrypt() calls (1 failed + 1 retried "
            "success); with_write_retry not being wired in would either "
            "raise on the first call or never retry at all — "
            f"got {len(calls)} call(s)"
        )
        assert view.secrets_set == ["GITHUB_TOKEN"]

        # Atomicity preserved across the retry: exactly one correct row,
        # not a duplicate or partial row left behind by the failed attempt
        # (its transaction must have rolled back in full).
        async with eng.connect() as conn:
            result = await conn.execute(
                select(mcp_servers).where(mcp_servers.c.slug == "github")
            )
            rows = result.fetchall()
        assert len(rows) == 1, f"expected exactly one row after retry; found {len(rows)}"

        got = await store.get("github")
        assert got is not None
        assert got.secrets == {"GITHUB_TOKEN": "ghp_test_token_abc123"}
        assert got.enabled is True
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_get_decrypts_secrets(tmp_path: Path) -> None:
    """install with secrets; get() returns decrypted plaintext values."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(
            slug="github",
            name="GitHub",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            secrets={"GITHUB_TOKEN": "ghp_test_token_abc123"},
        )

        internal = await store.get("github")
        assert internal is not None
        assert internal.slug == "github"
        assert "GITHUB_TOKEN" in internal.secrets
        assert internal.secrets["GITHUB_TOKEN"] == "ghp_test_token_abc123", (
            f"Expected 'ghp_test_token_abc123', got {internal.secrets.get('GITHUB_TOKEN')!r}"
        )
        assert "GITHUB_TOKEN" in internal.secrets_set
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_delete(tmp_path: Path) -> None:
    """install then delete; list_all() returns empty."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(slug="fetch", name="Fetch", transport="stdio")
        assert len(await store.list_all()) == 1

        await store.delete("fetch")
        assert len(await store.list_all()) == 0
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_list_host_configs_sse_secret_becomes_bearer_header(
    tmp_path: Path,
) -> None:
    """An sse server's secret is routed to an Authorization: Bearer header.

    http/sse transports have no child process, so a stored secret must become
    an ``Authorization: Bearer`` header (never a child env var).
    """
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(
            slug="crawl4ai",
            name="Crawl4AI",
            transport="sse",
            url="http://localhost:11235/mcp/sse",
            secrets={"CRAWL4AI_API_TOKEN": "tok-abc"},
            source="official",
            trust="curated",
        )
        configs = await store.list_host_configs()
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.transport == "sse"
        assert cfg.url == "http://localhost:11235/mcp/sse"
        assert cfg.env == {}
        assert cfg.headers == {"Authorization": "Bearer tok-abc"}
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_list_host_configs_stdio_secret_becomes_env(tmp_path: Path) -> None:
    """A stdio server's secret is passed as child env, with no headers."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(
            slug="brave-search",
            name="Brave Search",
            transport="stdio",
            command="npx",
            args=["-y", "@brave/brave-search-mcp-server"],
            secrets={"BRAVE_API_KEY": "bk-123"},
        )
        configs = await store.list_host_configs()
        cfg = configs[0]
        assert cfg.env == {"BRAVE_API_KEY": "bk-123"}
        assert cfg.headers == {}
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_list_host_configs_skips_corrupt_args_row(tmp_path: Path) -> None:
    """list_host_configs() skips a row with corrupted args and returns good rows.

    Validates FIX A: per-row try/except + args coercion guard.
    """
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)

        # Install two servers — one good, one whose args we'll corrupt below.
        await store.install(
            slug="good-server",
            name="Good Server",
            transport="stdio",
            command="npx",
            args=["-y", "good-pkg"],
        )
        await store.install(
            slug="bad-server",
            name="Bad Server",
            transport="stdio",
            command="npx",
            args=["-y", "bad-pkg"],
        )

        # Monkeypatch _decrypt_secrets to raise for the bad row's id,
        # simulating a corrupt encrypted blob that cannot be decrypted.
        original_decrypt = store._decrypt_secrets

        async def _patched_decrypt(server_id: int, blob: list[dict] | None) -> dict:
            # Retrieve the bad-server's DB id so we can target it precisely.
            from sqlalchemy import text as _text

            async with eng.connect() as conn:
                row = (
                    await conn.execute(
                        _text("SELECT id FROM mcp_servers WHERE slug = 'bad-server'")
                    )
                ).fetchone()
            bad_id = int(row[0]) if row else -1
            if server_id == bad_id:
                raise ValueError("simulated corrupt secret blob")
            return await original_decrypt(server_id, blob)

        store._decrypt_secrets = _patched_decrypt  # type: ignore[method-assign]

        configs = await store.list_host_configs()

        # The corrupt row must be silently skipped; the good row must survive.
        slugs = [c.server_id for c in configs]
        assert "good-server" in slugs, f"good-server missing from {slugs}"
        assert "bad-server" not in slugs, "bad-server should have been skipped"
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_list_host_configs_excludes_credential_decrypt_failure(
    tmp_path: Path,
) -> None:
    """A row whose secret fails to decrypt is excluded from configs and
    reported via the ``credential_errors`` out-param — never rehydrated
    keyless.

    P2 SILENT-FAILURE regression: previously a failed per-key decrypt was
    only logged at ERROR inside ``_decrypt_secrets`` and the server was
    still rehydrated into the host's config with the secret silently
    missing from ``env`` — nothing was surfaced to the admin. Now the whole
    row is excluded and the caller (B4 rehydration in app.py) can mark the
    server errored via ``McpHost.record_credential_error``.
    """
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(
            slug="good-server",
            name="Good Server",
            transport="stdio",
            command="npx",
            args=["-y", "good-pkg"],
        )
        await store.install(
            slug="broken-server",
            name="Broken Server",
            transport="stdio",
            command="npx",
            args=["-y", "broken-pkg"],
            secrets={"API_KEY": "should-never-surface"},
        )

        # Simulate a genuine per-key decrypt failure (e.g. a rotated
        # LM_CHAT_SECRET or corrupt ciphertext): _decrypt_secrets already
        # swallows the exception internally and omits the key from its
        # result, so reproduce that exact contract rather than raising.
        original_decrypt = store._decrypt_secrets

        async def _patched_decrypt(
            row_id: int, secrets_enc: list[dict] | None
        ) -> dict[str, str]:
            result = await original_decrypt(row_id, secrets_enc)
            result.pop("API_KEY", None)
            return result

        store._decrypt_secrets = _patched_decrypt  # type: ignore[method-assign]

        credential_errors: dict[str, str] = {}
        configs = await store.list_host_configs(credential_errors=credential_errors)

        slugs = [c.server_id for c in configs]
        assert "good-server" in slugs, f"good-server missing from {slugs}"
        assert "broken-server" not in slugs, (
            "broken-server must be excluded, not rehydrated keyless"
        )
        assert "broken-server" in credential_errors
        assert "API_KEY" in credential_errors["broken-server"]

        # Backward-compat: omitting credential_errors must not raise, and
        # must still exclude the broken row.
        configs_no_out_param = await store.list_host_configs()
        assert "broken-server" not in [c.server_id for c in configs_no_out_param]
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_list_host_configs_coerces_non_list_args(tmp_path: Path) -> None:
    """list_host_configs() returns empty list for args when DB value is not a list.

    Validates the isinstance(row.args, list) guard in FIX A.
    """
    from sqlalchemy import text as _text

    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(
            slug="corrupt-args",
            name="Corrupt Args",
            transport="stdio",
            command="npx",
            args=["original"],
        )

        # Overwrite args with a non-list value directly in the DB.
        async with eng.begin() as conn:
            await conn.execute(
                _text(
                    "UPDATE mcp_servers SET args = '\"not-a-list\"'"
                    " WHERE slug = 'corrupt-args'"
                )
            )

        configs = await store.list_host_configs()
        assert len(configs) == 1
        # Non-list args must be coerced to [] without raising.
        assert configs[0].args == []
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_secrets_round_trip(tmp_path: Path) -> None:
    """Multiple secrets all survive the encrypt/decrypt round-trip."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        secrets = {
            "FIRECRAWL_API_KEY": "fc-key-secret-value",
            "FIRECRAWL_API_URL": "https://my.firecrawl.example.com",
        }
        await store.install(
            slug="firecrawl",
            name="Firecrawl",
            transport="stdio",
            secrets=secrets,
        )

        internal = await store.get("firecrawl")
        assert internal is not None
        for key, expected in secrets.items():
            assert key in internal.secrets, f"Key {key!r} missing from decrypted secrets"
            assert internal.secrets[key] == expected, (
                f"Value mismatch for {key!r}: expected {expected!r},"
                f" got {internal.secrets[key]!r}"
            )
        # secrets_set should list all keys.
        assert set(internal.secrets_set) == set(secrets.keys())
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# B4 tests — tool_policy, update_enabled, update_tool_policy, list_host_configs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_sets_consented_true(tmp_path: Path) -> None:
    """install() always sets consented=True (install=consent)."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        view = await store.install(slug="github", name="GitHub", transport="stdio")
        assert view.consented is True, "install() must always set consented=True"
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_tool_policy_round_trip(tmp_path: Path) -> None:
    """install with tool_policy; list_all and get() return the denylist."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        policy = ["firecrawl_scrape", "firecrawl_map"]
        view = await store.install(
            slug="firecrawl",
            name="Firecrawl",
            transport="stdio",
            tool_policy=policy,
        )
        assert view.tool_policy == policy, f"Expected {policy!r}, got {view.tool_policy!r}"

        # list_all should surface it.
        all_views = await store.list_all()
        assert len(all_views) == 1
        assert all_views[0].tool_policy == policy

        # Internal view via get() should also carry it.
        internal = await store.get("firecrawl")
        assert internal is not None
        assert internal.tool_policy == policy
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_update_tool_policy(tmp_path: Path) -> None:
    """update_tool_policy() replaces the denylist; empty list clears it."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(slug="firecrawl", name="Firecrawl", transport="stdio")

        # Set a denylist.
        updated = await store.update_tool_policy("firecrawl", ["firecrawl_scrape"])
        assert updated is not None
        assert updated.tool_policy == ["firecrawl_scrape"]

        # Replace with a different denylist.
        updated2 = await store.update_tool_policy(
            "firecrawl", ["firecrawl_scrape", "firecrawl_map"]
        )
        assert updated2 is not None
        assert updated2.tool_policy == ["firecrawl_scrape", "firecrawl_map"]

        # Clear (empty list → stored as null; returned as []).
        cleared = await store.update_tool_policy("firecrawl", [])
        assert cleared is not None
        assert cleared.tool_policy == []
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_update_enabled(tmp_path: Path) -> None:
    """update_enabled() toggles enabled; returns updated view."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        await store.install(slug="github", name="GitHub", transport="stdio")

        disabled = await store.update_enabled("github", False)
        assert disabled is not None
        assert disabled.enabled is False

        re_enabled = await store.update_enabled("github", True)
        assert re_enabled is not None
        assert re_enabled.enabled is True
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_list_host_configs_returns_decrypted_env(tmp_path: Path) -> None:
    """list_host_configs() returns McpServerConfig with decrypted secrets for enabled rows."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        # Install one enabled server with secrets and one disabled server.
        await store.install(
            slug="github",
            name="GitHub",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            secrets={"GITHUB_TOKEN": "ghp_test_token"},
            enabled=True,
        )
        await store.install(
            slug="firecrawl",
            name="Firecrawl",
            transport="stdio",
            enabled=False,
        )

        configs = await store.list_host_configs()
        # Only the enabled server should appear.
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.server_id == "github"
        assert cfg.transport == "stdio"
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "@modelcontextprotocol/server-github"]
        # Decrypted secret must be in env.
        assert cfg.env.get("GITHUB_TOKEN") == "ghp_test_token", (
            f"Decrypted secret missing or wrong: {cfg.env!r}"
        )
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_list_host_configs_empty(tmp_path: Path) -> None:
    """list_host_configs() returns [] when no servers are installed."""
    from lmchat.services.mcp_server_store import McpServerStore

    eng = await _make_engine(tmp_path)
    try:
        store = McpServerStore(engine=eng)
        configs = await store.list_host_configs()
        assert configs == []
    finally:
        await eng.dispose()
