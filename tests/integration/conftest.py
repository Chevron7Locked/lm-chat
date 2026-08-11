# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for P10a live-backend integration tests.

Design goals
------------
- Live uvicorn: the app runs under a real uvicorn server in a background
  thread, not the ASGI TestClient.  This exercises the full middleware
  stack (rate limiter, security headers, session cookies).
- Single session per module: the server is started once and shared across
  all tests in the module.  Test isolation is achieved via unique usernames
  (``secrets.token_hex(8)`` suffix).
- Stub LM Studio: an in-process FastAPI ASGI app served on a second
  uvicorn instance.  Responds to the paths the main app calls.  Includes
  an ``X-Stub-Fault`` injection header for error-path tests.
- Low-cost scrypt: ``hash_password`` in ``auth_service`` is replaced with
  a N=2^10 version so tests run within the OpenSSL maxmem limit.

Port allocation
---------------
Both servers bind to ``127.0.0.1:0`` and the OS allocates a free port.
``conftest`` stores the ports on module-level variables so tests can
build the base URL without re-querying.

sys.path
--------
The repo root is added so ``migrations/`` is importable (required by
``ensure_schema_ready`` inside the lifespan).
"""
from __future__ import annotations

import functools
import secrets
import socket
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
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Module-level slot for the live server's DB path.  Set by the ``live_servers``
# session-scoped fixture so that ``register_user`` can bypass the
# single-admin registration gate by writing directly to the DB via sqlite3.
_live_db_path: Path | None = None

# ---------------------------------------------------------------------------
# sys.path: repo root → migrations/ importable
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_SCRYPT_N: int = 2**10
_LOW_COST: dict[str, int] = {"_hash_n": _TEST_SCRYPT_N, "_hash_r": 8, "_hash_p": 1}

# ---------------------------------------------------------------------------
# Per-test scrypt cost override (mirrors tests/routes/conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_scrypt_cost(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Replace hash_password in auth_service with a N=2^10 version per test.

    Mirrors the pattern from tests/routes/conftest.py so the integration
    tests run within the OpenSSL maxmem limit without affecting other test
    modules (monkeypatch is properly restored after each test).
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


@pytest.fixture(autouse=True)
def _deterministic_loaded_probe(
    monkeypatch: pytest.MonkeyPatch, live_servers: dict[str, Any]
) -> None:
    """Make the stub's loaded-models probe deterministic across the suite.

    ``live_servers`` is session-scoped, so ``models_service`` state survives
    between tests.  Two failure modes this guards against:

    1. ``list_loaded()`` only re-probes when ``_cache is None``; once any test
       leaves an empty ``[]`` (e.g. a deliberate probe-error test), that empty
       list sticks for the rest of the session, so the embedding fail-loud
       guard wrongly 503s on document upload.  Clearing ``_cache`` per test
       forces a fresh probe of the always-reachable stub.
    2. The forced-reprobe storm guard (``_FORCED_REPROBE_MIN_INTERVAL``, 5 s)
       and the 401 auth-failed backoff (``_AUTH_FAILED_BACKOFF_SEC``, 60 s)
       would otherwise skip that probe.  The in-process stub is always up, so
       these production protections are unnecessary here; zero them per test
       (monkeypatch restores them afterwards, leaving other modules untouched).
    """
    import lmchat.services.models_service as _ms

    monkeypatch.setattr(_ms, "_FORCED_REPROBE_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(_ms, "_AUTH_FAILED_BACKOFF_SEC", 0.0)
    live_servers["app"].state.models_service._cache = None

    # Clear any admin embedding-model pin a prior test left in the shared
    # session DB.  test_memory_routes pins ``stub-model-q4`` (the *LLM* stub,
    # not a loaded embedding model) via ``preferred_embedding_model_id``; when
    # that leaks ahead of a documents test the embedding fail-loud guard 503s
    # on upload.  Resetting to NULL restores the seeded default (nomic, which
    # the stub advertises as loaded).
    import sqlite3

    with sqlite3.connect(live_servers["db_path"]) as _conn:
        _conn.execute(
            "UPDATE server_lm_studio_default "
            "SET preferred_embedding_model_id = NULL WHERE id = 1"
        )
        _conn.commit()


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
    # Wait until the server is actually accepting connections.
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"uvicorn did not start on port {port} within 10 s")
        time.sleep(0.05)
    return server


# ---------------------------------------------------------------------------
# Stub LM Studio ASGI app
# ---------------------------------------------------------------------------
#
# Serves the minimal surface the main app calls:
#   GET  /api/v1/models      → model list
#   POST /api/v1/chat        → SSE stream (5 deltas + stream.complete)
#   POST /v1/embeddings      → deterministic float vector
#   POST /v1/chat/completions → non-streaming JSON completion (OpenAI compat)
#
# Fault injection: ``X-Stub-Fault: 400`` causes the next POST /api/v1/chat
# to return 400 (simulates LM Studio rejected-param).
# ``X-Stub-Fault: 503`` causes it to return 503.

_stub_lm_app = FastAPI(title="stub-lm-studio")

_STUB_MODEL_ID = "stub-model-q4"
_STUB_TOOL_MODEL_ID = "stub-tool-model-q4"  # tool-trained variant for integration override tests
# Advertise the embedding model under LM Studio's canonical default catalog key
# so memory_service.resolve_active_embedding_model_key resolves it as the loaded
# default (no admin preference set) instead of failing loud. Mirrors real usage:
# LM Studio ships nomic under this key on first launch.
_STUB_EMBEDDING_MODEL_ID = "text-embedding-nomic-embed-text-v1.5"

# Native LM Studio format — models_service._probe_upstream reads the "models"
# key and expects each entry to have a "key" field (not "id").
# The "data" key is kept so any OpenAI-compat consumers still work.
_MODELS_RESPONSE: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": _STUB_MODEL_ID,
            "object": "model",
            "type": "llm",
            "publisher": "stub",
            "arch": "llama",
            "compatibility_type": "gguf",
            "quantization": "Q4_K_M",
            "state": "not-loaded",
            "max_context_length": 4096,
        }
    ],
    # Native /api/v1/models format (models_service reads this key).
    "models": [
        {
            "key": _STUB_MODEL_ID,
            "type": "llm",
            "publisher": "stub",
            "loaded_instances": [
                {"id": _STUB_MODEL_ID, "config": {"context_length": 4096}}
            ],
            "maxContextLength": 4096,
            "capabilities": {
                "vision": False,
                "trained_for_tool_use": False,
            },
        },
        {
            # Tool-trained variant — used by integrations-override tests so
            # (a) the non-tool-model filter in streaming_service does NOT drop
            # integrations, and (b) the context-budget gate has enough headroom
            # (128 K) that no integrations are trimmed for small stub payloads.
            "key": _STUB_TOOL_MODEL_ID,
            "type": "llm",
            "publisher": "stub",
            "loaded_instances": [
                {"id": _STUB_TOOL_MODEL_ID, "config": {"context_length": 131072}}
            ],
            "maxContextLength": 131072,
            "capabilities": {
                "vision": False,
                "trained_for_tool_use": True,
            },
        },
        {
            "key": _STUB_EMBEDDING_MODEL_ID,
            "type": "embedding",
            "publisher": "stub",
            "loaded_instances": [
                {"id": _STUB_EMBEDDING_MODEL_ID, "config": {"context_length": 512}}
            ],
            "maxContextLength": 512,
            "capabilities": {
                "vision": False,
                "trained_for_tool_use": False,
            },
        },
        # Sampler-profile integration tests send Qwen3.6-35B-A3B and
        # some-non-qwen-model.  Added so the model-resolution guard in
        # streaming_service (which returns early with upstream_unavailable
        # when the key is unknown) doesn't bail out before the profiled
        # request reaches the stub.
        {
            "key": "Qwen3.6-35B-A3B",
            "type": "llm",
            "publisher": "stub",
            "loaded_instances": [
                {"id": "Qwen3.6-35B-A3B", "config": {"context_length": 32768}}
            ],
            "maxContextLength": 32768,
            "capabilities": {
                "vision": False,
                "trained_for_tool_use": False,
            },
        },
        {
            "key": "some-non-qwen-model",
            "type": "llm",
            "publisher": "stub",
            "loaded_instances": [
                {"id": "some-non-qwen-model", "config": {"context_length": 4096}}
            ],
            "maxContextLength": 4096,
            "capabilities": {
                "vision": False,
                "trained_for_tool_use": False,
            },
        },
    ],
}


@_stub_lm_app.get("/api/v1/models")
async def stub_models() -> JSONResponse:
    return JSONResponse(_MODELS_RESPONSE)


def _sse_chat_stream() -> Generator[str]:
    """Yield a proper LM Studio native SSE sequence.

    The native SSE decoder (lmchat.lmstudio.native.decode_native) requires:
        event: <name>
        data: <json>

    (bare ``data:``-only lines are silently ignored because the decoder
    requires both ``event:`` and ``data:`` fields before emitting a pair).

    Sequence mirrors a minimal warm-start LM Studio response:
        chat.start → message.start → message.delta ×3 → message.end → chat.end
    """
    yield "event: chat.start\ndata: {\"type\":\"chat.start\",\"response_id\":\"r-stub\"}\n\n"
    yield "event: message.start\ndata: {\"type\":\"message.start\"}\n\n"
    for i in range(3):
        payload = f'{{"type":"message.delta","content":"word{i}"}}'
        yield f"event: message.delta\ndata: {payload}\n\n"
    yield "event: message.end\ndata: {\"type\":\"message.end\"}\n\n"
    yield "event: chat.end\ndata: {\"type\":\"chat.end\",\"response_id\":\"r-stub\"}\n\n"


_STUB_LAST_CHAT_BODY: dict[str, Any] = {}


def get_stub_last_chat_body() -> dict[str, Any]:
    """Return the most recent JSON body posted to the stub /api/v1/chat.

    Used by P13h tests to assert that integrations override flows verbatim
    from CanonicalChatRequest → encode_native → LM Studio request body.
    Returns an empty dict if no chat call has happened yet (or if the body
    failed to parse as JSON).
    """
    return dict(_STUB_LAST_CHAT_BODY)


@_stub_lm_app.post("/api/v1/chat")
async def stub_chat(request: Request) -> Any:  # noqa: ANN401
    fault = request.headers.get("x-stub-fault", "").strip()
    if fault == "400":
        return JSONResponse(
            {"error": "unknown parameter: temperature"},
            status_code=400,
        )
    if fault == "503":
        return JSONResponse({"error": "upstream error"}, status_code=503)

    # P13h: capture the parsed JSON body for the integration test that
    # asserts the integrations override is forwarded to LM Studio.
    try:
        body = await request.json()
        if isinstance(body, dict):
            _STUB_LAST_CHAT_BODY.clear()
            _STUB_LAST_CHAT_BODY.update(body)
    except Exception:  # noqa: BLE001
        pass

    return StreamingResponse(
        _sse_chat_stream(),
        media_type="text/event-stream",
    )


def _make_embeddings_response(request_body: dict[str, Any], default_model: str) -> dict[str, Any]:
    """Build an embeddings response matching the batch size of the request."""
    inputs = request_body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    model_id = request_body.get("model", default_model)
    n = max(1, len(inputs))
    return {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "index": i,
                "embedding": [0.1] * 384,
            }
            for i in range(n)
        ],
        "model": model_id,
        "usage": {"prompt_tokens": 5 * n, "total_tokens": 5 * n},
    }


@_stub_lm_app.post("/api/v1/embeddings")
async def stub_embeddings_native(request: Request) -> JSONResponse:
    """LM Studio native embeddings endpoint used by EmbeddingClient.

    Returns one 384-dim zero vector per input text so batch calls succeed.
    """
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(_make_embeddings_response(body, _STUB_EMBEDDING_MODEL_ID))


@_stub_lm_app.post("/v1/embeddings")
async def stub_embeddings(request: Request) -> JSONResponse:
    """OpenAI-compat embeddings endpoint (kept for backwards compat)."""
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(_make_embeddings_response(body, _STUB_MODEL_ID))


@_stub_lm_app.post("/v1/chat/completions")
async def stub_compat_completions() -> JSONResponse:
    """OpenAI-compat non-streaming completion."""
    return JSONResponse(
        {
            "id": "stub-cmpl-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "stub response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }
    )


# ---------------------------------------------------------------------------
# P11b stub lifecycle endpoints
# ---------------------------------------------------------------------------


@_stub_lm_app.post("/api/v1/models/load")
async def stub_load_model(request: Request) -> JSONResponse:
    """Stub for POST /api/v1/models/load.

    Returns a canned load-complete response.
    Fault injection: ``X-Stub-Fault: 404`` → model not found,
                     ``X-Stub-Fault: 503`` → gateway error.
    """
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    fault = request.headers.get("x-stub-fault", "").strip()
    if fault == "404":
        return JSONResponse(
            {"error": {"type": "model_not_found", "message": "Model not found"}},
            status_code=404,
        )
    if fault == "503":
        return JSONResponse(
            {"error": {"type": "upstream_error", "message": "stub 503"}},
            status_code=503,
        )

    model = body.get("model", "stub-model")
    return JSONResponse(
        {
            "type": "llm",
            "instance_id": f"{model}:stub",
            "load_time_seconds": 1.5,
            "status": "loaded",
        },
        status_code=200,
    )


@_stub_lm_app.post("/api/v1/models/unload")
async def stub_unload_model(request: Request) -> JSONResponse:
    """Stub for POST /api/v1/models/unload.

    Returns the instance_id that was unloaded.
    Fault injection: ``X-Stub-Fault: 404`` → not loaded.
    """
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    fault = request.headers.get("x-stub-fault", "").strip()
    if fault == "404":
        return JSONResponse(
            {"error": {"type": "model_not_found", "message": "not loaded"}},
            status_code=404,
        )

    instance_id = body.get("instance_id", "stub-instance")
    return JSONResponse({"instance_id": instance_id}, status_code=200)


@_stub_lm_app.post("/api/v1/models/download")
async def stub_download_model(request: Request) -> JSONResponse:
    """Stub for POST /api/v1/models/download."""
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    model = body.get("model", "")
    if not model:
        return JSONResponse(
            {"error": {"type": "invalid_request", "message": "Missing required field 'model'"}},
            status_code=400,
        )
    if "/" not in model:
        return JSONResponse(
            {"error": {"type": "invalid_request", "message": "Invalid model name format"}},
            status_code=400,
        )
    return JSONResponse({"status": "ok", "model": model}, status_code=200)


# ---------------------------------------------------------------------------
# Module-scoped live server fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_servers(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[dict[str, Any]]:
    """Start a stub LM Studio server and a live main app server.

    Note on monkeypatch scope: ``monkeypatch`` is function-scoped; we
    cannot use it at module scope.  Instead we directly mutate env vars
    via ``os.environ`` before starting the server, then restore them.
    The server's lifespan reads settings once at startup so restoring
    after the yield is safe.

    Yields a dict:
        ``base_url``   — main app base URL, e.g. ``http://127.0.0.1:PORT``.
        ``db_path``    — path to the SQLite DB used by the main app.
        ``stub_port``  — port the stub LM Studio server listens on.
    """
    import os

    from lmchat.config import get_settings

    global _live_db_path  # noqa: PLW0603

    tmp = tmp_path_factory.mktemp("integration_db")
    db_path = tmp / "integration.db"
    _live_db_path = db_path
    stub_port = _free_port()
    app_port = _free_port()

    saved: dict[str, str | None] = {}
    env_overrides = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "LM_CHAT_SECRET": "integration-test-secret-32bytes!",
        "LM_STUDIO_BASE_URL": f"http://127.0.0.1:{stub_port}",
        "LM_STUDIO_API_KEY": "",
        "LM_CHAT_SINGLE_SESSION": "false",
        # The IntegrationsService falls back to reading
        # ~/.lmstudio/mcp.json when the DB list is empty. Disable
        # that here so the dev machine's mcp.json doesn't bleed into
        # hermetic test fixtures (the test "clears the list and
        # expects empty" would otherwise see the dev's MCP servers).
        "LM_CHAT_LOCAL_MCP_DISCOVERY_ENABLED": "false",
        # This session-scoped live server is shared by the WHOLE integration
        # suite: nearly every test that needs an authenticated user calls
        # register_and_login()/register_admin_and_login() below, each doing
        # a REAL POST /api/auth/login — hundreds of distinct usernames, all
        # from the same loopback test-client IP, within one process. That
        # volume is exactly what the per-IP login cap (closes the
        # username-rotation evasion — see rate_limit.py) is designed to
        # catch, but a real deployment never sees hundreds of logins from
        # one IP in minutes the way this fixture legitimately does. Raise
        # the cap here so the harness's own traffic doesn't trip a control
        # meant for attackers, not test fixtures; the production default
        # (30/min) is exercised directly by tests/middleware/test_rate_limit.py.
        "LM_CHAT_LOGIN_RATE_LIMIT_PER_IP_PER_MIN": "5000",
    }
    for k, v in env_overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    get_settings.cache_clear()

    # Reset the global engine so the lifespan picks up the new DATABASE_URL.
    from lmchat.db import engine as engine_mod

    engine_mod.dispose_engine()

    # Start stub LM Studio.
    stub_server = _start_server(_stub_lm_app, stub_port)

    # Build and start the main app.
    from lmchat.app import create_app

    main_app = create_app()
    main_server = _start_server(main_app, app_port)

    # Seed a placeholder row so the bootstrap-admin grant in
    # auth_service.register (auto-admin for the very first user) does not
    # accidentally promote the first test-driven registration.  Without this,
    # tests that expect a newly-registered user to be non-admin (e.g.
    # _make_fresh_regular) would fail when run in isolation against an
    # empty DB.  We use a one-shot synchronous sqlite3 connection so the
    # seed is committed before any test event loop spins up — avoids loop
    # / aiosqlite ownership concerns with the app's live engine.
    import sqlite3 as _sqlite3  # local alias — only used here

    with _sqlite3.connect(str(db_path)) as _seed_conn:
        _seed_conn.execute(
            "INSERT INTO users (username, password_hash, is_admin)"
            " VALUES ('__bootstrap_placeholder__',"
            " 'scrypt$0$0$0$0$0', 0)"
        )
        # Seed the admin default LM Studio config so the app's singletons
        # resolve to the stub URL.  2026-05-27: env fallback was removed from
        # resolve_admin_tier_only(); the lifespan now boots with an empty
        # base_url unless an admin default row exists.  Without this seed,
        # models_service._base_url stays "" and document upload tests fail
        # ("No embedding model is currently loaded").
        _stub_base = f"http://127.0.0.1:{stub_port}"
        _seed_conn.execute(
            "INSERT INTO server_lm_studio_default (id, base_url, api_key_enc, default_model)"
            f" VALUES (1, '{_stub_base}', NULL, NULL)"
        )
        _seed_conn.commit()

    # Patch the live singletons to point at the stub URL.  The lifespan
    # already ran (before _start_server returns) and resolved admin-tier
    # config to "" (no row existed then).  Now that we've seeded the row,
    # sync-patch the attributes so subsequent requests use the correct URL.
    # We also reset models_service._cache to None so the next list_loaded()
    # call refreshes from the stub rather than returning the empty [] that
    # the failed warmup left behind.
    from lmchat.services.lm_studio_overrides_service import EMBEDDINGS_PATH_CONST

    _stub_base = f"http://127.0.0.1:{stub_port}"
    _state = main_app.state
    _state.models_service._base_url = _stub_base
    _state.lmstudio_adapter._base_url = _stub_base
    _state.embedding_client._base_url = _stub_base
    _state.embedding_client._endpoint = f"{_stub_base}{EMBEDDINGS_PATH_CONST}"
    _state.models_service._cache = None  # force refresh on next list_loaded()

    yield {
        "base_url": f"http://127.0.0.1:{app_port}",
        "db_path": db_path,
        "stub_port": stub_port,
        "app": main_app,
    }

    # Shutdown — signal both servers and wait for them to stop.
    main_server.should_exit = True
    stub_server.should_exit = True
    # Give uvicorn time to drain in-flight requests and close the event loop.
    # 1.5 s is generous; the servers have no long-lived connections at this
    # point because all tests have finished.
    time.sleep(1.5)

    # Dispose the global engine so aiosqlite's worker thread doesn't try to
    # use an already-closed asyncio event loop in subsequent tests.
    engine_mod.dispose_engine()

    # Reset structlog to a clean state so tests that call configure_logging()
    # (e.g. test_logging_middleware.py) get a fresh processor chain.  Without
    # this, the processor chain configured by the lifespan (JSON output) is
    # still active and the tests' caplog-based assertions find zero records.
    import logging

    import structlog
    from structlog.contextvars import clear_contextvars

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

    # Restore env vars.
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Async HTTP client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def client(live_servers: dict[str, Any]) -> AsyncGenerator[httpx.AsyncClient]:
    """Module-scoped async httpx client pointed at the live server."""
    async with httpx.AsyncClient(
        base_url=live_servers["base_url"],
        follow_redirects=True,
        timeout=15.0,
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# DB engine for direct assertions
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db_engine(live_servers: dict[str, Any]) -> AsyncGenerator[AsyncEngine]:
    """Yield an async engine connected to the test DB for direct queries."""
    from lmchat.db.schema import metadata

    eng = create_async_engine(
        f"sqlite+aiosqlite:///{live_servers['db_path']}", pool_pre_ping=True
    )
    # Ensure schema exists (lifespan already created it, but belt+suspenders).
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


async def register_user(
    client: httpx.AsyncClient,
    username: str | None = None,
    password: str = "integration-pw-OK",
) -> dict[str, Any]:
    """Register a user; return a body dict with at least {id, username}.

    When a live-server DB path is known (``_live_db_path`` set by the
    ``live_servers`` fixture), this bypasses the single-admin registration
    gate by inserting directly into the DB with a low-cost scrypt hash.
    A ``GET /api/auth/me`` call is made afterwards to obtain the real user_id.
    """
    if username is None:
        username = f"u_{secrets.token_hex(6)}"
    if _live_db_path is not None:
        # Bypass the registration gate via direct DB insert.
        from lmchat.utils.hashing import hash_password as _hash_pw

        pw_hash = _hash_pw(password, n=_TEST_SCRYPT_N, r=8, p=1)
        import sqlite3 as _sqlite3

        with _sqlite3.connect(str(_live_db_path)) as _conn:
            row = _conn.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM users"
            ).fetchone()
            next_id = row[0] if row else 1
            _conn.execute(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (?, ?, ?)",
                (next_id, username, pw_hash),
            )
            _conn.commit()
        return {"id": next_id, "username": username}

    resp = await client.post(
        "/api/auth/register",
        data={"username": username, "password": password},
    )
    assert resp.status_code == 201, f"register failed: {resp.text}"
    return dict(resp.json())


async def login_user(
    client: httpx.AsyncClient,
    username: str,
    password: str = "integration-pw-OK",
    totp_code: str | None = None,
) -> httpx.Response:
    """POST /api/auth/login and return the raw response."""
    data: dict[str, str] = {"username": username, "password": password}
    if totp_code is not None:
        data["totp_code"] = totp_code
    return await client.post("/api/auth/login", data=data)


async def make_admin(db_engine: AsyncEngine, user_id: int) -> None:
    """Flip is_admin=1 for *user_id* directly in the DB."""
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET is_admin = 1 WHERE id = :uid"),
            {"uid": user_id},
        )


async def register_and_login(
    client: httpx.AsyncClient,
    username: str | None = None,
    password: str = "integration-pw-OK",
) -> tuple[dict[str, Any], str]:
    """Register a user and log in; return (user_body, session_cookie_value)."""
    user = await register_user(client, username, password)
    resp = await login_user(client, user["username"], password)
    assert resp.status_code == 200, f"login failed: {resp.text}"
    cookie: str = resp.cookies.get("lmchat_session") or ""
    return user, cookie


async def register_admin_and_login(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    username: str | None = None,
    password: str = "integration-pw-OK",
) -> tuple[dict[str, Any], str]:
    """Register an admin user, promote them, log in, return (body, cookie)."""
    user, _ = await register_and_login(client, username, password)
    await make_admin(db_engine, user["id"])
    # Re-login to get a cookie after the admin flag is set.
    resp = await login_user(client, user["username"], password)
    assert resp.status_code == 200, f"admin login failed: {resp.text}"
    cookie: str = resp.cookies.get("lmchat_session") or ""
    return user, cookie
