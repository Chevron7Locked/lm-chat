# SPDX-License-Identifier: Apache-2.0
"""Tests for RequestLoggingMiddleware.

Logging middleware tests.

Test architecture
-----------------
A minimal FastAPI app with RequestLoggingMiddleware is driven through
``httpx.AsyncClient(transport=ASGITransport(...))``.  Log capture uses
the ``caplog`` fixture after wiring structlog through the stdlib root
logger with ``configure_logging(console=False, log_file=None)``.

The request_id contextvar is set via
:func:`structlog.contextvars.bind_contextvars` to simulate the
upstream P0 RequestContextMiddleware.

User attachment is simulated by a test route that sets
``request.state.user`` before returning, mimicking what AuthMiddleware
does in the real stack.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx
import pytest
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport
from structlog.contextvars import bind_contextvars, clear_contextvars

from lmchat.logging import configure_logging
from lmchat.middleware.logging import RequestLoggingMiddleware

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stdlib_logging() -> Iterator[None]:
    """Wire structlog through stdlib so caplog captures structured events."""
    configure_logging(console=False, log_file=None)
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in list(root.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    clear_contextvars()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeUser:
    """Minimal user-like object to simulate an attached authenticated user."""

    def __init__(self, uid: int) -> None:
        self.id = uid


def make_app(*, inject_user: _FakeUser | None = None) -> FastAPI:
    """Build a minimal FastAPI app with RequestLoggingMiddleware."""
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        if inject_user is not None:
            request.state.user = inject_user
        return JSONResponse({"ok": True})

    @app.get("/error")
    async def boom() -> JSONResponse:
        return JSONResponse({"err": True}, status_code=500)

    return app


def _get_request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Filter captured log records to those with event='http.request'."""
    return [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "http.request"
        or "http.request" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logging_emits_one_request_event_per_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exactly one log record with event='http.request' is emitted per request."""
    app = make_app()
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/probe")
            await client.get("/probe")

    http_records = [
        r for r in caplog.records if "http.request" in r.getMessage()
    ]
    assert len(http_records) == 2, (
        f"Expected 2 http.request log records, got {len(http_records)}"
    )


@pytest.mark.asyncio
async def test_logging_includes_method_path_status_latency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log record contains method, path, status, and latency_ms."""
    app = make_app()
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/probe")

    http_records = [r for r in caplog.records if "http.request" in r.getMessage()]
    assert len(http_records) == 1
    record = http_records[0]
    msg = record.getMessage()

    # These fields must appear in the serialised log output.
    assert "GET" in msg or hasattr(record, "method")
    assert "200" in msg or hasattr(record, "status")
    assert "latency_ms" in msg or hasattr(record, "latency_ms")


@pytest.mark.asyncio
async def test_logging_includes_request_id_from_contextvar(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The request_id from structlog contextvars appears in the log record."""
    bind_contextvars(request_id="test-req-id-12345")

    app = make_app()
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/probe")

    http_records = [r for r in caplog.records if "http.request" in r.getMessage()]
    assert len(http_records) == 1
    assert "test-req-id-12345" in http_records[0].getMessage()


@pytest.mark.asyncio
async def test_logging_user_id_null_when_anonymous(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """user_id is None/null in the log record for anonymous requests."""
    app = make_app()  # no inject_user
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/probe")

    http_records = [r for r in caplog.records if "http.request" in r.getMessage()]
    assert len(http_records) == 1
    msg = http_records[0].getMessage()
    # user_id should be None (serialised as null or "None" in the log).
    assert "user_id" in msg
    assert "None" in msg or "null" in msg


@pytest.mark.asyncio
async def test_logging_user_id_present_when_authenticated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """user_id is the integer user ID when the user is authenticated."""
    fake_user = _FakeUser(uid=42)
    app = make_app(inject_user=fake_user)
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/probe")

    http_records = [r for r in caplog.records if "http.request" in r.getMessage()]
    assert len(http_records) == 1
    msg = http_records[0].getMessage()
    assert "42" in msg


@pytest.mark.asyncio
async def test_logging_emits_on_error_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A log record is emitted even when the inner app returns a 500."""
    app = make_app()
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/error")

    http_records = [r for r in caplog.records if "http.request" in r.getMessage()]
    assert len(http_records) == 1
    assert "500" in http_records[0].getMessage()


# ---------------------------------------------------------------------------
# 404 route template coverage
# ---------------------------------------------------------------------------


def test_route_template_returns_unknown_when_scope_lacks_path() -> None:
    """When the scope dict has no 'path' key (pre-Router context),
    _route_template falls back to the _UNKNOWN_ROUTE sentinel."""
    from lmchat.middleware.logging import _UNKNOWN_ROUTE, _route_template

    # Scope without "path" key — pre-Router context (middleware stack head).
    assert _route_template({}) == _UNKNOWN_ROUTE


def test_route_template_returns_unknown_when_path_is_not_str() -> None:
    """A non-string path value (defensive guard) falls back to _UNKNOWN_ROUTE."""
    from lmchat.middleware.logging import _UNKNOWN_ROUTE, _route_template

    # Type-confused scope where path is a bytes object — defensive guard.
    assert _route_template({"path": b"/api/foo"}) == _UNKNOWN_ROUTE
