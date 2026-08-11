# SPDX-License-Identifier: Apache-2.0
"""OpenTelemetry SDK initialisation for the stress harness.

Gated on ``LM_CHAT_OTEL_ENABLED=true``.  When unset OR ``"false"``,
``initialise()`` is a no-op so the production app path is unchanged.

Configuration env vars:

- ``LM_CHAT_OTEL_ENABLED``         — gate (``true`` activates).
- ``LM_CHAT_OTEL_SERVICE_NAME``    — default ``lm-chat``.
- ``LM_CHAT_OTEL_EXPORTER_OTLP_ENDPOINT``
                                   — default ``http://localhost:4317``
                                   (Jaeger compose's OTLP gRPC ingest).
- ``LM_CHAT_OTEL_SAMPLER_RATIO``   — float in [0, 1], default 0.1
                                     (10% sampling).

Returns silently on init failure so a misconfigured OTel endpoint cannot
crash the FastAPI startup.  Failures are logged at WARN.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("stress.tracing.otel_setup")

_INITIALISED = False


def is_enabled() -> bool:
    """Return True iff ``LM_CHAT_OTEL_ENABLED=true`` is set in the env."""
    return os.environ.get("LM_CHAT_OTEL_ENABLED", "").lower() == "true"


def initialise(app: object | None = None) -> bool:
    """Initialise the OpenTelemetry SDK + instrument FastAPI + httpx.

    Args:
        app: Optional FastAPI app; when provided, FastAPI instrumentation
             is attached so every route generates a server span.

    Returns:
        True when initialisation actually ran, False when no-op
        (disabled or already initialised).
    """
    global _INITIALISED
    if _INITIALISED:
        return False
    if not is_enabled():
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except ImportError as exc:  # pragma: no cover - dep missing
        log.warning("otel imports failed: %s", exc)
        return False

    service_name = os.environ.get("LM_CHAT_OTEL_SERVICE_NAME", "lm-chat")
    endpoint = os.environ.get(
        "LM_CHAT_OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://localhost:4317",
    )
    try:
        ratio = float(os.environ.get("LM_CHAT_OTEL_SAMPLER_RATIO", "0.1"))
    except ValueError:
        ratio = 0.1
    ratio = max(0.0, min(1.0, ratio))

    try:
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(
            resource=resource, sampler=TraceIdRatioBased(ratio)
        )
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception as exc:  # noqa: BLE001
        log.warning("otel tracer provider setup failed: %s", exc)
        return False

    # FastAPI instrumentation — wraps every request in a span.
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            log.warning("otel fastapi instrument failed: %s", exc)

    # httpx instrumentation — adds spans + injects traceparent into
    # outbound calls (LM Studio path picks it up).
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        log.warning("otel httpx instrument failed: %s", exc)

    _INITIALISED = True
    log.info(
        "otel initialised: service=%s endpoint=%s sampler_ratio=%s",
        service_name, endpoint, ratio,
    )
    return True
