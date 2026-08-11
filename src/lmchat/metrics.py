# SPDX-License-Identifier: Apache-2.0
"""Prometheus metric definitions for lm-chat.

This module declares the contract metrics. Each is registered against the default
global registry the moment this module is imported, so
``prometheus_client.generate_latest()`` produces the expected
exposition format without any explicit registration step.

Contract:

- ``lm_chat_requests_total{method, path, status}`` — Counter.
- ``lm_chat_request_latency_seconds{method, path}`` — Histogram
  (default buckets).
- ``lm_chat_streams_completed_total`` — Counter.
- ``lm_chat_streams_failed_total{reason}`` — Counter with
  closed-enum reason label.
- ``lm_chat_streams_active`` — Gauge.
- ``lm_chat_memory_distillations_total`` — Counter,
  insight-distillation events only.
- ``lm_chat_memory_reindexed_total`` — Counter, one increment
  per message processed during ``/api/memory/reindex``.

Additional metrics (e.g. streaming reasons, panel_runs) are added in
their own modules — this module is the baseline set.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT: Counter = Counter(
    "lm_chat_requests_total",
    "Count of HTTP requests handled by the lm-chat backend.",
    labelnames=("method", "path", "status"),
)

REQUEST_LATENCY: Histogram = Histogram(
    "lm_chat_request_latency_seconds",
    "HTTP request latency in seconds, by method and route template.",
    labelnames=("method", "path"),
)

STREAMS_COMPLETED: Counter = Counter(
    "lm_chat_streams_completed_total",
    "Count of streaming chat responses that completed successfully.",
)

STREAMS_FAILED: Counter = Counter(
    "lm_chat_streams_failed_total",
    "Count of streaming chat responses that failed, by reason.",
    labelnames=("reason",),
)

STREAMS_ACTIVE: Gauge = Gauge(
    "lm_chat_streams_active",
    "Number of streaming chat responses currently in progress.",
)

MEMORY_DISTILLATIONS: Counter = Counter(
    "lm_chat_memory_distillations_total",
    "Count of memory-insight distillation events.",
)

MEMORY_REINDEXED: Counter = Counter(
    "lm_chat_memory_reindexed_total",
    "Count of messages processed during /api/memory/reindex runs.",
)

PARAMS_REJECTED: Counter = Counter(
    "lm_chat_params_rejected_total",
    "Count of per-model rejected-param cache entries learned (reactive + proactive).",
    labelnames=("model_id",),
)

# Count model rows dropped during _probe_upstream.
# Labels: reason=validation_error | unknown.
MODELS_PROBE_DROPPED: Counter = Counter(
    "lm_chat_models_probe_dropped_total",
    "Count of model rows dropped during the LM Studio capability probe due to parse errors.",
    labelnames=("reason",),
)

# Content-starvation salvage counter.
# Incremented when accumulated_content is empty but accumulated_reasoning is
# present at chat.end; the reasoning is promoted to content so the user sees
# a non-blank assistant message.
#
# Anticipated future labels (pick from this set when adding new salvage paths):
#   "content_empty_reasoning_present"  — this counter
#   "tool_consumed_no_final_text"      — candidate
#   "token_budget_exhausted_mid_turn"  — candidate
#   "upstream_timeout_before_output"   — candidate
STREAMS_SALVAGED: Counter = Counter(
    "lmchat_streams_salvaged_total",
    "Streams where content was empty and the harness salvaged a usable response.",
    labelnames=("reason",),
)

# Exact-repeat tool-call detection counter.
# Incremented each time an identical tool call (same name + same arguments)
# is detected in the same stream after a prior successful call.
TOOL_CALL_REPEATS_TOTAL: Counter = Counter(
    "lmchat_tool_call_repeats_total",
    "Identical tool calls (same name + same args) made in successive turns of the same stream.",
    labelnames=("tool_name",),
)

# Consecutive failure-streak counter.
# Incremented each time the same tool hits 3+ consecutive failures in a single stream.
TOOL_CALL_FAILURE_STREAKS_TOTAL: Counter = Counter(
    "lmchat_tool_call_failure_streaks_total",
    (
        "Number of times a tool hit a 3+ consecutive failure streak "
        "within a single chat stream."
    ),
    labelnames=("tool_name",),
)

# Structural tool-name validation counter.
# Incremented each time a tool_call.name event carries a structurally invalid name.
TOOL_CALL_NAME_WARNINGS_TOTAL: Counter = Counter(
    "lmchat_tool_call_name_warnings_total",
    (
        "Tool calls where the name failed structural validation "
        "(malformed identifier; likely hallucinated)."
    ),
    labelnames=("kind",),
)
