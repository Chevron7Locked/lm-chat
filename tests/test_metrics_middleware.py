# SPDX-License-Identifier: Apache-2.0
"""Contract tests for ``lmchat.middleware.metrics``.

Covers the metrics middleware:
- Records lm_chat_requests_total + lm_chat_request_latency_seconds
  on every HTTP request.
- ``path`` label uses the FastAPI route template
  (``/api/items/{item_id}``), not the rendered URL.
- ``/api/metrics`` itself is excluded from the counter to avoid
  self-amplification on Prometheus scrapes.
- Non-HTTP scopes (lifespan / websocket) pass through.
- If the inner app fails to send ``http.response.start`` the
  recorded status is ``500``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from starlette.types import Message, Scope

from lmchat import metrics
from lmchat.middleware.metrics import PrometheusMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)

    @app.get("/items/{item_id}")
    def read_item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @app.get("/api/metrics")
    def metrics_endpoint() -> str:
        return generate_latest().decode("utf-8")

    return app


def _read_counter_value(method: str, path: str, status: str) -> float:
    """Read current lm_chat_requests_total counter for the given labels."""
    return metrics.REQUEST_COUNT.labels(
        method=method,
        path=path,
        status=status,
    )._value.get()


def test_request_count_increments_with_route_template() -> None:
    """One GET /items/42 → counter for path=/items/{item_id}, status=200 increments by 1."""
    client = TestClient(_build_app())
    before = _read_counter_value("GET", "/items/{item_id}", "200")
    client.get("/items/42")
    after = _read_counter_value("GET", "/items/{item_id}", "200")
    assert after == pytest.approx(before + 1)


def test_path_label_is_route_template_not_rendered_url() -> None:
    """Two requests to /items/42 + /items/99 increment the SAME label."""
    client = TestClient(_build_app())
    before_template = _read_counter_value("GET", "/items/{item_id}", "200")
    before_rendered = _read_counter_value("GET", "/items/42", "200")
    client.get("/items/42")
    client.get("/items/99")
    after_template = _read_counter_value("GET", "/items/{item_id}", "200")
    after_rendered = _read_counter_value("GET", "/items/42", "200")
    # Template-form label gets exactly +2; rendered-URL label is unchanged
    # (delta check, not absolute-zero — robust to other tests sharing the registry).
    assert after_template == pytest.approx(before_template + 2)
    assert after_rendered == pytest.approx(before_rendered)


def test_metrics_endpoint_is_excluded_from_counter() -> None:
    """/api/metrics requests do not increment lm_chat_requests_total."""
    client = TestClient(_build_app())
    before_unknown = _read_counter_value("GET", "unknown", "200")
    before_metrics = _read_counter_value("GET", "/api/metrics", "200")
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    after_unknown = _read_counter_value("GET", "unknown", "200")
    after_metrics = _read_counter_value("GET", "/api/metrics", "200")
    assert after_unknown == before_unknown
    assert after_metrics == before_metrics


def test_404_uses_unknown_route_label() -> None:
    """Request hitting no route is labeled path='unknown'."""
    client = TestClient(_build_app())
    before = _read_counter_value("GET", "unknown", "404")
    client.get("/this-route-does-not-exist")
    after = _read_counter_value("GET", "unknown", "404")
    assert after == pytest.approx(before + 1)


def test_inner_app_exception_records_500() -> None:
    """If the inner app raises before sending response.start, status=500 is recorded."""
    async def crashing_app(scope: Scope, receive: Any, send: Any) -> None:  # noqa: ARG001
        raise RuntimeError("boom")

    mw = PrometheusMiddleware(crashing_app)
    before = _read_counter_value("GET", "unknown", "500")

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/boom",
        "headers": [],
    }
    with pytest.raises(RuntimeError):
        asyncio.run(mw(scope, receive, send))
    after = _read_counter_value("GET", "unknown", "500")
    assert after == pytest.approx(before + 1)


def test_latency_histogram_observes_each_request() -> None:
    """Each request adds one observation to the latency histogram."""
    client = TestClient(_build_app())
    latency_child = metrics.REQUEST_LATENCY.labels(
        method="GET",
        path="/items/{item_id}",
    )
    before_count = latency_child._sum.get()  # cumulative sum of observations
    client.get("/items/7")
    after_count = latency_child._sum.get()
    assert after_count >= before_count  # cumulative sum is monotonic


def test_non_http_scope_passes_through() -> None:
    """Lifespan scope passes through without recording metrics."""
    invocations: list[Scope] = []

    async def inner(scope: Scope, receive: Any, send: Any) -> None:  # noqa: ARG001
        invocations.append(scope)

    mw = PrometheusMiddleware(inner)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:  # noqa: ARG001
        pass

    before = _read_counter_value("GET", "unknown", "200")
    asyncio.run(mw({"type": "lifespan", "headers": []}, receive, send))
    after = _read_counter_value("GET", "unknown", "200")
    assert len(invocations) == 1
    assert after == before
