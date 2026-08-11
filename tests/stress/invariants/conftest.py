# SPDX-License-Identifier: Apache-2.0
"""Pytest fixtures for the post-run stress invariants.

Each invariant module opens the DB that the load just populated and
asserts a single class of correctness.  The shared engine fixture is
async and points at ``DATABASE_URL`` (set by the orchestrator).
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@pytest.fixture(scope="session")
def stress_database_url() -> str:
    """Return the DB URL the orchestrator populated.

    Requires STRESS_DATABASE_URL to be set (the stress orchestrator exports
    this before invoking pytest).  Refusing to fall back to ./lmchat.db
    prevents invariant tests from silently scanning the admin's real dev
    database and producing nondeterministic results.
    """
    url = os.environ.get("STRESS_DATABASE_URL")
    if not url:
        pytest.skip(
            "STRESS_DATABASE_URL not set — run via the stress orchestrator; "
            "refusing to fall back to lmchat.db"
        )
    return url


@pytest_asyncio.fixture(scope="function")
async def stress_engine(stress_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Yield an async SQLAlchemy engine pointing at the populated DB."""
    engine = create_async_engine(stress_database_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()
