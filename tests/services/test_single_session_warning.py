# SPDX-License-Identifier: Apache-2.0
"""Tests for single-session enforcement warning when replicas are detected.

Tests for the single-session concurrency warning.

When ``LM_CHAT_REPLICAS_DETECTED=true`` and the session store reports
``supports_distributed_revoke == False``, a structured-log WARNING with
event="single_session_warning" is emitted during login.

SQLiteSessionStore always returns ``supports_distributed_revoke = False``,
so it triggers the warning when the env var is set.

Capture strategy
-----------------
structlog's output destination depends on test ordering.  When
``configure_logging()`` has been called by a prior test, structlog routes
through stdlib logging (intercepted by pytest's ``caplog`` fixture).  When
``reset_logging`` strips all handlers, structlog falls back to its built-in
ConsoleRenderer which writes to stdout (intercepted by ``capsys``).

To make the test order-independent, the ``_configure_logging_for_test``
autouse fixture calls ``configure_logging(console=False, log_file=None)``
to force structlog through stdlib with no file/console handlers.  This
guarantees the event is always captured by ``caplog`` regardless of prior
test state.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.services.auth_service import login
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

#: Low-cost scrypt parameters — keeps tests within OpenSSL maxmem.
LOW_COST: dict[str, int] = {"_hash_n": 2**10, "_hash_r": 8, "_hash_p": 1}


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure LM_CHAT_SECRET is set and structlog routes through stdlib for the test.

    Calls ``configure_logging(console=False, log_file=None)`` to wire structlog
    through the stdlib root logger with no handlers.  This makes the test order-
    independent: pytest's ``caplog`` fixture intercepts stdlib log records
    regardless of whether a prior test left structlog in its default (stdout)
    or configured (handlers) state.
    """
    monkeypatch.setenv("LM_CHAT_SECRET", "warning-test-secret")
    monkeypatch.setenv("LM_CHAT_SINGLE_SESSION", "true")

    from lmchat.config import get_settings
    from lmchat.services.auth_service import (
        _reset_dummy_hash_cache,
        _reset_single_session_locks,
    )

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    _reset_single_session_locks()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    _reset_single_session_locks()


@pytest.fixture()
async def eng(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a test engine with a real user."""
    db_path = tmp_path / "warn.db"
    e = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with e.begin() as conn:
        await conn.run_sync(metadata.create_all)

    from sqlalchemy import func, select, text

    from lmchat.db.schema import users as users_tbl

    pw = hash_password("pw", n=2**10, r=8, p=1)
    async with e.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users_tbl.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        await conn.execute(
            text("INSERT INTO users (id, username, password_hash) VALUES (:id, :u, :ph)"),
            {"id": next_id, "u": "warnuser", "ph": pw},
        )
    yield e
    await e.dispose()


async def test_single_session_warning_emitted_with_replicas_detected(
    eng: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With LM_CHAT_REPLICAS_DETECTED=true the warning is logged.

    Spies on ``auth_service.log.warning`` directly rather than going through
    caplog/structlog plumbing. This is order-independent because the spy
    intercepts at the BoundLogger level, bypassing the entire stdlib-routing
    chain that can be poisoned by other tests' configure_logging calls.
    """
    from lmchat.services import auth_service

    monkeypatch.setenv("LM_CHAT_REPLICAS_DETECTED", "true")

    captured_events: list[str] = []
    original_warning = auth_service.log.warning

    def _spy_warning(event: str, **kwargs: object) -> None:
        captured_events.append(event)
        original_warning(event, **kwargs)

    monkeypatch.setattr(auth_service.log, "warning", _spy_warning)

    store = SQLiteSessionStore(engine=eng)
    await login(
        username="warnuser",
        password="pw",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=eng,
        **LOW_COST,
    )

    assert "single_session_warning" in captured_events, (
        f"Expected 'single_session_warning' in captured events but got:\n{captured_events}"
    )


async def test_single_session_warning_not_emitted_without_env(
    eng: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without LM_CHAT_REPLICAS_DETECTED=true no warning is emitted.

    Spies on ``auth_service.log.warning`` directly (same pattern as the
    above test — order-independent).
    """
    from lmchat.services import auth_service

    monkeypatch.delenv("LM_CHAT_REPLICAS_DETECTED", raising=False)

    captured_events: list[str] = []
    original_warning = auth_service.log.warning

    def _spy_warning(event: str, **kwargs: object) -> None:
        captured_events.append(event)
        original_warning(event, **kwargs)

    monkeypatch.setattr(auth_service.log, "warning", _spy_warning)

    store = SQLiteSessionStore(engine=eng)
    await login(
        username="warnuser",
        password="pw",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=eng,
        **LOW_COST,
    )

    assert "single_session_warning" not in captured_events
