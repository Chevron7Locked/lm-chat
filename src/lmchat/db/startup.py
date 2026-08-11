# SPDX-License-Identifier: Apache-2.0
"""Schema-readiness check and Alembic stamp logic.

This module is intentionally separate from ``app.py`` so it can be
tested in isolation.  The FastAPI lifespan hook calls
``ensure_schema_ready(engine)`` after the engine is created and before
routes are mounted.

Algorithm
---------
1. Compute ``live_fingerprint = schema_fingerprint(metadata)``.
2. Try ``SELECT version_num FROM alembic_version``.
3. **Fresh DB** (``alembic_version`` table missing):
   - Run ``alembic upgrade head`` (via programmatic API, wrapped in
     ``asyncio.to_thread`` because Alembic's command API is sync).
   - After upgrade, recompute the fingerprint over the migrated DB.
   - If the post-migration fingerprint equals ``SCHEMA_FINGERPRINT``
     from ``0001_baseline.py``, all is well.  Otherwise raise — the
     migration is wrong.
4. **Existing DB** (``alembic_version`` table present):
   - Compare ``live_fingerprint`` against ``SCHEMA_FINGERPRINT``.
   - If equal: nothing to do.
   - If different: run ``alembic upgrade head``; recompute.  If still
     different, raise ``RuntimeError`` — the schema has drifted out-of-
     band (manual ALTER outside Alembic).

The ``SCHEMA_FINGERPRINT`` constant is imported from
``migrations.versions.0001_baseline`` to keep it a single source of
truth.
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import alembic.command
import alembic.config
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.fingerprint import schema_fingerprint
from lmchat.db.schema import metadata
from lmchat.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of SCHEMA_FINGERPRINT from the baseline revision.
# The import is deferred so that startup.py can be imported without
# the migrations/ package being on sys.path — tests that override the
# path will still work.
# ---------------------------------------------------------------------------


def _baseline_fingerprint() -> str:
    """Return the ``SCHEMA_FINGERPRINT`` constant from the baseline revision.

    Lazily imported so that this module can be tested without alembic
    installed or the migrations directory on sys.path.

    Returns:
        The hex fingerprint string baked into ``0001_baseline.py``.
    """
    mod = importlib.import_module("migrations.versions.0001_baseline")
    return str(mod.SCHEMA_FINGERPRINT)


# ---------------------------------------------------------------------------
# Alembic config builder
# ---------------------------------------------------------------------------


def _build_alembic_cfg() -> alembic.config.Config:
    """Return an ``alembic.config.Config`` pointing at ``alembic.ini``.

    Overrides ``sqlalchemy.url`` and ``script_location`` (absolute path)
    so that the programmatic upgrade call works regardless of the process
    working directory.

    Returns:
        A configured :class:`alembic.config.Config` instance.
    """
    from lmchat.config import get_settings

    # Locate alembic.ini relative to this file: src/lmchat/db/ → repo root
    repo_root = Path(__file__).resolve().parents[3]
    ini_path = repo_root / "alembic.ini"

    cfg = alembic.config.Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    # Use absolute path so Alembic finds migrations/ regardless of CWD.
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    return cfg


# ---------------------------------------------------------------------------
# Sync upgrade helper (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_upgrade_head() -> None:
    """Run ``alembic upgrade head`` synchronously.

    Wrapped with ``asyncio.to_thread`` in async callers because
    ``alembic.command`` is synchronous.
    """
    cfg = _build_alembic_cfg()
    alembic.command.upgrade(cfg, "head")


def _get_alembic_head() -> str:
    """Return the current head revision ID from the migration chain.

    Uses ``alembic.script.ScriptDirectory`` to walk the migration chain
    and find the head without connecting to a database.

    Returns:
        The head revision ID string (e.g. ``"0019"``).
    """
    from alembic.script import ScriptDirectory

    cfg = _build_alembic_cfg()
    scripts = ScriptDirectory.from_config(cfg)
    heads = scripts.get_heads()
    if not heads:
        raise RuntimeError("No migration head found — is the migrations directory empty?")
    if len(heads) > 1:
        raise RuntimeError(
            f"Multiple migration heads detected: {heads!r} — resolve before deploying."
        )
    return heads[0]


async def _get_live_version(engine: AsyncEngine) -> str | None:
    """Return the current alembic_version from the live database, or None."""
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            row = result.fetchone()
            return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ensure_schema_ready(engine: AsyncEngine) -> None:
    """Verify the database schema is up-to-date; run migrations if needed.

    Called from the FastAPI lifespan hook, after the engine is created
    and before routes are mounted.  Raises ``RuntimeError`` on
    unrecoverable schema drift.

    Args:
        engine: The live ``AsyncEngine`` connected to the application
            database.

    Raises:
        RuntimeError: If the post-migration schema fingerprint does not
            match the baseline constant, indicating out-of-band DDL
            changes (manual ALTER outside Alembic).
    """
    live_fingerprint = schema_fingerprint(metadata)
    baseline = _baseline_fingerprint()

    log.debug(
        "schema_ready_check",
        live_fingerprint=live_fingerprint,
        baseline_fingerprint=baseline,
    )

    # -------------------------------------------------------------------
    # Probe: does the alembic_version table exist?
    # -------------------------------------------------------------------
    has_alembic_version = False
    current_version: str | None = None

    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            row = result.fetchone()
            has_alembic_version = True
            current_version = row[0] if row else None
    except Exception:  # noqa: BLE001
        # Table doesn't exist — fresh DB.
        has_alembic_version = False

    if not has_alembic_version:
        # -------------------------------------------------------------------
        # Branch A — fresh DB: run migrations then verify.
        # -------------------------------------------------------------------
        log.info("alembic_version missing — running upgrade head on fresh DB")
        await asyncio.to_thread(_run_upgrade_head)

        # Re-fingerprint using the *model* metadata — the migration must
        # have produced a DB matching what schema.py describes.
        post_fingerprint = schema_fingerprint(metadata)
        if post_fingerprint != baseline:
            raise RuntimeError(
                f"Post-upgrade schema fingerprint {post_fingerprint!r} does not "
                f"match baseline {baseline!r} — the migration may be out of sync "
                "with db/schema.py"
            )
        log.info(
            "schema_ready",
            path="fresh_db_upgraded",
            fingerprint=post_fingerprint,
        )
        return

    # -----------------------------------------------------------------------
    # Branch B — existing DB.
    # -----------------------------------------------------------------------
    # Compute the head revision so we can detect a version-behind DB even when
    # the fingerprint matches (model-derived fingerprint is always the latest
    # schema.py value and cannot detect a DB that is missing recent migrations).
    alembic_head = _get_alembic_head()

    if live_fingerprint == baseline:
        if current_version == alembic_head:
            log.info(
                "schema_ready",
                path="existing_db_consistent",
                version=current_version,
                fingerprint=live_fingerprint,
            )
            return
        # Fingerprint consistent but version behind head — run migrations.
        # This covers the case where the schema fingerprint was updated in the
        # same commit as the migration (so the model and baseline agree), but
        # an existing DB was stamped at an earlier revision.
        log.warning(
            "schema_version_behind_head — running upgrade head",
            current_version=current_version,
            alembic_head=alembic_head,
            fingerprint=live_fingerprint,
        )
        await asyncio.to_thread(_run_upgrade_head)
        post_version = await _get_live_version(engine)
        if post_version != alembic_head:
            raise RuntimeError(
                f"Alembic version {post_version!r} still behind head {alembic_head!r} "
                "after upgrade — migration chain may be broken."
            )
        log.info(
            "schema_ready",
            path="existing_db_version_upgraded",
            version=post_version,
            fingerprint=live_fingerprint,
        )
        return

    # Fingerprint mismatch — attempt upgrade.
    log.warning(
        "schema_drift detected — attempting alembic upgrade head",
        live_fingerprint=live_fingerprint,
        baseline_fingerprint=baseline,
        current_version=current_version,
    )
    await asyncio.to_thread(_run_upgrade_head)

    post_fingerprint = schema_fingerprint(metadata)
    if post_fingerprint != baseline:
        raise RuntimeError(
            f"Schema drift remains after alembic upgrade head: "
            f"live={post_fingerprint!r}, baseline={baseline!r}. "
            "The schema has been altered outside Alembic — manual repair required."
        )

    log.info(
        "schema_ready",
        path="existing_db_upgraded",
        fingerprint=post_fingerprint,
    )
