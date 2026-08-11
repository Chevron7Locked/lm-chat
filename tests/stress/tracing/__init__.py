# SPDX-License-Identifier: Apache-2.0
"""OpenTelemetry tracing scaffolding for the P14 stress harness.

``otel_setup.initialise()`` is the single entry point.  It is a no-op
when ``LM_CHAT_OTEL_ENABLED`` is not ``"true"`` so the production app
path stays unchanged on a normal run.

The orchestrator (``tests/stress/run_stress.py``) sets that env var to
``"true"`` ONLY when ``--with-tracing`` is passed; Jaeger compose is
stood up on the same flag.
"""
