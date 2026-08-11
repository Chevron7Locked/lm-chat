# SPDX-License-Identifier: Apache-2.0
"""Contract tests for ``lmchat.middleware.request_context``.

Covers request context injection:
- Read ``X-Request-ID`` header when present; echo to
  ``Request-ID`` response header.
- Generate ``secrets.token_urlsafe(12)`` (16 URL-safe chars) when
  absent.
- Bind to structlog contextvars so logs emitted during the
  request include ``request_id``.
- Non-HTTP scopes (lifespan / websocket) pass through untouched.
- No contextvar leakage between requests sharing the same task.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import structlog.contextvars as cvars
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from lmchat.logging import configure_logging, get_logger
from lmchat.middleware.request_context import RequestContextMiddleware


def _build_app(log_file: Path | None = None) -> FastAPI:
    """Test app: middleware + a route that emits a structured log."""
    if log_file is not None:
        configure_logging(console=False, log_file=log_file)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        get_logger(__name__).info("ping_handled")
        return {"ok": "true"}

    return app


def _flush() -> None:
    for h in logging.getLogger().handlers:
        h.flush()


def test_request_id_echoed_when_provided() -> None:
    client = TestClient(_build_app())
    resp = client.get("/ping", headers={"X-Request-ID": "client-supplied-12345"})
    assert resp.status_code == 200
    assert resp.headers["request-id"] == "client-supplied-12345"


def test_request_id_generated_when_absent() -> None:
    client = TestClient(_build_app())
    resp = client.get("/ping")
    assert resp.status_code == 200
    rid = resp.headers["request-id"]
    # secrets.token_urlsafe(12) → exactly 16 URL-safe chars.
    assert len(rid) == 16
    assert re.match(r"^[A-Za-z0-9_-]+$", rid)


def test_each_request_gets_unique_id() -> None:
    client = TestClient(_build_app())
    ids = {client.get("/ping").headers["request-id"] for _ in range(5)}
    assert len(ids) == 5


def test_request_id_propagates_to_log_lines(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    client = TestClient(_build_app(log_file=log_file))
    rid = "log-propagation-rid-x9"
    client.get("/ping", headers={"X-Request-ID": rid})
    _flush()

    entries = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    handled = next(e for e in entries if e.get("event") == "ping_handled")
    assert handled["request_id"] == rid


def test_contextvars_unbound_after_request(tmp_path: Path) -> None:
    """request_id must NOT leak into the contextvars scope after response."""
    configure_logging(console=False, log_file=tmp_path / "x.log")
    client = TestClient(_build_app())
    cvars.clear_contextvars()
    client.get("/ping", headers={"X-Request-ID": "leak-test"})
    assert cvars.get_contextvars().get("request_id") is None


def test_malformed_header_fallback_to_generated() -> None:
    """Non-ASCII header bytes fall back to generation; no crash."""
    async def inner(scope: Scope, receive: Any, send: Any) -> None:  # noqa: ARG001
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent_messages: list[Message] = []

    async def send(message: Message) -> None:
        sent_messages.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = RequestContextMiddleware(inner)
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/ping",
        "headers": [(b"x-request-id", b"\xc3\x28"), (b"host", b"test")],  # malformed UTF-8
    }
    asyncio.run(mw(scope, receive, send))

    # Response should still include a Request-ID, generated since the
    # malformed header was rejected.
    start = next(m for m in sent_messages if m["type"] == "http.response.start")
    request_id_headers = [v for k, v in start["headers"] if k == b"request-id"]
    assert len(request_id_headers) == 1
    rid = request_id_headers[0].decode("ascii")
    assert len(rid) == 16


def test_non_http_scope_passes_through() -> None:
    """Lifespan + WebSocket scopes are forwarded with no headers added."""
    inner_invocations: list[Scope] = []

    async def inner(scope: Scope, receive: Any, send: Any) -> None:  # noqa: ARG001
        inner_invocations.append(scope)

    mw = RequestContextMiddleware(inner)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    sent_messages: list[Message] = []

    async def send(message: Message) -> None:
        sent_messages.append(message)

    asyncio.run(mw({"type": "lifespan", "headers": []}, receive, send))

    assert len(inner_invocations) == 1
    assert inner_invocations[0]["type"] == "lifespan"
    # No Request-ID was added to any messages (none were sent by inner anyway).
    assert not any(b"request-id" in dict(m.get("headers", [])) for m in sent_messages)
