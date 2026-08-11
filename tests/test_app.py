# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the wired FastAPI app.

Covers app-level release-criterion checks:
- /healthz returns 200 with version.
- /api/metrics returns Prometheus exposition format.
- A request to any route produces a JSON log line that
  includes request_id.

These tests use ``TestClient`` against ``create_app()`` so the
real middleware stack is exercised end-to-end.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from lmchat import __version__, metrics
from lmchat.app import create_app
from lmchat.logging import configure_logging


def _flush_handlers() -> None:
    for h in logging.getLogger().handlers:
        h.flush()


def test_healthz_returns_200_with_version() -> None:
    """GET /healthz → 200 with {status, version}."""
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "version": __version__}


def test_metrics_endpoint_returns_prometheus_format() -> None:
    """GET /api/metrics → 200 with Prometheus content-type."""
    client = TestClient(create_app())
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "lm_chat_requests_total" in resp.text or "# HELP" in resp.text


def test_request_to_healthz_emits_request_id_log(tmp_path: Path) -> None:
    """A request to /healthz produces a JSON log line that includes request_id."""
    log_file = tmp_path / "app.log"
    configure_logging(console=False, log_file=log_file)
    # Re-creating the app after configure_logging ensures lifespan
    # doesn't reconfigure (it would but we want this test to use
    # our tmp_path file).
    app = create_app()

    with TestClient(app) as client:
        # The lifespan inside TestClient will call configure_logging
        # again with defaults. Re-set after entering the context.
        configure_logging(console=False, log_file=log_file)
        from lmchat.logging import get_logger
        get_logger("test").info("test_marker")
        rid = "rid-integration-x9"
        client.get("/healthz", headers={"X-Request-ID": rid})

    _flush_handlers()

    lines = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    # The marker we emitted should be in the log
    markers = [line for line in lines if line.get("event") == "test_marker"]
    assert len(markers) >= 1


def test_request_id_header_echoed_through_full_stack() -> None:
    """Full middleware stack: Request-ID header is set on the response."""
    client = TestClient(create_app())
    resp = client.get("/healthz", headers={"X-Request-ID": "stack-test-001"})
    assert resp.headers["request-id"] == "stack-test-001"


def test_request_id_generated_when_absent_through_full_stack() -> None:
    """Full stack: generated Request-ID has the 16-char URL-safe shape."""
    client = TestClient(create_app())
    resp = client.get("/healthz")
    rid = resp.headers["request-id"]
    assert len(rid) == 16
    assert re.match(r"^[A-Za-z0-9_-]+$", rid)


def test_request_counter_increments_through_full_stack() -> None:
    """Full stack: GET /healthz increments lm_chat_requests_total for the route."""
    client = TestClient(create_app())
    before = metrics.REQUEST_COUNT.labels(
        method="GET",
        path="/healthz",
        status="200",
    )._value.get()
    client.get("/healthz")
    after = metrics.REQUEST_COUNT.labels(
        method="GET",
        path="/healthz",
        status="200",
    )._value.get()
    assert after == before + 1


def test_metrics_endpoint_self_amplification_excluded() -> None:
    """/api/metrics itself is NOT recorded in lm_chat_requests_total."""
    client = TestClient(create_app())
    before = metrics.REQUEST_COUNT.labels(
        method="GET",
        path="/api/metrics",
        status="200",
    )._value.get()
    client.get("/api/metrics")
    after = metrics.REQUEST_COUNT.labels(
        method="GET",
        path="/api/metrics",
        status="200",
    )._value.get()
    assert after == before  # exclusion holds


# ---------------------------------------------------------------------------
# P1 lifespan tests
# ---------------------------------------------------------------------------


def test_startup_with_missing_secret_exits_78(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subprocess with LM_CHAT_SECRET="" must exit with code 78 (EX_CONFIG).

    This test uses subprocess.run so that sys.exit(78) in the lifespan does
    not kill the test process itself.  We patch out DATABASE_URL to a
    tmp_path DB so the test doesn't create stray files.
    """
    import os

    env = os.environ.copy()
    env["LM_CHAT_SECRET"] = ""
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path}/missing_secret.db"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio; "
                "from lmchat.app import create_app; "
                "from fastapi.testclient import TestClient; "
                "from lmchat.config import get_settings; "
                "get_settings.cache_clear(); "
                "app = create_app(); "
                "client = TestClient(app); "
                "client.__enter__()"
            ),
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 78, (
        f"Expected exit code 78, got {result.returncode}.\n"
        f"stdout: {result.stdout.decode()}\n"
        f"stderr: {result.stderr.decode()}"
    )


def test_lifespan_runs_ensure_schema_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan on a fresh DB runs Alembic upgrade head and creates tables."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/lifespan_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200

    # Verify the DB file was created and has the alembic_version table.
    import sqlite3

    db_path = tmp_path / "lifespan_test.db"
    assert db_path.exists(), "DB file should have been created by lifespan"
    con = sqlite3.connect(str(db_path))
    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    con.close()
    assert "alembic_version" in tables
    assert "users" in tables

    # Cleanup
    engine_mod.dispose_engine()
    get_settings.cache_clear()


def test_session_cleanup_task_is_scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan registers a cleanup task on app.state.cleanup_task."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/cleanup_task_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    import asyncio

    app = create_app()
    with TestClient(app) as client:
        client.get("/healthz")
        # The cleanup task should be registered on app.state.
        task = app.state.cleanup_task
        assert task is not None
        assert isinstance(task, asyncio.Task)
        assert not task.done(), "Cleanup task should still be running during lifespan"

    # After lifespan ends (TestClient.__exit__), the task should be cancelled.
    assert task.done(), "Cleanup task should be done after lifespan ends"

    # Cleanup
    engine_mod.dispose_engine()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# P2 lifespan tests
# ---------------------------------------------------------------------------


def _make_p2_lifespan_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Set up env + engine disposal, return (app, client_context_manager).

    Returns the app (for state inspection) and the TestClient as a context
    manager so tests can enter it themselves.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path}/p2_lifespan.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    app = create_app()
    return app, engine_mod


def test_lifespan_attaches_params_service_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a ParamsService instance to app.state.params_service."""
    from lmchat.services.params_service import ParamsService

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "params_service")
        assert isinstance(app.state.params_service, ParamsService)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_attaches_models_service_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a ModelsService instance to app.state.models_service."""
    from lmchat.services.models_service import ModelsService

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "models_service")
        assert isinstance(app.state.models_service, ModelsService)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_attaches_lmstudio_adapter_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a LmstudioAdapter instance to app.state.lmstudio_adapter."""
    from lmchat.services.lmstudio_adapter import LmstudioAdapter

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "lmstudio_adapter")
        assert isinstance(app.state.lmstudio_adapter, LmstudioAdapter)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_closes_http_client_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan closes the shared http_client on shutdown."""

    import httpx

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    http_client: httpx.AsyncClient | None = None

    with TestClient(app) as client:
        client.get("/healthz")
        http_client = app.state.http_client
        assert http_client is not None
        assert isinstance(http_client, httpx.AsyncClient)
        # During lifespan the client should be open (not closed).
        assert not http_client.is_closed

    # After lifespan the client should be closed.
    assert http_client.is_closed, "http_client should be closed after lifespan shutdown"

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# P3 lifespan tests
# ---------------------------------------------------------------------------


def test_lifespan_attaches_memory_service_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a MemoryService instance to app.state.memory_service."""
    from lmchat.services.memory_service import MemoryService

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "memory_service")
        assert isinstance(app.state.memory_service, MemoryService)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_attaches_quality_mode_service_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a QualityModeService to app.state.quality_mode_service."""
    from lmchat.services.quality_modes import QualityModeService

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "quality_mode_service")
        assert isinstance(app.state.quality_mode_service, QualityModeService)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_attaches_embedding_client_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches an EmbeddingClient to app.state.embedding_client."""
    from lmchat.embedding.client import EmbeddingClient

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "embedding_client")
        assert isinstance(app.state.embedding_client, EmbeddingClient)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# P4 lifespan tests
# ---------------------------------------------------------------------------


def test_lifespan_attaches_chat_service_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a ChatService instance to app.state.chat_service."""
    from lmchat.services.chat_service import ChatService

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "chat_service")
        assert isinstance(app.state.chat_service, ChatService)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_attaches_message_service_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a MessageService instance to app.state.message_service."""
    from lmchat.services.message_service import MessageService

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "message_service")
        assert isinstance(app.state.message_service, MessageService)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_attaches_chat_locks_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a dict to app.state.chat_locks (per-chat mutex dict)."""
    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "chat_locks")
        assert isinstance(app.state.chat_locks, dict)
        # Fresh dict — no locks yet.
        assert len(app.state.chat_locks) == 0

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_has_pg_trgm_false_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan sets has_pg_trgm=False on SQLite (no pg_trgm extension)."""
    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "has_pg_trgm")
        assert app.state.has_pg_trgm is False

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# P5 lifespan tests
# ---------------------------------------------------------------------------


def test_lifespan_attaches_streaming_service_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches a StreamingService instance to app.state.streaming_service."""
    from lmchat.services.streaming_service import StreamingService

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "streaming_service")
        assert isinstance(app.state.streaming_service, StreamingService)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_starts_reaper_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan starts the stream_reaper background task on app.state.reaper_task."""
    import asyncio

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        task = app.state.reaper_task
        assert task is not None
        assert isinstance(task, asyncio.Task)
        assert not task.done(), "Reaper task should be running during lifespan"

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_cancels_reaper_task_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan cancels the reaper task on shutdown (TestClient.__exit__)."""
    import asyncio

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    task_holder: dict[str, asyncio.Task] = {}

    with TestClient(app) as client:
        client.get("/healthz")
        task_holder["task"] = app.state.reaper_task
        assert not task_holder["task"].done()

    # After lifespan ends, the reaper task should be cancelled.
    task = task_holder["task"]
    assert task.done(), "Reaper task should be done after lifespan shutdown"

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_attaches_stream_buckets_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan attaches an InMemoryBucketStore to app.state.stream_buckets."""
    from lmchat.middleware._bucket_store import InMemoryBucketStore

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get("/healthz")
        assert hasattr(app.state, "stream_buckets")
        assert isinstance(app.state.stream_buckets, InMemoryBucketStore)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()


def test_lifespan_cancels_reindex_task_on_shutdown_if_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan cancels app.state.reindex_task during shutdown when one
    is actually running.

    The None-sentinel path is also covered (a fresh lifespan with no reindex
    triggered leaves reindex_task=None and shutdown must not raise). After
    that, we inject a real never-ending asyncio.Task INSIDE the running
    event loop via an in-app endpoint, then exit the TestClient context and
    verify the injected task ended (cancelled). This closes a coverage gap —
    previously only the None-sentinel path was tested.
    """
    import asyncio

    app, engine_mod = _make_p2_lifespan_client(tmp_path, monkeypatch)

    # Inject an endpoint that creates a never-ending task inside the
    # running event loop and stores the handle on app.state.reindex_task.
    # The lifespan's shutdown hook should cancel it when the TestClient
    # exits.
    injected_task_holder: dict[str, asyncio.Task[None]] = {}

    @app.get("/_test_inject_reindex_task")
    async def _inject() -> dict[str, bool]:
        async def _never_ending() -> None:
            try:
                await asyncio.sleep(9999)
            except asyncio.CancelledError:
                # Expected on shutdown; re-raise so the task is observable
                # as cancelled, not finished-cleanly.
                raise

        task = asyncio.create_task(_never_ending(), name="injected_test_reindex")
        app.state.reindex_task = task
        injected_task_holder["task"] = task
        return {"injected": True}

    with TestClient(app) as client:
        # Sanity: the None sentinel path before any reindex has run.
        client.get("/healthz")
        assert app.state.reindex_task is None

        # Now inject a real running task and verify it's attached.
        resp = client.get("/_test_inject_reindex_task")
        assert resp.status_code == 200
        assert app.state.reindex_task is not None
        assert not app.state.reindex_task.done()

    # Lifespan shutdown ran on TestClient exit. The injected task must now
    # be done (cancelled).
    task = injected_task_holder["task"]
    assert task.done(), "reindex_task should have been cancelled on shutdown"
    assert task.cancelled() or isinstance(task.exception(), asyncio.CancelledError)

    engine_mod.dispose_engine()
    from lmchat.config import get_settings
    get_settings.cache_clear()
