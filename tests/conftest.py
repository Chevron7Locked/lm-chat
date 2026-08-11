# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures for lm-chat tests.

Per `docs/IMPLEMENTATION_METHODOLOGY.md` §1: tests use real
infrastructure (real DB, real LM Studio probes); structlog
and stdlib-logging state must be reset between tests because
they share global state.

sys.path note: the ``migrations/`` package lives at the repo root and must be
importable by ``ensure_schema_ready`` (called by the lifespan in tests that
run the full TestClient lifecycle).  We add the repo root here so all test
modules — not just those under ``tests/routes/`` — can load migrations.
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from structlog.contextvars import clear_contextvars

# Repo root → migrations/ package is importable from any test file.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """Reset structlog + stdlib root logger between tests.

    structlog.configure() is global state. Without this fixture,
    a test that calls configure_logging() leaks handlers and
    contextvars into the next test, producing non-deterministic
    output.
    """
    yield
    structlog.reset_defaults()
    clear_contextvars()
    root = logging.getLogger()
    for handler in list(root.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            root.removeHandler(handler)
    root.setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def _require_lm_chat_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure LM_CHAT_SECRET is set for every test.

    ``LM_CHAT_SECRET`` became a required (non-empty) setting at P1.
    All tests that touch the settings singleton or the application must
    have this env var present.  Tests that need a *different* value can
    override by calling ``monkeypatch.setenv`` in their own fixture after
    this autouse fixture runs.

    The settings lru_cache is cleared before and after each test so that
    monkeypatched values are picked up by a fresh ``get_settings()`` call.
    """
    import os

    from lmchat.config import get_settings

    # Only set if not already set in the environment (allows .env.local
    # to supply a real secret for integration tests against live infra).

    if not os.environ.get("LM_CHAT_SECRET"):
        monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-for-testing!!")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Mock LM Studio server fixtures — §1A
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mock_lmstudio_server() -> Iterator[str]:
    """Session-scoped mock LM Studio server on a dynamic port.

    Starts a Starlette ASGI app via uvicorn on a dynamically-allocated port
    (127.0.0.1:0).  Yields the base URL (e.g. ``http://127.0.0.1:54321``)
    which tests use as ``LM_STUDIO_BASE_URL``.

    The server loads ``happy_text`` script by default.  Switch scripts via
    ``mock_lmstudio_script()``.
    """
    import asyncio
    import time

    import uvicorn

    from tests.fixtures.lmstudio_mock.server import _find_free_port, _state, create_app

    app = create_app()

    # Retry the bind up to 3 times to handle the race where another
    # process claims our ephemeral port between _find_free_port() and
    # uvicorn binding.
    server: uvicorn.Server | None = None
    base_url: str | None = None
    for _attempt in range(3):
        port = _find_free_port()
        candidate_url = f"http://127.0.0.1:{port}"

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="off",
        )
        candidate_server = uvicorn.Server(config)

        async def _run(srv: uvicorn.Server = candidate_server) -> None:
            try:
                await srv.serve()
            except OSError:
                pass  # port race — will retry in outer loop

        loop = asyncio.new_event_loop()

        def _start(loop: asyncio.AbstractEventLoop = loop) -> None:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_run())

        import threading

        t = threading.Thread(target=_start, daemon=True)
        t.start()

        # Give uvicorn a moment to bind.
        time.sleep(0.1)

        # Check if the server is actually running.
        import httpx

        try:
            r = httpx.get(f"{candidate_url}/healthz", timeout=0.5)
            if r.status_code == 200:
                server = candidate_server
                base_url = candidate_url
                break
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            # Bind failed (race) — clean up and retry.
            candidate_server.should_exit = True
            t.join(timeout=1)
            continue
    else:
        raise RuntimeError(
            "mock_lmstudio_server: failed to bind after 3 attempts"
        )

    # Wait for the server to be ready by polling /healthz (up to 100 retries).
    import httpx

    for _retry in range(100):
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=0.5)
            if r.status_code == 200:
                break
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            time.sleep(0.05)
    else:
        raise RuntimeError(
            f"mock_lmstudio_server: server at {base_url} not healthy "
            f"after 100 healthcheck retries"
        )

    _state.load_script("happy_text")
    yield base_url

    # Teardown: stop the server.
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def mock_lmstudio_script() -> Iterator[dict[str, object]]:
    """Function-scoped fixture that returns a switch-script helper.

    Usage::

        def test_foo(mock_lmstudio_script):
            mock_lmstudio_script("reasoning_then_text")
            # ... make request to mock server
            mock_lmstudio_script("happy_text")  # restore default

    The returned callable sets the script on the shared ``_state`` singleton.
    """
    from tests.fixtures.lmstudio_mock.server import _state

    def _switch(name: str) -> None:
        _state.load_script(name)

    yield {"switch": _switch}  # type: ignore[misc]

    # Restore default after each test.
    _state.load_script("happy_text")
