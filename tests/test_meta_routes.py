# SPDX-License-Identifier: Apache-2.0
"""Contract tests for ``lmchat.routes._meta``.

Covers meta routes (/readyz, /metrics):
- /api/metrics returns 200.
- Content-type is the Prometheus exposition media type
  (``text/plain; version=0.0.4; charset=utf-8``).
- Body includes every contract metric name.
- The endpoint is excluded from the OpenAPI schema (operations
  surface, not SPA surface).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from lmchat import metrics
from lmchat.routes._meta import router as meta_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(meta_router)
    return app


def test_metrics_endpoint_returns_200() -> None:
    """GET /api/metrics returns HTTP 200."""
    client = TestClient(_build_app())
    resp = client.get("/api/metrics")
    assert resp.status_code == 200


def test_metrics_endpoint_uses_prometheus_content_type() -> None:
    """Content-Type matches the Prometheus exposition spec."""
    client = TestClient(_build_app())
    resp = client.get("/api/metrics")
    # CONTENT_TYPE_LATEST is "text/plain; version=0.0.4; charset=utf-8"
    assert resp.headers["content-type"] == CONTENT_TYPE_LATEST


def test_metrics_endpoint_emits_all_contract_metrics() -> None:
    """Body contains every contract metric name."""
    # Touch each metric so children are registered (Prometheus
    # only emits label sets that have been observed).
    metrics.REQUEST_COUNT.labels(method="GET", path="/x", status="200")
    metrics.REQUEST_LATENCY.labels(method="GET", path="/x")
    metrics.STREAMS_FAILED.labels(reason="client_disconnect")
    metrics.STREAMS_COMPLETED.inc()
    metrics.STREAMS_ACTIVE.set(0)
    metrics.MEMORY_DISTILLATIONS.inc()
    metrics.MEMORY_REINDEXED.inc()

    client = TestClient(_build_app())
    resp = client.get("/api/metrics")
    body = resp.text

    for name in (
        "lm_chat_requests_total",
        "lm_chat_request_latency_seconds",
        "lm_chat_streams_completed_total",
        "lm_chat_streams_failed_total",
        "lm_chat_streams_active",
        "lm_chat_memory_distillations_total",
        "lm_chat_memory_reindexed_total",
    ):
        assert name in body, f"missing metric: {name}"


def test_metrics_endpoint_excluded_from_openapi_schema() -> None:
    """The metrics route is not advertised in the OpenAPI document."""
    client = TestClient(_build_app())
    spec = client.get("/openapi.json").json()
    assert "/api/metrics" not in spec.get("paths", {})


def test_metrics_endpoint_body_is_not_empty_when_no_metrics_observed() -> None:
    """Even with no app traffic, the registry emits process_* metrics by default."""
    client = TestClient(_build_app())
    resp = client.get("/api/metrics")
    # prometheus_client's default registry includes Python GC + process
    # metrics. The endpoint must never return an empty body.
    assert len(resp.text) > 0
