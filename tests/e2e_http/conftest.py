# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for e2e_http tests — §2I stream_in_progress 409 race, etc.

Design
------
- Session-scoped live server (the main FastAPI app) with a mock LM Studio
  backend from ``tests/fixtures/lmstudio_mock/server.py``.
- ``httpx.AsyncClient`` fixture for real HTTP dispatch against the live server.
- ``db_engine`` fixture for direct DB assertions.

The ``mock_lmstudio_server`` session-scoped fixture is defined in
``tests/conftest.py`` and reused here.
"""
from __future__ import annotations

import functools
import os
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import tests.integration.conftest as _iconf
from tests.integration.conftest import (  # noqa: F811
    login_user,
    make_admin,
    register_admin_and_login,
    register_and_login,
    register_user,
)

# ---------------------------------------------------------------------------
# sys.path: ensure repo root is importable
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_SCRYPT_N: int = 2**10


# ---------------------------------------------------------------------------
# Per-test scrypt cost override
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_scrypt_cost(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Replace hash_password with a N=2^10 version per test.

    Without this, the OpenSSL maxmem limit kicks in during auth operations.
    Restored automatically by monkeypatch after each test.
    """
    import lmchat.services.auth_service as auth_svc
    from lmchat.services.auth_service import _reset_dummy_hash_cache
    from lmchat.utils.hashing import hash_password as _real_hash

    @functools.wraps(_real_hash)
    def _low_cost_hash(password: str, **kwargs: Any) -> str:
        kwargs["n"] = _TEST_SCRYPT_N
        return _real_hash(password, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_svc, "hash_password", _low_cost_hash)
    _reset_dummy_hash_cache()
    yield
    _reset_dummy_hash_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return an OS-allocated free TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(asgi_app: Any, port: int) -> uvicorn.Server:
    """Start *asgi_app* on *port* in a daemon thread; return the server."""
    cfg = uvicorn.Config(
        asgi_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        loop="asyncio",
    )
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"uvicorn did not start on port {port} within 10 s")
        time.sleep(0.05)
    return server


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_server(
    mock_lmstudio_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[dict[str, Any]]:
    """Session-scoped live server fixture.

    Starts the main FastAPI app with:
    - A mock LM Studio backend at the URL from ``mock_lmstudio_server``.
    - A temporary SQLite database.
    - Required env vars for the lifespan.

    Yields a dict with ``base_url``, ``db_path``, and ``stub_url``.
    """
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    tmp = tmp_path_factory.mktemp("e2e_http_db")
    db_path = tmp / "e2e_http.db"

    # Expose the DB path to the shared ``register_user`` helper from
    # ``tests.integration.conftest`` so it uses direct DB insert to bypass
    # the single-admin registration gate (instead of calling POST /register).
    _iconf._live_db_path = db_path

    app_port = _free_port()

    saved: dict[str, str | None] = {}
    env_overrides = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "LM_CHAT_SECRET": "e2e-http-test-secret-32bytes!!!",
        "LM_STUDIO_BASE_URL": mock_lmstudio_server,
        "LM_STUDIO_API_KEY": "",
        "LM_CHAT_SINGLE_SESSION": "false",
        "LM_CHAT_LOCAL_MCP_DISCOVERY_ENABLED": "false",
    }
    for k, v in env_overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    from lmchat.app import create_app

    main_app = create_app()
    main_server = _start_server(main_app, app_port)

    # Seed a bootstrap placeholder so the first test registration doesn't
    # accidentally become admin (auto-admin for the very first user).
    # Also seed the admin default LM Studio config row.
    with sqlite3.connect(str(db_path)) as seed_conn:
        seed_conn.execute(
            "INSERT INTO users (username, password_hash, is_admin)"
            " VALUES ('__bootstrap_placeholder__',"
            " 'scrypt$0$0$0$0$0', 0)"
        )
        seed_conn.execute(
            "INSERT INTO server_lm_studio_default (id, base_url, api_key_enc, default_model)"
            f" VALUES (1, '{mock_lmstudio_server}', NULL, NULL)"
        )
        seed_conn.commit()

    # Patch in-memory singletons to point at the mock LM Studio backend.
    from lmchat.services.lm_studio_overrides_service import EMBEDDINGS_PATH_CONST

    _state = main_app.state
    _state.models_service._base_url = mock_lmstudio_server
    _state.lmstudio_adapter._base_url = mock_lmstudio_server
    _state.embedding_client._base_url = mock_lmstudio_server
    _state.embedding_client._endpoint = f"{mock_lmstudio_server}{EMBEDDINGS_PATH_CONST}"
    _state.models_service._cache = None

    yield {
        "base_url": f"http://127.0.0.1:{app_port}",
        "db_path": str(db_path),
        "stub_url": mock_lmstudio_server,
    }

    main_server.should_exit = True
    time.sleep(1.5)

    # Reset the shared DB-path pointer so tests from other modules that run
    # after this session don't accidentally inherit the e2e_http DB path.
    _iconf._live_db_path = None

    engine_mod.dispose_engine()

    import logging  # noqa: I001
    import structlog  # noqa: I001
    from structlog.contextvars import clear_contextvars  # noqa: I001

    structlog.reset_defaults()
    clear_contextvars()
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            root_logger.removeHandler(handler)
    root_logger.setLevel(logging.WARNING)

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    live_server: dict[str, Any],
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client pointed at the live server."""
    async with httpx.AsyncClient(
        base_url=live_server["base_url"],
        follow_redirects=True,
        timeout=30.0,
    ) as c:
        yield c


@pytest_asyncio.fixture
async def db_engine(
    live_server: dict[str, Any],
) -> AsyncGenerator[AsyncEngine, None]:
    """Async SQLAlchemy engine for direct DB assertions."""
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{live_server['db_path']}", pool_pre_ping=True
    )
    from lmchat.db.schema import metadata

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


__all__ = [
    "live_server",
    "client",
    "db_engine",
    "register_and_login",
    "register_user",
    "login_user",
    "make_admin",
    "register_admin_and_login",
]