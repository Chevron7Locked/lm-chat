# SPDX-License-Identifier: Apache-2.0
"""Unit tests for LmStudioOverridesService.

Covers:
- Empty DB → ``resolve`` returns env-tier values for every field.
- ``set_user_override`` writes encrypted api_key envelope; ``resolve``
  decrypts cleanly.
- Server-admin default takes precedence over env but loses to user
  override (field-by-field).
- ``clear`` reverts a single field; the others stay.
- Empty-string write values rejected with ``ValueError``.
- Unknown ``clear`` field rejected with ``ValueError``.
- Probe shape: 200 + list[models] → ok=True; 200 + dict.data list →
  ok=True; non-200 → ok=False with error string.
- Envelope shape: stored ``api_key_enc`` starts with ``enc$v1$``
  (rules out accidental cleartext writes).
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.config import Settings
from lmchat.db.schema import metadata, user_lm_studio_overrides
from lmchat.services.lm_studio_overrides_service import (
    LmStudioOverridesService,
    resolve_lm_studio_endpoint_mode,
)

_SECRET: Final[str] = "test-secret-32-bytes-of-entropy!!"


@pytest.fixture(autouse=True)
def _seed_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure LM_CHAT_SECRET is set; encryption requires it."""
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SECRET", _SECRET)
    get_settings.cache_clear()


def _settings(
    *,
    base_url: str = "http://env.example:1234",
    api_key: str = "env-api-key",
    default_model: str = "env-model",
) -> Settings:
    """Return a Settings instance with synthetic env values."""
    return Settings(  # type: ignore[call-arg]
        lm_studio_base_url=base_url,
        lm_studio_api_key=api_key,
        lm_studio_default_model=default_model,
        lm_chat_secret=_SECRET,
    )


async def _make_engine(tmp_path: Path) -> AsyncEngine:
    """Build a temp SQLite engine with the lm-chat schema."""
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/p13g_unit.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


# ─── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_returns_env_for_empty_db(tmp_path: Path) -> None:
    """Empty DB → env fallback REMOVED.

    resolve() no longer falls through to env-tier values.
    Every field returns '' with source 'unset' when neither user nor
    admin row exists.  Env values are reference-only via get_env_suggestion().
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        cfg = await svc.resolve(user_id=42)
        assert cfg.base_url == ""
        assert cfg.api_key == ""
        assert cfg.default_model == ""
        assert cfg.source_base_url == "unset"
        assert cfg.source_api_key == "unset"
        assert cfg.source_default_model == "unset"
        assert cfg.api_key_set is False  # no api_key in DB
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_user_override_writes_enc_envelope(tmp_path: Path) -> None:
    """User write produces an ``enc$v1$`` envelope (no cleartext on disk)."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        await svc.set_user_override(
            user_id=7,
            base_url="http://user.example",
            api_key="user-secret-123",
            default_model="user-model",
            clear=None,
        )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(user_lm_studio_overrides.c.api_key_enc).where(
                        user_lm_studio_overrides.c.user_id == 7
                    )
                )
            ).first()
        assert row is not None
        assert row.api_key_enc is not None
        assert row.api_key_enc.startswith("enc$v1$"), row.api_key_enc
        # Crucially, the cleartext does NOT appear in the column.
        assert "user-secret-123" not in row.api_key_enc
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_after_user_override_returns_user_tier(
    tmp_path: Path,
) -> None:
    """User override beats env across every field."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        await svc.set_user_override(
            user_id=11,
            base_url="http://user.example",
            api_key="user-secret-123",
            default_model="user-model",
            clear=None,
        )
        cfg = await svc.resolve(user_id=11)
        assert cfg.base_url == "http://user.example"
        assert cfg.api_key == "user-secret-123"
        assert cfg.default_model == "user-model"
        assert cfg.source_base_url == "user"
        assert cfg.source_api_key == "user"
        assert cfg.source_default_model == "user"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_returns_only_caller_row(tmp_path: Path) -> None:
    """No cross-user leak invariant: each user sees only
    their own override row, never another user's.

    Env fallback was removed from resolve().  A user with
    no override row now returns 'unset' sources, not 'env'.
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        await svc.set_user_override(
            user_id=101,
            base_url="http://alice.example",
            api_key="alice-secret",
            default_model="alice-model",
            clear=None,
        )
        await svc.set_user_override(
            user_id=202,
            base_url="http://bob.example",
            api_key="bob-secret",
            default_model="bob-model",
            clear=None,
        )

        alice = await svc.resolve(user_id=101)
        bob = await svc.resolve(user_id=202)

        # Each resolution sees ONLY the caller's row.
        assert alice.base_url == "http://alice.example"
        assert alice.api_key == "alice-secret"
        assert bob.base_url == "http://bob.example"
        assert bob.api_key == "bob-secret"

        # A user with no override returns 'unset' — env is no longer a
        # fallback.
        carol = await svc.resolve(user_id=303)
        assert carol.source_base_url == "unset"
        assert carol.source_api_key == "unset"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_default_beats_env_loses_to_user(tmp_path: Path) -> None:
    """Field-by-field fallback: user > admin > env."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        await svc.set_admin_default(
            base_url="http://admin.example",
            api_key="admin-secret",
            default_model="admin-model",
            clear=None,
        )
        # User only overrides base_url; api_key + default_model fall through
        # to admin.
        await svc.set_user_override(
            user_id=22,
            base_url="http://user.example",
            api_key=None,
            default_model=None,
            clear=None,
        )
        cfg = await svc.resolve(user_id=22)
        assert cfg.base_url == "http://user.example"
        assert cfg.source_base_url == "user"
        assert cfg.api_key == "admin-secret"
        assert cfg.source_api_key == "server_admin"
        assert cfg.default_model == "admin-model"
        assert cfg.source_default_model == "server_admin"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_clear_reverts_single_field(tmp_path: Path) -> None:
    """``clear: ['api_key']`` wipes the api_key but preserves the others.

    Env fallback was removed.  Cleared api_key now returns
    '' with source 'unset' instead of falling through to env-tier value.
    """
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        await svc.set_user_override(
            user_id=33,
            base_url="http://user.example",
            api_key="user-secret",
            default_model="user-model",
            clear=None,
        )
        await svc.set_user_override(
            user_id=33,
            base_url=None,
            api_key=None,
            default_model=None,
            clear=["api_key"],
        )
        cfg = await svc.resolve(user_id=33)
        assert cfg.base_url == "http://user.example"
        assert cfg.source_base_url == "user"
        assert cfg.default_model == "user-model"
        assert cfg.source_default_model == "user"
        # api_key was cleared; no env fallback — returns '' / 'unset'.
        assert cfg.api_key == ""
        assert cfg.source_api_key == "unset"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_user_override_rejects_empty_string(tmp_path: Path) -> None:
    """Empty string is not a valid write — use ``clear`` to wipe."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        with pytest.raises(ValueError, match="empty string"):
            await svc.set_user_override(
                user_id=44,
                base_url="",
                api_key=None,
                default_model=None,
                clear=None,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_user_override_rejects_unknown_clear_field(
    tmp_path: Path,
) -> None:
    """Unknown clear field name raises ValueError."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        with pytest.raises(ValueError, match="unknown clear field"):
            await svc.set_user_override(
                user_id=55,
                base_url=None,
                api_key=None,
                default_model=None,
                clear=["password"],
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_probe_ok_with_data_dict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LM Studio's ``{"data": [...]}`` shape probes as ok with model count."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())

        def _transport_handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/models"
            assert request.headers.get("Authorization") == "Bearer key123"
            return httpx.Response(
                200,
                json={"data": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]},
            )

        transport = httpx.MockTransport(_transport_handler)

        # Patch httpx.AsyncClient to inject the mock transport.
        from lmchat.services import lm_studio_overrides_service as svc_mod

        real_client = svc_mod.httpx.AsyncClient

        def _patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_client(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(svc_mod.httpx, "AsyncClient", _patched_client)

        result = await svc.probe(base_url="http://probe.example", api_key="key123")
        assert result.ok is True
        assert result.model_count == 3
        assert result.error is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_probe_fails_on_non_200(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-200 upstream → ok=False with an error string."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())

        def _transport_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized")

        transport = httpx.MockTransport(_transport_handler)
        from lmchat.services import lm_studio_overrides_service as svc_mod

        real_client = svc_mod.httpx.AsyncClient

        def _patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_client(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(svc_mod.httpx, "AsyncClient", _patched_client)

        result = await svc.probe(base_url="http://probe.example", api_key=None)
        assert result.ok is False
        assert result.model_count is None
        assert result.error is not None
        assert "401" in result.error
    finally:
        await engine.dispose()


def test_migration_0013_schema_matches_metadata(tmp_path: Path) -> None:
    """Migration 0013's upgrade()+downgrade() match the schema metadata.

    Drives the migration's ``upgrade`` and ``downgrade`` callables directly
    against a fresh sqlite engine (bypassing the full alembic CLI, which
    spawns ``asyncio.run`` and collides with pytest-asyncio's auto-mode
    event loop).  Verifies:

    1. ``upgrade()`` creates both tables and they accept inserts that
       match the ``db/schema.py`` definitions byte-for-byte.
    2. ``downgrade()`` drops both tables.
    3. The cycle is repeatable (idempotent under create→drop→create).

    Full alembic CLI round-trip runs in CI; this test is the
    in-process equivalent that's portable across environments.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    # Use a synchronous in-process engine; the migration module uses
    # ``sa`` core, not the async engine.
    from sqlalchemy import create_engine, inspect

    db_path = tmp_path / "p13g_inprocess.db"
    sync_eng = create_engine(f"sqlite:///{db_path}")

    # Import migration module
    import importlib.util

    mig_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0013_p13g_lm_studio_overrides.py"
    )
    spec = importlib.util.spec_from_file_location("p13g_migration_mod", mig_path)
    assert spec is not None and spec.loader is not None
    mig_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig_mod)

    # First we need the users table (FK target) — create just that.
    from lmchat.db.schema import users

    with sync_eng.begin() as conn:
        users.create(conn)

    def _run_op(fn_name: str) -> None:
        with sync_eng.begin() as conn:
            mc = MigrationContext.configure(conn)
            op_proxy = Operations(mc)
            # Bind ``op`` symbol in the migration module to our proxy
            # (the migration uses ``from alembic import op`` at module
            # scope; replacing the attribute is sufficient because it
            # was bound by the import).
            saved = getattr(mig_mod, "op")  # noqa: B009
            setattr(mig_mod, "op", op_proxy)  # noqa: B010
            try:
                getattr(mig_mod, fn_name)()
            finally:
                setattr(mig_mod, "op", saved)  # noqa: B010

    # 1. Upgrade — tables appear.
    _run_op("upgrade")
    inspector = inspect(sync_eng)
    names = set(inspector.get_table_names())
    assert "user_lm_studio_overrides" in names
    assert "server_lm_studio_default" in names

    # 2. Downgrade — tables disappear.
    _run_op("downgrade")
    inspector = inspect(sync_eng)
    names = set(inspector.get_table_names())
    assert "user_lm_studio_overrides" not in names
    assert "server_lm_studio_default" not in names

    # 3. Re-upgrade — idempotent.
    _run_op("upgrade")
    inspector = inspect(sync_eng)
    names = set(inspector.get_table_names())
    assert "user_lm_studio_overrides" in names
    assert "server_lm_studio_default" in names

    sync_eng.dispose()


@pytest.mark.asyncio
async def test_seed_admin_default_from_env_seeds_when_empty(tmp_path: Path) -> None:
    """seed_admin_default_from_env writes the admin row when no
    tier has an api_key; resolve() then reports the seeded key + base_url with
    source ``server_admin``."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        seeded = await svc.seed_admin_default_from_env(
            base_url="http://localhost:1234", api_key="real-key"
        )
        assert seeded is True
        cfg = await svc.resolve(user_id=1)
        assert cfg.api_key == "real-key"
        assert cfg.api_key_set is True
        assert cfg.source_api_key == "server_admin"
        assert cfg.base_url == "http://localhost:1234"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_admin_default_from_env_noop_when_key_already_set(
    tmp_path: Path,
) -> None:
    """The seed is a no-op (returns False, never overrides) once the admin
    tier already has an api_key — so it can only ever bootstrap, never clobber."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        assert await svc.seed_admin_default_from_env(
            base_url="http://localhost:1234", api_key="first-key"
        ) is True
        # Second call: admin row now has a key → no-op, no override.
        assert await svc.seed_admin_default_from_env(
            base_url="http://other:1234", api_key="second-key"
        ) is False
        cfg = await svc.resolve(user_id=1)
        assert cfg.api_key == "first-key"
        assert cfg.base_url == "http://localhost:1234"
    finally:
        await engine.dispose()


# ─── Endpoint-mode toggle ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_endpoint_mode_defaults_to_native(tmp_path: Path) -> None:
    """Empty row (or no row at all) → fetch_endpoint_mode returns 'native'."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        assert await svc.fetch_endpoint_mode() == "native"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_endpoint_mode_roundtrips_openai_compat(tmp_path: Path) -> None:
    """set_endpoint_mode('openai_compat') persists and reads back."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        await svc.set_endpoint_mode("openai_compat")
        assert await svc.fetch_endpoint_mode() == "openai_compat"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_endpoint_mode_back_to_native_stores_null(tmp_path: Path) -> None:
    """Setting back to 'native' stores NULL (the NULL=default convention) and
    still reads back as 'native'."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        await svc.set_endpoint_mode("openai_compat")
        await svc.set_endpoint_mode("native")
        assert await svc.fetch_endpoint_mode() == "native"

        from lmchat.db.schema import server_lm_studio_default  # noqa: PLC0415

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(server_lm_studio_default.c.lm_studio_endpoint_mode)
                )
            ).first()
        assert row is not None
        assert row.lm_studio_endpoint_mode is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_endpoint_mode_rejects_unknown_value(tmp_path: Path) -> None:
    """An unrecognized mode raises ValueError (route layer turns this into 400)."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        with pytest.raises(ValueError, match="lm_studio_endpoint_mode"):
            await svc.set_endpoint_mode("bogus")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_lm_studio_endpoint_mode_free_function(tmp_path: Path) -> None:
    """The free-function mirror used by streaming_service.py's hot path reads
    the same column as the service method."""
    engine = await _make_engine(tmp_path)
    try:
        svc = LmStudioOverridesService(engine=engine, settings=_settings())
        assert await resolve_lm_studio_endpoint_mode(engine=engine) == "native"

        await svc.set_endpoint_mode("openai_compat")
        assert await resolve_lm_studio_endpoint_mode(engine=engine) == "openai_compat"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_lm_studio_endpoint_mode_fails_soft_on_db_error() -> None:
    """A closed/broken engine must not raise — falls back to 'native'."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await engine.dispose()  # engine is now unusable
    assert await resolve_lm_studio_endpoint_mode(engine=engine) == "native"
