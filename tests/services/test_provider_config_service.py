# SPDX-License-Identifier: Apache-2.0
"""Tests for ProviderConfigService (A4 — multi-provider credential persistence).

Covers:
- add_or_update (new) → get() returns decrypted API key.
- add_or_update (update) → overwrites values cleanly.
- list_all() returns api_key_set=True and NEVER exposes cleartext.
- delete() removes the row; get() returns None afterwards.
- Undecryptable api_key (tampered blob) → get() returns api_key=None,
  no exception raised (graceful InvalidTag handling).
- Migration table-presence test: upgrade to 0029 creates provider_configs.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Final

import alembic.command
import alembic.config
import pytest
import sqlalchemy as sa
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
        f"sqlite+aiosqlite:///{tmp_path}/provider_cfg_test.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_then_get_decrypts_api_key(tmp_path: Path) -> None:
    """add_or_update creates a row; get() returns the decrypted API key."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test-key",
            default_model="meta-llama/llama-3.1-8b-instruct",
            extra_headers={"HTTP-Referer": "http://localhost", "X-Title": "LMChat"},
            enabled=True,
        )
        view = await svc.get("openrouter")
        assert view is not None
        assert view.provider == "openrouter"
        assert view.base_url == "https://openrouter.ai/api/v1"
        assert view.api_key == "sk-or-test-key", (
            f"Expected 'sk-or-test-key', got {view.api_key!r}"
        )
        assert view.api_key_set is True
        assert view.default_model == "meta-llama/llama-3.1-8b-instruct"
        assert view.extra_headers == {
            "HTTP-Referer": "http://localhost",
            "X-Title": "LMChat",
        }
        assert view.enabled is True
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_add_new_provider_rolls_back_fully_when_key_encryption_fails(
    tmp_path: Path,
) -> None:
    """RED-ON-REVERT: a failure between INSERT and the api_key_enc UPDATE
    must never leave a keyless-but-enabled row behind (#21).

    add_or_update() for a brand-new provider used to insert the row
    (with the caller's real `enabled` value and api_key_enc=NULL) in its
    OWN committed transaction, then encrypt the key and UPDATE it in a
    SEPARATE transaction. Anything failing between those two committed
    transactions left the already-committed insert stuck around
    permanently — an enabled provider with no key, exactly the state a
    concurrent registry.refresh() must never observe.

    The fix runs insert-then-set-key as ONE transaction, so injecting a
    failure into the encryption step must roll back the insert too: no
    row should exist afterwards, not a keyless-enabled one.
    """
    import lmchat.services.provider_config_service as pcs_module
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)

        def _boom(*args: object, **kwargs: object) -> bytes:
            raise RuntimeError("simulated encryption failure")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pcs_module, "encrypt", _boom)
            with pytest.raises(RuntimeError, match="simulated encryption failure"):
                await svc.add_or_update(
                    provider="openai",
                    base_url="https://api.openai.com/v1",
                    api_key="sk-openai-secret",
                    enabled=True,
                )

        # No partial row must survive the rollback.
        view = await svc.get("openai")
        assert view is None, (
            "add_or_update() left a row behind after key encryption failed "
            "-- this is the keyless-enabled-row bug: a concurrent reader "
            "could observe an enabled provider with no api_key_enc"
        )
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_add_new_provider_retries_on_transient_lock_error(
    tmp_path: Path,
) -> None:
    """RED-ON-REVERT: a transient SQLITE_BUSY during insert+encrypt+update
    must be retried, not surfaced as an immediate failure.

    The insert-then-set-key sequence (see
    test_add_new_provider_rolls_back_fully_when_key_encryption_fails above)
    runs inside ONE transaction, but that transaction was not wrapped in
    ``with_write_retry`` — the same helper every other write path in this
    codebase uses for transient "database is locked" errors. Without the
    wrapper, a single transient lock error would propagate straight out of
    add_or_update() instead of being retried.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    import lmchat.services.provider_config_service as pcs_module
    from lmchat.db.schema import provider_configs
    from lmchat.services.provider_config_service import ProviderConfigService
    from lmchat.utils.encryption import encrypt as real_encrypt

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)

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
            mp.setattr(pcs_module, "encrypt", _flaky_encrypt)
            # Must NOT raise: with_write_retry retries the whole closure
            # (a fresh engine.begin() transaction) after the transient lock
            # error on the first attempt.
            await svc.add_or_update(
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key="sk-openai-secret",
                enabled=True,
            )

        assert len(calls) == 2, (
            "expected exactly 2 encrypt() calls (1 failed + 1 retried "
            "success); with_write_retry not being wired in would either "
            "raise on the first call or never retry at all — "
            f"got {len(calls)} call(s)"
        )

        # Atomicity preserved across the retry: exactly one correct row,
        # not a duplicate or partial row left behind by the failed attempt
        # (its transaction must have rolled back in full).
        async with eng.connect() as conn:
            result = await conn.execute(
                select(provider_configs).where(provider_configs.c.provider == "openai")
            )
            rows = result.fetchall()
        assert len(rows) == 1, f"expected exactly one row after retry; found {len(rows)}"

        view = await svc.get("openai")
        assert view is not None
        assert view.api_key == "sk-openai-secret"
        assert view.enabled is True
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_list_all_shows_api_key_set_no_cleartext(tmp_path: Path) -> None:
    """list_all() shows api_key_set=True but never exposes the cleartext key."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-openai-secret",
        )
        await svc.add_or_update(
            provider="groq",
            base_url="https://api.groq.com/openai/v1",
            # No api_key — groq row without key.
        )
        views = await svc.list_all()
        assert len(views) == 2

        by_provider = {v.provider: v for v in views}
        openai_view = by_provider["openai"]
        assert openai_view.api_key_set is True
        # Ensure cleartext is not leaked (safe view has no api_key attribute).
        assert not hasattr(openai_view, "api_key"), (
            "ProviderConfigSafeView must not expose api_key"
        )

        groq_view = by_provider["groq"]
        assert groq_view.api_key_set is False
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_update_overwrites_values(tmp_path: Path) -> None:
    """add_or_update on an existing provider overwrites all fields."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="old-key",
            enabled=True,
        )
        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="new-key",
            enabled=False,
        )
        view = await svc.get("openrouter")
        assert view is not None
        assert view.api_key == "new-key"
        assert view.enabled is False
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_delete_removes_row(tmp_path: Path) -> None:
    """delete() removes the row; get() returns None afterwards."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="groq",
            base_url="https://api.groq.com/openai/v1",
        )
        # Confirm it exists.
        assert await svc.get("groq") is not None
        await svc.delete("groq")
        # Should be gone.
        assert await svc.get("groq") is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_delete_nonexistent_is_noop(tmp_path: Path) -> None:
    """delete() on a missing provider is a no-op (no exception raised)."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        # Must not raise.
        await svc.delete("does-not-exist")
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_undecryptable_key_returns_none_no_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tampered or undecryptable api_key → get() returns api_key=None gracefully."""
    from lmchat.db.schema import provider_configs
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="real-key",
        )

        # Overwrite the api_key_enc with garbage (simulates a rotated secret
        # or tampered ciphertext — decryption will raise InvalidTag).
        async with eng.begin() as conn:
            await conn.execute(
                provider_configs.update()
                .where(provider_configs.c.provider == "openai")
                .values(api_key_enc="enc$v1$AAAAAAAAAAAAAAAAAAAAAAAAA=")
            )

        # get() must not raise — it should log an error and return api_key=None.
        view = await svc.get("openai")
        assert view is not None, "Row should still exist"
        assert view.api_key is None, (
            f"Expected api_key=None for undecryptable blob, got {view.api_key!r}"
        )
        # The blob is still present, so api_key_set should still be True.
        assert view.api_key_set is True
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_get_unknown_provider_returns_none(tmp_path: Path) -> None:
    """get() on a provider that doesn't exist returns None."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        result = await svc.get("unknown-provider")
        assert result is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_no_api_key_stored_when_not_provided(tmp_path: Path) -> None:
    """add_or_update with no api_key → api_key_set=False in both views."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="custom",
            base_url="http://localhost:8080/v1",
        )
        internal = await svc.get("custom")
        assert internal is not None
        assert internal.api_key is None
        assert internal.api_key_set is False

        safe_views = await svc.list_all()
        assert len(safe_views) == 1
        assert safe_views[0].api_key_set is False
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# Migration table-presence test (mirrors tests/db/test_migration_0028.py)
# ---------------------------------------------------------------------------


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    cfg = alembic.config.Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _upgrade(db: Path, rev: str = "head") -> None:
    alembic.command.upgrade(_alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev)


def _downgrade(db: Path, rev: str) -> None:
    alembic.command.downgrade(_alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev)


def _table_names(db: Path) -> set[str]:
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        return set(sa.inspect(eng).get_table_names())
    finally:
        eng.dispose()


def _column_info(db: Path, table: str) -> dict:
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        return {c["name"]: c for c in sa.inspect(eng).get_columns(table)}
    finally:
        eng.dispose()


def test_0029_creates_provider_configs_table(tmp_path: Path) -> None:
    """Upgrade to 0029 creates the provider_configs table."""
    db = tmp_path / "test_0029_upgrade.db"
    _upgrade(db, "0029")
    tables = _table_names(db)
    assert "provider_configs" in tables, (
        f"provider_configs missing after upgrade to 0029; tables={tables}"
    )


def test_0029_table_schema_columns(tmp_path: Path) -> None:
    """provider_configs has all expected columns after upgrade to 0029."""
    db = tmp_path / "test_0029_cols.db"
    _upgrade(db, "0029")
    cols = _column_info(db, "provider_configs")
    expected = {
        "id",
        "provider",
        "base_url",
        "api_key_enc",
        "default_model",
        "extra_headers",
        "enabled",
        "created_at",
        "updated_at",
    }
    missing = expected - set(cols)
    assert not missing, f"Missing columns: {missing}"
    # Nullability checks.
    assert cols["api_key_enc"]["nullable"] is True
    assert cols["default_model"]["nullable"] is True
    assert cols["extra_headers"]["nullable"] is True
    # base_url must be NOT NULL.
    assert cols["base_url"]["nullable"] is False


def test_0029_round_trip_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    """head → downgrade -1 (0028) → head is clean; table drops and re-appears."""
    db = tmp_path / "test_0029_roundtrip.db"
    _upgrade(db, "head")
    assert "provider_configs" in _table_names(db)

    _downgrade(db, "0028")
    assert "provider_configs" not in _table_names(db), (
        "Table still present after downgrade to 0028"
    )

    _upgrade(db, "head")
    assert "provider_configs" in _table_names(db)


def test_0029_insert_and_read(tmp_path: Path) -> None:
    """After upgrade to 0029, rows can be inserted and read from provider_configs."""
    db = tmp_path / "test_0029_insert.db"
    _upgrade(db, "0029")
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT INTO provider_configs (provider, base_url, enabled)"
            " VALUES ('openrouter', 'https://openrouter.ai/api/v1', 1)"
        )
        con.commit()
        row = con.execute(
            "SELECT provider, base_url, api_key_enc FROM provider_configs"
            " WHERE provider='openrouter'"
        ).fetchone()
        assert row is not None
        assert row[0] == "openrouter"
        assert row[1] == "https://openrouter.ai/api/v1"
        assert row[2] is None  # api_key_enc NULL as expected
    finally:
        con.close()


# ---------------------------------------------------------------------------
# allowed_models CRUD tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_models_persists_and_round_trips(tmp_path: Path) -> None:
    """add_or_update with allowed_models persists; both views return it."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test",
            allowed_models=["openai/gpt-4o", "meta-llama/llama-3.3-70b"],
        )
        # Internal view
        internal = await svc.get("openrouter")
        assert internal is not None
        assert internal.allowed_models == ["openai/gpt-4o", "meta-llama/llama-3.3-70b"]

        # Safe view via list_all()
        safe_views = await svc.list_all()
        assert len(safe_views) == 1
        assert safe_views[0].allowed_models == ["openai/gpt-4o", "meta-llama/llama-3.3-70b"]
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_allowed_models_null_when_not_provided(tmp_path: Path) -> None:
    """add_or_update with no allowed_models → allowed_models=None in both views."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="openai",
            base_url="https://api.openai.com/v1",
        )
        internal = await svc.get("openai")
        assert internal is not None
        assert internal.allowed_models is None

        safe_views = await svc.list_all()
        assert safe_views[0].allowed_models is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_allowed_models_empty_list_stored_as_null(tmp_path: Path) -> None:
    """add_or_update with allowed_models=[] is normalized to NULL (no-filter semantics)."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="groq",
            base_url="https://api.groq.com/openai/v1",
            allowed_models=[],
        )
        internal = await svc.get("groq")
        assert internal is not None
        # Empty list is normalized to None (NULL in DB) — same semantics as "all allowed".
        assert internal.allowed_models is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_allowed_models_update_overwrites(tmp_path: Path) -> None:
    """Updating a provider replaces the allowed_models list."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            allowed_models=["model-a"],
        )
        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            allowed_models=["model-b", "model-c"],
        )
        internal = await svc.get("openrouter")
        assert internal is not None
        assert internal.allowed_models == ["model-b", "model-c"]
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_update_without_api_key_preserves_existing_key(tmp_path: Path) -> None:
    """add_or_update on an existing row with api_key=None keeps the stored key."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        # Seed: set a key.
        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="original-secret",
            enabled=True,
        )
        # Edit: change only default_model, omit api_key.
        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=None,
            default_model="meta-llama/llama-3.3-70b",
            enabled=True,
        )
        view = await svc.get("openrouter")
        assert view is not None
        # Key must still be present and decryptable.
        assert view.api_key_set is True
        assert view.api_key == "original-secret"
        # The other field was updated.
        assert view.default_model == "meta-llama/llama-3.3-70b"
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_update_with_new_api_key_replaces_it(tmp_path: Path) -> None:
    """add_or_update on an existing row with a new api_key replaces the stored key."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="old-key",
            enabled=True,
        )
        await svc.add_or_update(
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="new-key",
            enabled=True,
        )
        view = await svc.get("openai")
        assert view is not None
        assert view.api_key_set is True
        assert view.api_key == "new-key"
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_new_provider_with_no_api_key_stores_null(tmp_path: Path) -> None:
    """Brand-new provider row with api_key=None stores NULL (api_key_set=False)."""
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)
        await svc.add_or_update(
            provider="groq",
            base_url="https://api.groq.com/openai",
            api_key=None,
            enabled=True,
        )
        view = await svc.get("groq")
        assert view is not None
        assert view.api_key_set is False
        assert view.api_key is None
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# base_url normalization — save -> get -> construct pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_url_trailing_v1_is_normalized(tmp_path: Path) -> None:
    """A base_url saved with OpenRouter's own documented trailing '/v1' and
    the equivalent form without it must converge on the SAME effective
    models URL once a live OpenAICompatProvider is built from the stored
    config.

    ProviderConfigService itself stores/returns base_url verbatim (see
    test_add_then_get_decrypts_api_key — 'https://openrouter.ai/api/v1'
    round-trips unchanged); normalization happens at OpenAICompatProvider
    construction (providers/openai_compat.py), which is what the live
    registry and the /test probe both build from the stored config. This
    test covers that full save -> get -> construct pipeline end to end so
    a doubled '.../api/v1/v1/models' path (confirmed live 404 for
    OpenRouter) cannot recur regardless of which equivalent form is saved.
    """
    from unittest.mock import MagicMock

    import httpx

    from lmchat.providers.openai_compat import OpenAICompatProvider
    from lmchat.services.provider_config_service import ProviderConfigService

    eng = await _make_engine(tmp_path)
    try:
        svc = ProviderConfigService(engine=eng)

        await svc.add_or_update(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="or-key",
        )
        await svc.add_or_update(
            provider="openrouter-no-v1",
            base_url="https://openrouter.ai/api",
            api_key="or-key",
        )

        stored_with_v1 = await svc.get("openrouter")
        stored_without_v1 = await svc.get("openrouter-no-v1")
        assert stored_with_v1 is not None
        assert stored_without_v1 is not None

        # The service itself stores/returns base_url verbatim, unchanged.
        assert stored_with_v1.base_url == "https://openrouter.ai/api/v1"
        assert stored_without_v1.base_url == "https://openrouter.ai/api"

        provider_with_v1 = OpenAICompatProvider(
            name="openrouter",
            base_url=stored_with_v1.base_url,
            api_key=stored_with_v1.api_key,
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        provider_without_v1 = OpenAICompatProvider(
            name="openrouter-no-v1",
            base_url=stored_without_v1.base_url,
            api_key=stored_without_v1.api_key,
            http_client=MagicMock(spec=httpx.AsyncClient),
        )

        # Both converge on the same effective base_url — and therefore the
        # same models URL — regardless of which form was saved.
        assert provider_with_v1._base_url == "https://openrouter.ai/api"  # noqa: SLF001
        assert provider_without_v1._base_url == "https://openrouter.ai/api"  # noqa: SLF001
        assert provider_with_v1._base_url == provider_without_v1._base_url  # noqa: SLF001
    finally:
        await eng.dispose()
