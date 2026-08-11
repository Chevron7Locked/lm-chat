# SPDX-License-Identifier: Apache-2.0
"""Schemathesis custom hooks for lm-chat DAST (L5).

Load via the ``SCHEMATHESIS_HOOKS`` environment variable::

    SCHEMATHESIS_HOOKS=security.schemathesis.hooks \\
    uv run schemathesis run docs/api/openapi.yaml \\
        ...

Requires Schemathesis 4.19+ (uses ``@schemathesis.hook`` decorator).

Hooks implemented
-----------------
1. **rotate Origin / CSRF** — inject ``Origin: https://evil.example`` on every
   state-mutating request (POST/PUT/PATCH/DELETE) and log if the server does
   NOT reply with 4xx.
2. **oversized bodies** — add a 1 MB + 100 KB body example to every JSON
   endpoint and assert 413.
3. **truncated SSE handshake** — mark streaming-endpoint cases with
   ``Connection: close`` so the HTTP client sees an early FIN.
4. **malformed ``previous_response_id``** — for every request body that
   carries ``previous_response_id``, mutate it to null / non-UUID / expired
   UUID.
5. **wrong-shape ``CanonicalChatRequest``** — for every body that matches
   the CanonicalChatRequest shape, generate missing-model, missing-input,
   and wrong-type variants.

See PLAN v3 §2C.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import schemathesis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STATE_MUTATING_METHODS: frozenset[str] = frozenset(
    {"POST", "PUT", "PATCH", "DELETE"}
)

# A sentinel UUID representing an expired token.
_EXPIRED_UUID: str = "00000000-0000-0000-0000-000000000000"

# 1 MB + 100 KB of padding for oversized-body tests.
_OVERSIZED_PADDING: str = "x" * (1024 * 1024 + 100 * 1024)

# Endpoint paths that are streaming endpoints.
_STREAMING_ENDPOINTS: frozenset[str] = frozenset({
    "/api/chat/stream",
    "/api/chats/{chat_id}/sub-session/stream",
    "/api/chats/{chat_id}/sub-session/finalize",
})

# ---------------------------------------------------------------------------
# Module-level counters for mutation cycling (single-threaded in Schemathesis)
# ---------------------------------------------------------------------------

_prev_resp_mutation_count: dict[str, int] = {}
_ccr_mutation_count: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _is_streaming_endpoint(case: Any) -> bool:
    """Return True if the case targets a known streaming endpoint.

    ``case.path`` holds the OpenAPI template path (e.g.
    ``/api/chats/{chat_id}/sub-session/stream``), so exact membership in
    ``_STREAMING_ENDPOINTS`` is sufficient.
    """
    return case.path in _STREAMING_ENDPOINTS if case.path else False


def _body_has_previous_response_id(body: Any) -> bool:
    """Return True if *body* contains ``previous_response_id``."""
    if isinstance(body, dict):
        return "previous_response_id" in body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            return isinstance(parsed, dict) and "previous_response_id" in parsed
        except (json.JSONDecodeError, TypeError):
            return False
    return False


def _looks_like_canonical_chat_request(body: Any) -> bool:
    """Heuristic: the body dict has a ``model`` key (CanonicalChatRequest)."""
    if isinstance(body, dict):
        return "model" in body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            return isinstance(parsed, dict) and "model" in parsed
        except (json.JSONDecodeError, TypeError):
            return False
    return False


def _ensure_body_is_dict(case: Any) -> dict[str, Any] | None:
    """Return the body as a mutable dict, parsing JSON if needed."""
    if case.body is None:
        return None
    if isinstance(case.body, dict):
        return case.body
    if isinstance(case.body, str):
        try:
            parsed = json.loads(case.body)
            if isinstance(parsed, dict):
                case.body = parsed
                return parsed
        except (json.JSONDecodeError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# before_call — combined handler for hooks 1, 3, 4, 5
# ---------------------------------------------------------------------------


@schemathesis.hook
def before_call(context, case, kwargs) -> None:
    """Combined before_call handler.

    Applies:
    1. Origin / CSRF header injection (state-mutating methods).
    2. Truncated SSE handshake setup (streaming endpoints).
    3. Malformed ``previous_response_id`` mutation.
    4. Wrong-shape ``CanonicalChatRequest`` mutation.
    """
    method = (case.method or "GET").upper()

    # --- 1. Origin / CSRF ---
    if method in _STATE_MUTATING_METHODS:
        if case.headers is None:
            case.headers = {}
        case.headers.setdefault("Origin", "https://evil.example")

    # --- 2. Truncated SSE handshake ---
    if _is_streaming_endpoint(case):
        if case.headers is None:
            case.headers = {}
        case.headers["Connection"] = "close"
        try:
            if hasattr(case, "meta") and case.meta is not None:
                case.meta.timeout = 3.0
        except (AttributeError, TypeError):
            pass

    # --- 3. Malformed previous_response_id ---
    body = _ensure_body_is_dict(case)
    if body is not None and _body_has_previous_response_id(body):
        mutation_key = (
            f"{case.operation}_{case.path}" if case.operation else (case.path or "")
        )
        _prev_resp_mutation_count[mutation_key] = (
            _prev_resp_mutation_count.get(mutation_key, 0) + 1
        )
        variant = _prev_resp_mutation_count[mutation_key] % 3
        if variant == 0:
            body["previous_response_id"] = None
        elif variant == 1:
            body["previous_response_id"] = "not-a-uuid"
        else:
            body["previous_response_id"] = _EXPIRED_UUID

    # --- 4. Wrong-shape CanonicalChatRequest ---
    if body is not None and _looks_like_canonical_chat_request(body):
        mutation_key = (
            f"{case.operation}_{case.path}" if case.operation else (case.path or "")
        )
        _ccr_mutation_count[mutation_key] = (
            _ccr_mutation_count.get(mutation_key, 0) + 1
        )
        variant = _ccr_mutation_count[mutation_key] % 3
        if variant == 0:
            body.pop("model", None)
        elif variant == 1:
            body.pop("input", None)
            body["input"] = None
        else:
            body["model"] = 42


# ---------------------------------------------------------------------------
# after_call — combined handler for hooks 1, 2, 3
# ---------------------------------------------------------------------------


@schemathesis.hook
def after_call(context, case, response) -> None:
    """Combined after_call handler.

    Checks:
    1. CSRF gap detection.
    2. Oversized-body 413 assertion.
    3. Truncated SSE handshake logging.
    """
    method = (case.method or "GET").upper()

    # --- 1. CSRF gap detection ---
    if method in _STATE_MUTATING_METHODS:
        origin = (case.headers or {}).get("Origin")
        if origin == "https://evil.example":
            status = response.status_code
            if status == 200:
                logger.warning(
                    "CSRF-GAP: %s %s returned 200 with Origin:evil — possible CSRF gap",
                    method,
                    case.path,
                )
            elif status >= 500:
                logger.warning(
                    "CSRF-GAP: %s %s returned %d (server error, not 4xx)",
                    method,
                    case.path,
                    status,
                )

    # --- 2. Oversized-body 413 assertion ---
    # An oversized request body must be rejected before it is processed. The
    # canonical signal is 413 (Payload Too Large). Auth-protected endpoints
    # legitimately short-circuit at the authentication layer (401) or the
    # authorization layer (403) BEFORE the body is ever sized — that is a
    # stronger rejection (the oversized payload is never processed at all), so
    # it satisfies the same security goal. Any 2xx (oversized body accepted) or
    # other status is a real gap.
    body = case.body
    if isinstance(body, dict) and body.get("_oversized_payload"):
        if response.status_code not in (413, 401, 403):
            raise AssertionError(
                f"OVERSIZE-GAP: {method} {case.path} returned "
                f"{response.status_code}, expected 413 "
                f"(or 401/403 when the endpoint is auth-gated)"
            )

    # --- 3. Truncated SSE handshake ---
    if _is_streaming_endpoint(case):
        conn = (case.headers or {}).get("Connection", "").lower()
        if conn == "close" and response.status_code == 200:
            logger.warning(
                "SSE-TRUNC-GAP: %s %s returned 200 with Connection:close — "
                "expected 503 or partial content",
                method,
                case.path,
            )


# ---------------------------------------------------------------------------
# before_add_examples — inject oversized-body example (hook 2)
# ---------------------------------------------------------------------------


@schemathesis.hook
def before_add_examples(context, examples) -> None:
    """Inject an oversized-body example for JSON-body endpoints (→ 413).

    Schemathesis 4.20+: ``operation.body`` is a ``PayloadAlternatives``
    (``ParameterSet[OpenApiBody]``); iterate ``body.items`` to find
    ``application/json`` media types.
    """
    operation = context.operation
    if operation is None:
        return

    body_alt = operation.body
    if body_alt is None or not body_alt.items:
        return

    # Find the first JSON body definition.
    json_body = None
    for item in body_alt.items:
        if getattr(item, "media_type", None) == "application/json":
            json_body = item
            break
    if json_body is None:
        return

    # Extract the JSON Schema from the body's definition.
    definition = getattr(json_body, "definition", {})
    schema = definition.get("schema", {}) if isinstance(definition, dict) else {}
    oversized_body = _build_oversized_body(schema)

    # Build a minimal Case so the examples iterator can access .headers etc.
    method = getattr(operation, "method", "POST")
    case = schemathesis.Case(
        operation=operation,
        path=operation.path,
        method=method,  # type: ignore[arg-type]
        body=oversized_body,
    )
    examples.append(case)


def _build_oversized_body(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a dict conforming loosely to *schema* but with 1 MB payload."""
    props = schema.get("properties", {})
    body: dict[str, Any] = {}
    for prop_name, prop_schema in props.items():
        prop_type = prop_schema.get("type", "string")
        if prop_type == "string":
            body[prop_name] = _OVERSIZED_PADDING[:100]
        elif prop_type == "integer":
            body[prop_name] = 0
        elif prop_type == "number":
            body[prop_name] = 0.0
        elif prop_type == "boolean":
            body[prop_name] = False
        elif prop_type == "array":
            body[prop_name] = []
        elif prop_type == "object":
            body[prop_name] = {}
        else:
            body[prop_name] = None
    body["_oversized_payload"] = _OVERSIZED_PADDING
    return body