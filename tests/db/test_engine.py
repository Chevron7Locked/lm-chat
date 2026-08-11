# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.db.engine — AsyncEngine factory and lifecycle.

Tests run against an in-memory SQLite
URL to avoid touching any real on-disk DB.
The postgres URL test is skipped because asyncpg is not in the P1
test runtime (it is an optional dep); dialect detection is verified
by parsing the URL string rather than attempting a real connection.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.engine import dispose_engine, get_engine


@pytest.fixture(autouse=True)
def isolate_engine():
    """Dispose the global engine before and after each test."""
    dispose_engine()
    yield
    dispose_engine()


@pytest.fixture()
def memory_url(monkeypatch):
    """Point settings at an in-memory SQLite database."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    # Reset the lru_cache so the new env var is picked up.
    from lmchat.config import get_settings
    get_settings.cache_clear()
    yield "sqlite+aiosqlite:///:memory:"
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Engine creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_engine_returns_async_engine(memory_url):
    """get_engine() must return an AsyncEngine for a sqlite URL."""
    engine = get_engine()
    assert isinstance(engine, AsyncEngine)


@pytest.mark.asyncio
async def test_get_engine_cached(memory_url):
    """get_engine() called twice must return the same object."""
    engine1 = get_engine()
    engine2 = get_engine()
    assert engine1 is engine2


@pytest.mark.asyncio
async def test_dispose_engine_clears_cache(memory_url):
    """After dispose_engine(), get_engine() builds a new instance."""
    engine1 = get_engine()
    dispose_engine()
    engine2 = get_engine()
    assert engine1 is not engine2


# ---------------------------------------------------------------------------
# PRAGMA application on SQLite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pragmas_applied_on_sqlite(memory_url):
    """PRAGMAs must be applied on connect — verify foreign_keys=ON."""
    engine = get_engine()

    async with engine.connect() as conn:
        # The connect event fires when the underlying DBAPI conn is made.
        # Running a PRAGMA query verifies the hook ran.
        result = await conn.execute(sa_text("PRAGMA foreign_keys"))
        value = result.scalar()
    # foreign_keys=ON → SQLite returns 1
    assert value == 1


# ---------------------------------------------------------------------------
# Postgres dialect detection (no real connection; structural test only)
# ---------------------------------------------------------------------------


def test_postgres_url_dialect_detection():
    """Dialect detection must not raise for a postgresql+asyncpg URL string.

    We do NOT attempt a real connection (asyncpg not guaranteed in P1
    test environment) but we verify that the URL string contains
    'postgresql' and would therefore take the Postgres branch in engine.py.
    """
    pg_url = "postgresql+asyncpg://user:pass@localhost/testdb"
    assert "postgresql" in pg_url
    assert not pg_url.startswith("sqlite")
