# SPDX-License-Identifier: Apache-2.0
"""Contract tests for ``lmchat.metrics`` (the registration module).

Each metric must exist with
the correct type, name, and label set the moment the module is
imported. The contract is **wire-format**: tests verify the
Prometheus exposition output rather than internal attributes
(prometheus_client strips the ``_total`` suffix from a
Counter's internal ``_name`` field — the wire format keeps it).
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest

from lmchat import metrics


def _exposition() -> str:
    """Render the current default registry as Prometheus text format."""
    return generate_latest().decode("utf-8")


def test_request_count_is_counter_with_expected_labels() -> None:
    """lm_chat_requests_total{method, path, status} (Counter)."""
    assert isinstance(metrics.REQUEST_COUNT, Counter)
    metrics.REQUEST_COUNT.labels(method="GET", path="/x", status="200")
    text = _exposition()
    assert "# TYPE lm_chat_requests_total counter" in text
    assert 'lm_chat_requests_total{method="GET",path="/x",status="200"}' in text


def test_request_latency_is_histogram_with_expected_labels() -> None:
    """lm_chat_request_latency_seconds{method, path} (Histogram)."""
    assert isinstance(metrics.REQUEST_LATENCY, Histogram)
    metrics.REQUEST_LATENCY.labels(method="GET", path="/x").observe(0.01)
    text = _exposition()
    assert "# TYPE lm_chat_request_latency_seconds histogram" in text
    assert 'lm_chat_request_latency_seconds_count{method="GET",path="/x"}' in text


def test_streams_completed_is_counter_without_labels() -> None:
    """lm_chat_streams_completed_total (Counter, no labels)."""
    assert isinstance(metrics.STREAMS_COMPLETED, Counter)
    metrics.STREAMS_COMPLETED.inc()
    text = _exposition()
    assert "# TYPE lm_chat_streams_completed_total counter" in text
    assert "lm_chat_streams_completed_total " in text


def test_streams_failed_has_reason_label() -> None:
    """lm_chat_streams_failed_total{reason} (Counter)."""
    assert isinstance(metrics.STREAMS_FAILED, Counter)
    metrics.STREAMS_FAILED.labels(reason="client_disconnect").inc()
    text = _exposition()
    assert "# TYPE lm_chat_streams_failed_total counter" in text
    assert 'lm_chat_streams_failed_total{reason="client_disconnect"}' in text


def test_streams_active_is_gauge_without_labels() -> None:
    """lm_chat_streams_active (Gauge, no labels)."""
    assert isinstance(metrics.STREAMS_ACTIVE, Gauge)
    metrics.STREAMS_ACTIVE.set(0)
    text = _exposition()
    assert "# TYPE lm_chat_streams_active gauge" in text
    assert "lm_chat_streams_active " in text


def test_memory_distillations_is_counter() -> None:
    """lm_chat_memory_distillations_total (Counter)."""
    assert isinstance(metrics.MEMORY_DISTILLATIONS, Counter)
    metrics.MEMORY_DISTILLATIONS.inc()
    text = _exposition()
    assert "# TYPE lm_chat_memory_distillations_total counter" in text
    assert "lm_chat_memory_distillations_total " in text


def test_memory_reindexed_is_counter() -> None:
    """lm_chat_memory_reindexed_total (Counter, P3 amendment)."""
    assert isinstance(metrics.MEMORY_REINDEXED, Counter)
    metrics.MEMORY_REINDEXED.inc()
    text = _exposition()
    assert "# TYPE lm_chat_memory_reindexed_total counter" in text
    assert "lm_chat_memory_reindexed_total " in text


def test_all_contract_metrics_appear_in_exposition() -> None:
    """generate_latest() emits every contract metric name."""
    # Touch each labeled child so the metric appears in the
    # exposition (Prometheus omits never-incremented label sets).
    metrics.REQUEST_COUNT.labels(method="GET", path="/x", status="200")
    metrics.REQUEST_LATENCY.labels(method="GET", path="/x")
    metrics.STREAMS_FAILED.labels(reason="client_disconnect")
    # P12c QM-003: register lm_chat_models_probe_dropped_total so the counter
    # appears in the exposition before the assertion (Prometheus client omits
    # label-sets that have never been incremented).
    metrics.MODELS_PROBE_DROPPED.labels(reason="test").inc(0)

    text = _exposition()
    expected_names = [
        "lm_chat_memory_distillations_total",
        "lm_chat_memory_reindexed_total",
        "lm_chat_models_probe_dropped_total",
        "lm_chat_request_latency_seconds",
        "lm_chat_requests_total",
        "lm_chat_streams_active",
        "lm_chat_streams_completed_total",
        "lm_chat_streams_failed_total",
    ]
    for name in expected_names:
        assert name in text, f"missing metric: {name}"
