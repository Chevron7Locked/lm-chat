# SPDX-License-Identifier: Apache-2.0
"""LM Studio adapter — the single integration point between the streaming
service and LM Studio's three surfaces.

This module owns:
- Surface selection (native / compat / responses) — see select_surface().
- Request encoding (delegated to lmstudio.native / lmstudio.compat /
  lmstudio.responses).
- Rejected-param stripping on every outbound request.
- 400 reactive-learn-and-retry loop (record_rejection → strip → retry ONCE).
- Integration-degrade retries (drop the offending ``mcp/<name>`` plugin(s)
  and retry ONCE, rather than dying the whole turn) for two failure shapes:
    - HTTP 403 "permission denied" before any streaming starts
      (``_parse_denied_integrations``).
    - Native mid-stream 200 ``error`` event, "Layer 1" of the
      unserveable-plugin resilience fix: a dead/removed/unreachable MCP
      server (e.g. a stale ``enabled_by_default`` integration) surfaces as
      LM Studio's ``plugin_connection_error`` — degradable ONLY before any
      content-bearing event has been yielded for this attempt
      (``_is_unserveable_plugin_error`` / ``_parse_unserveable_plugins``).
      Both degrade paths rebuild the retry request via the shared
      ``_build_degraded_request`` helper. See ``routes/streaming.py`` for
      "Layer 2", the proactive pre-flight filter that avoids the round-trip
      when LM Studio's live plugin set is known.
- SSE event streaming (delegated to decode_native / decode_compat /
  decode_responses).
- Canonical error surfacing for all failure modes.

Surface selection
-----------------
- ``integrations`` non-empty → ``"native"`` (/api/v1/chat).
- ``tools`` non-empty AND ``integrations`` empty → ``"responses"``
  (/v1/responses).  Replaces ``"compat"`` for the tool-use path.
  /v1/responses supports both stateful chaining AND client-side tool-use.
- Both empty → ``"native"`` (preferred for plain chat).
- ``"compat"`` (/v1/chat/completions) is kept in the codebase for legacy
  use and future paths but is no longer selected by select_surface().

Design constraints
------------------
- The adapter is PURE on (req, history) — it owns no per-conversation state.
  The streaming service owns history retrieval from the DB.
- Events are yielded directly from decode_native/decode_compat/decode_responses
  — no buffering of event DATA. The one exception is control flow, not data:
  a native mid-stream ``error`` event is inspected before it is yielded so an
  eligible plugin_connection_error can be swallowed and retried instead of
  forwarded (see "Layer 1" above) — every other event still flows straight
  through.
- The retry loop fires AT MOST ONCE per request per failure class (record →
  strip → retry; deny → drop → retry; plugin error → drop → retry). A second
  consecutive failure of the same class is terminal; the adapter surfaces it
  as an ``error`` event and closes the stream.
- Network errors (httpx.HTTPError subclasses) are caught and surfaced as
  canonical ``error`` events with code="upstream_unavailable".
- No bare except.  Every exception path is explicit.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from lmchat.providers.base import ContextMode

import httpx

from lmchat.lmstudio.compat import decode_compat, encode_compat
from lmchat.lmstudio.native import _normalize_error_event, decode_native, encode_native
from lmchat.lmstudio.responses import decode_responses, encode_responses
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalMessage,
    CanonicalTool,
)
from lmchat.logging import get_logger
from lmchat.providers.openai_compat import OpenAICompatProvider
from lmchat.services.model_profile import ModelProfile, resolve_profile
from lmchat.services.params_service import ParamsService

MTP_SUSPECT_THRESHOLD = int(os.getenv("LM_CHAT_MTP_SUSPECT_THRESHOLD", "20"))

log = get_logger(__name__)

# Timeout for streaming chat requests.  The connect/write legs are short;
# the read leg is 1800 s (30 min) to match settings.lm_chat_stream_idle_timeout_sec
# — LM Chat is built FOR local models, where slow prompt-processing and long
# generations are EXPECTED, never a fault. This is the transport-level
# counterpart to that app-level idle-stall watcher: httpx's own read timeout
# fires on silence between chunks regardless of what the app-level watcher
# is configured to, so a shorter value here would abort a legitimately-slow
# turn before the app's own graceful upstream_stall handling ever sees it.
# Keep the two in step.
# This is the SINGLE source of truth — app.py imports it for the lifespan-
# scoped AsyncClient construction so timeout changes don't require editing
# two files.
CHAT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=5.0,
    read=1800.0,
    write=10.0,
    pool=5.0,
)

# httpx.AsyncClient connection-pool limits sized for a 50-concurrent-SSE-
# stream stress target. The default of 10 max_connections would exhaust under
# load; explicit Limits sized for 50 concurrent streams + headroom.
CHAT_LIMITS: Final[httpx.Limits] = httpx.Limits(
    max_connections=60,
    max_keepalive_connections=30,
    keepalive_expiry=30.0,
)

# Backwards-compatibility alias for any code that still references the
# pre-fix internal name.
_CHAT_TIMEOUT: Final[httpx.Timeout] = CHAT_TIMEOUT

# LM Studio error codes that trigger the reactive-learn-and-retry loop.
# All three codes carry an ``error.param`` field naming the offending parameter.
# ``invalid_type`` is emitted by /v1/responses for wrong-type params.
# encode_responses() does not currently pass any param that triggers it,
# but the code is here so a future
# change that introduces such a param is caught and cached automatically.
_REJECTION_CODES: Final[frozenset[str]] = frozenset({
    "unrecognized_keys",
    "invalid_param",
    "invalid_type",
})

# Extracts `mcp/<name>` tokens out of an LM-Studio-authored error message.
# Shared by the 403 integration-denial parser and the native mid-stream
# unserveable-plugin parser below — both read plugin ids out of prose LM
# Studio wrote, not out of a structured field.
_MCP_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"mcp/[a-zA-Z0-9_-]+")

# LM Studio's own error-taxonomy code when it cannot resolve a requested
# MCP plugin's server-side handle (removed/renamed/unreachable server —
# e.g. a stale `enabled_by_default` integration for a server that no
# longer exists). The message-pattern fallback in
# _is_unserveable_plugin_error() covers builds that surface the same
# failure under a different or absent code.
_PLUGIN_CONNECTION_ERROR_CODE: Final[str] = "plugin_connection_error"

# Native-surface event types that carry rendered content. Once one of
# these has been yielded for a turn, the bytes are already on the wire —
# a later mid-stream error can no longer be silently retried, only
# surfaced. Mirrors streaming_service._CONTENT_EMITTED_EVENTS (chat.start,
# chat.end, and the prompt_processing.*/model_load.* heartbeats are
# intentionally excluded: none of them render anything to the user).
_NATIVE_CONTENT_EVENT_TYPES: Final[frozenset[str]] = frozenset({
    "message.delta", "message.end",
    "reasoning.delta", "reasoning.end",
    "tool_call.start", "tool_call.name", "tool_call.arguments",
    "tool_call.success", "tool_call.failure",
})


def _make_error_event(*, code: str, message: str, extra: dict | None = None) -> CanonicalEvent:
    """Build a canonical ``error`` event from a code and message string.

    Args:
        code:    Short machine-readable error code (e.g. "upstream_unavailable").
        message: Human-readable detail (e.g. upstream response text, truncated).
        extra:   Optional dict of additional fields to merge into the error payload.

    Returns:
        A :class:`~lmchat.lmstudio.types.CanonicalEvent` of type ``"error"``.
    """
    error_dict = {"code": code, "message": message}
    if extra:
        error_dict.update(extra)
    return CanonicalEvent(type="error", error=error_dict)


def _strip_reasoning(
    history: list[CanonicalMessage],
) -> list[CanonicalMessage]:
    """Return a copy of ``history`` with ``reasoning_content`` cleared on
    every assistant message. Source list is NOT mutated; the persisted
    DB-backed history keeps the field intact — only the SENT copy strips.

    Re-sending prior
    ``reasoning_content`` bytes degrades quality on Qwen / DeepSeek /
    Nemotron thinking templates. Gated by
    :attr:`ModelProfile.strip_reasoning_from_history`.
    """
    return [
        msg.model_copy(update={"reasoning_content": None})
        if msg.role == "assistant"
        else msg
        for msg in history
    ]


def _is_mtp_suspected(
    *,
    status_code: int,
    req: CanonicalChatRequest,
    cumulative_tool_rounds: int,
    mid_stream_error_type: str | None = None,
) -> bool:
    """Return True when MTP misbehavior is a plausible explanation for an error.

    Fires on two conditions (defense-in-depth, not a root-cause claim):
    1. HTTP 500 after many cumulative tool rounds with tools present.
    2. Mid-stream ``tool_format_generation_error`` when integrations are
       non-empty (the original gate only checked ``req.tools``; native
       surface routes tool use via ``integrations``, not ``tools``).

    Args:
        status_code:          HTTP status from the upstream response (0 for
                              mid-stream errors not surfaced as HTTP errors).
        req:                  The canonical request being streamed.
        cumulative_tool_rounds: Rounds accumulated by the caller.
        mid_stream_error_type: LM Studio ``error.type`` from a mid-stream
                              ``error`` event, or None for HTTP-level errors.
    """
    has_tools = bool(req.tools) or bool(req.integrations)
    # HTTP 500 path (existing behavior — now extended to integrations).
    if status_code == 500 and has_tools and cumulative_tool_rounds >= MTP_SUSPECT_THRESHOLD:
        return True
    # Mid-stream path.
    if (
        mid_stream_error_type == "tool_format_generation_error"
        and bool(req.integrations)
        and cumulative_tool_rounds >= MTP_SUSPECT_THRESHOLD
    ):
        return True
    return False


def _parse_rejection(response_body: bytes) -> str | None:
    """Extract the rejected param name from a LM Studio 400 response body.

    LM Studio 400 shape for unrecognised / invalid params:
    ``{"error": {"code": "unrecognized_keys", "param": "min_p", ...}}``

    Args:
        response_body: Raw response bytes from a 400 upstream response.

    Returns:
        The param name string if the response matches the rejection schema,
        or ``None`` if the body doesn't match (e.g. a different 400 reason).
    """
    try:
        obj = json.loads(response_body)
    except json.JSONDecodeError:
        return None

    error_block = obj.get("error")
    if not isinstance(error_block, dict):
        return None

    code = error_block.get("code", "")
    if code not in _REJECTION_CODES:
        return None

    param = error_block.get("param")
    if not isinstance(param, str) or not param:
        return None

    return param


def _parse_denied_integrations(
    raw_body: bytes,
    requested: list[str],
) -> list[str]:
    """Extract the subset of ``requested`` integrations denied by a 403 response.

    LM Studio 403 shape for integration denial (observed pattern):
    ``{"error": {"message": "Permission denied to use plugin mcp/firecrawl",
                  "code": "permission_denied", "param": "integrations"}}``

    Detection is robust:
    - If ``error.param == "integrations"`` AND the message contains a
      plugin-permission pattern, parse named ``mcp/<name>`` tokens from
      the message and return exactly those.
    - If ``error.param == "integrations"`` with no named plugins, return ALL
      ``requested`` integrations (full degrade — the turn proceeds text-only).
    - Otherwise return ``[]`` (not the integration case).

    Args:
        raw_body:  Raw response bytes from a 403 upstream response.
        requested: The integrations list that was sent in the request.

    Returns:
        Subset of ``requested`` that are denied (may be the full list).
        Empty list means no integration denial detected.
    """
    try:
        obj = json.loads(raw_body)
    except json.JSONDecodeError:
        return []

    error_block = obj.get("error")
    if not isinstance(error_block, dict):
        return []

    param = error_block.get("param", "")
    message = error_block.get("message", "")

    # Check if this is an integrations-param 403.
    is_integrations_param = param == "integrations"
    # Also catch permission-denied + plugin patterns even without param match.
    has_plugin_pattern = (
        bool(re.search(r"permission\s+denied", message, re.IGNORECASE))
        and bool(re.search(r"plugin", message, re.IGNORECASE))
    )

    if not is_integrations_param and not has_plugin_pattern:
        return []

    # Extract named mcp/<name> plugins from the message.
    named_plugins: list[str] = _MCP_TOKEN_RE.findall(message)

    if named_plugins:
        # Return only the requested integrations that match named denied plugins.
        denied_set = set(named_plugins)
        return [i for i in requested if i in denied_set]

    # Generic integrations denial with no specific names — drop ALL.
    return list(requested)


def _is_unserveable_plugin_error(*, code: str, message: str) -> bool:
    """True when a canonical error event names an MCP plugin LM Studio
    cannot serve for this turn (removed/renamed/unreachable server).

    Fires on LM Studio's own ``plugin_connection_error`` code, or — as a
    message-pattern fallback for firmware that surfaces the same failure
    under a different or absent code — LM Studio's stable wording for it
    ("Cannot find plugin handle for plugin" / "Unable to get plugin tools
    for"). Intentionally narrow: must not fire on ordinary tool-call
    failures or unrelated upstream errors.

    Args:
        code:    ``event.error["code"]`` from a canonical ``error`` event.
        message: ``event.error["message"]`` from the same event.

    Returns:
        True if this error represents an unserveable MCP plugin.
    """
    if code == _PLUGIN_CONNECTION_ERROR_CODE:
        return True
    low = message.lower()
    return "cannot find plugin handle" in low or "unable to get plugin tools" in low


def _parse_unserveable_plugins(message: str, requested: list[str]) -> list[str]:
    """Extract the subset of ``requested`` integrations named in an
    unserveable-plugin error message.

    Mirrors ``_parse_denied_integrations``'s token parsing (both read
    ``mcp/<name>`` tokens out of prose LM Studio wrote) but is fed from
    the mid-stream 200 ``error`` event path rather than an HTTP 403 body.

    Args:
        message:   The error message text (``event.error["message"]``).
        requested: The integrations list that was sent in the request.

    Returns:
        Subset of ``requested`` named in the message. When the message
        names no specific plugin, returns the FULL ``requested`` list —
        a generic connection failure with no named culprit degrades the
        whole turn to text-only rather than guessing which one is bad.
    """
    named = _MCP_TOKEN_RE.findall(message)
    if named:
        named_set = set(named)
        return [i for i in requested if i in named_set]
    return list(requested)


def _plugin_degrade_warning_event(denied: list[str]) -> CanonicalEvent:
    """Build the non-fatal ``warning`` event for a dropped-plugin degrade.

    Shared by both unserveable-plugin degrade sites in ``stream_chat``
    (the HTTP-error-body form and the native mid-stream form) — same
    wording either way, just a different trigger point.

    Args:
        denied: The ``mcp/<name>`` integration(s) dropped for this retry.

    Returns:
        A canonical ``warning`` event, ``code="adapter.plugin_connection_error_degraded"``.
    """
    return CanonicalEvent(
        type="warning",
        warning={
            "code": "adapter.plugin_connection_error_degraded",
            "message": (
                f"Integration{'s' if len(denied) > 1 else ''} "
                f"{', '.join(sorted(denied))} could not be reached by "
                f"LM Studio. Continuing without "
                f"{'them' if len(denied) > 1 else 'it'}."
            ),
        },
    )


class LmstudioAdapter:
    """Adapter between the canonical chat API and LM Studio's two HTTP surfaces.

    Construct once at lifespan start and attach to ``app.state.lmstudio_adapter``.
    The adapter is stateless across conversations — all per-conversation state
    lives in the streaming service.

    Satisfies the ``ChatProvider`` Protocol structurally
    via duck-typing:
    - ``name = "lmstudio"`` — short provider identifier.
    - ``context_mode = "chain"`` — LM Studio manages history server-side via
      ``previous_response_id``; the ``history`` arg to ``stream_chat`` is
      ignored for the native surface (used only for compat/responses surfaces).

    Args:
        http_client:    Shared ``httpx.AsyncClient`` (auth headers pre-set by
                        the lifespan, e.g. ``Authorization: Bearer <key>``).
        base_url:       LM Studio base URL, e.g. ``"http://localhost:1234"``.
        params_service: Per-model rejected-param cache.
    """

    # ChatProvider structural attributes.
    name: str = "lmstudio"
    context_mode: ContextMode = "chain"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        base_url: str,
        params_service: ParamsService,
    ) -> None:
        """Initialise the adapter.

        Args:
            http_client:    Shared ``httpx.AsyncClient`` with auth headers.
            base_url:       LM Studio base URL.
            params_service: Rejected-param cache service.
        """
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._params_service = params_service

    # ------------------------------------------------------------------
    # Surface selection
    # ------------------------------------------------------------------

    def select_surface(
        self, req: CanonicalChatRequest
    ) -> Literal["native", "compat", "responses"]:
        """Choose between native, compat, and responses surfaces.

        Decision matrix:
        - ``integrations`` non-empty → ``"native"`` (/api/v1/chat).  MCP is
          server-side; LM Studio executes the tool loop.  If ``tools`` is also
          non-empty, integrations wins and a structured-log WARNING is emitted
          (tools will be stripped before the outbound request).
        - ``tools`` non-empty AND ``integrations`` empty → ``"responses"``
          (/v1/responses).  Client-side tool-use via the Responses API, which
          supports both stateful chaining and tool-use simultaneously.  This
          replaces ``"compat"`` for the tool-use path.
        - Both empty → ``"native"`` (preferred for plain chat; richer typed
          output blocks, reasoning, response_id chain support).

        ``"compat"`` (/v1/chat/completions) is retained in the codebase but
        is no longer selected by this method.  It is kept for legacy callers
        and future paths.

        Args:
            req: The canonical request from the SPA.

        Returns:
            ``"native"``, ``"compat"``, or ``"responses"``.
        """
        if req.integrations:
            if req.tools:
                log.warning(
                    "adapter.tools_dropped_for_integrations",
                    tools=[t.name for t in req.tools],
                    model_id=req.model,
                    note=(
                        "Both integrations and tools were set; integrations wins. "
                        "The tools field will be stripped from the native request. "
                        "LM Studio's MCP host is the authoritative tool source."
                    ),
                )
            return "native"

        if req.tools:
            return "responses"

        return "native"


    # ------------------------------------------------------------------
    # Endpoint-mode presentation (openai_compat)
    # ------------------------------------------------------------------

    def as_openai_compat_provider(self) -> OpenAICompatProvider:
        """Return an ``OpenAICompatProvider`` view onto this adapter's live connection.

        Used when the admin has set the LM Studio endpoint mode to
        ``"openai_compat"`` (Settings → Models → LM Studio, or onboarding):
        instead of the native ``/api/v1/chat`` surface, LM Studio traffic
        is dispatched through the SAME replay + agentic-MCP path cloud
        providers already use — MCP tools then run client-side through LM
        Chat's own MCP Store rather than LM Studio's server-side
        ``~/.lmstudio/mcp.json`` host.

        Reads ``_base_url`` and ``_http_client`` live off ``self`` so a
        subsequent ``rewire_singletons`` (which mutates this adapter in
        place) is picked up automatically — no separate rewiring path is
        needed for this view.

        ``api_key=None``: the shared ``_http_client`` already carries LM
        Studio's ``Authorization: Bearer <key>`` as a client default (the
        same thing ``_post_stream`` relies on), so no per-request auth
        header is needed here either.  ``inherit_shared_client_auth=True``
        is required for that to actually take effect: OpenAICompatProvider
        otherwise sends an explicit empty ``Authorization`` override for
        any provider with no api_key of its own, specifically so a cloud
        provider sharing this same client (see ProviderRegistry) can never
        leak LM Studio's bearer token onto its own requests.  This adapter
        IS that shared client's rightful owner, so it opts back in.
        """
        return OpenAICompatProvider(
            name=self.name,
            base_url=self._base_url,
            api_key=None,
            http_client=self._http_client,
            inherit_shared_client_auth=True,
        )

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _native_url(self) -> str:
        """Return the canonical native chat endpoint URL."""
        return f"{self._base_url}/api/v1/chat"

    def _compat_url(self) -> str:
        """Return the canonical compat chat endpoint URL."""
        return f"{self._base_url}/v1/chat/completions"

    def _responses_url(self) -> str:
        """Return the Responses-API endpoint URL.

        Used for client-side tool-use.  Replaces compat for the
        tool-use path; supports both stateful chaining and tool-use.
        """
        return f"{self._base_url}/v1/responses"

    # ------------------------------------------------------------------
    # Internal: single upstream request attempt
    # ------------------------------------------------------------------

    async def _post_stream(
        self,
        url: str,
        body: dict,  # type: ignore[type-arg]
    ) -> httpx.Response:
        """POST ``body`` to ``url`` and return the streaming response.

        The response is opened in streaming mode (``stream=True``).  The
        caller owns the response and must iterate it (and close it) within
        the calling ``async with`` or generator scope.

        The per-request timeout is embedded in the ``Request`` object via
        ``build_request(timeout=...)`` — ``AsyncClient.send`` does not accept
        a ``timeout`` parameter directly.

        Args:
            url:  Full upstream URL.
            body: JSON-serialisable request body dict.

        Returns:
            An open :class:`httpx.Response` in streaming mode.

        Raises:
            httpx.HTTPError: On connection / protocol errors.
        """
        request = self._http_client.build_request(
            "POST", url, json=body, timeout=_CHAT_TIMEOUT
        )
        return await self._http_client.send(request, stream=True)

    # ------------------------------------------------------------------
    # Public: probe_for_error
    # ------------------------------------------------------------------

    async def probe_for_error(self, req: CanonicalChatRequest) -> str | None:
        """Re-issue *req* with ``stream=False`` and extract LM Studio's
        actual error message.

        LM Studio's streaming response collapses upstream failures into a
        bare ``chat.start`` frame followed by a connection close — the
        SSE body contains no error event. When the persist loop detects
        a ``generator_exhausted_without_terminal`` exit, this method
        re-POSTs the same canonical request with streaming disabled so
        the JSON error body becomes available, then maps it to a
        user-facing string. Read-only with respect to LM Studio state.

        Args:
            req: The canonical chat request that just failed mid-stream.

        Returns:
            A user-facing detail string if a meaningful error was
            extracted (suitable for the SSE ``upstream_error`` frame's
            ``detail`` field), or ``None`` if the probe couldn't help —
            in which case the caller surfaces its generic fallback.
        """
        try:
            body = encode_native(req)
            body["stream"] = False
            # The probe wants the
            # error envelope, not a completion. Pre-fix, a 2xx probe
            # meant LM Studio just generated a full reply against an
            # already-struggling instance for nothing — discarded, also
            # potentially storing a server-side conversation entry.
            # Disable storage and cap output at 1 token so the probe is
            # the cheapest call that can still surface an error body.
            body["store"] = False
            body["max_output_tokens"] = 1
            url = self._native_url()
            resp = await self._http_client.post(
                url, json=body, timeout=httpx.Timeout(10.0)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "adapter.probe_for_error_failed",
                model_id=req.model,
                error=str(exc),
            )
            return None

        # 2xx — the probe succeeded; nothing to surface. The original
        # failure was probably transient or LM Studio-internal; the
        # caller's generic fallback covers it. (Per the hedge
        # above, that 2xx response is also now max_output_tokens=1 —
        # at most a 1-token reply, not a full generation.)
        if 200 <= resp.status_code < 300:
            return None

        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return f"LM Studio returned HTTP {resp.status_code}."

        # LM Studio's error shape: {"error": {"message": "...", "type": "...", "code": ...}}
        err = payload.get("error") if isinstance(payload, dict) else None
        msg: str = ""
        if isinstance(err, dict):
            msg = str(err.get("message") or "")

        # The exceed_context_size_error is by far the most common — and
        # the one a user can act on by toggling integrations. Promote it
        # to a clear, actionable message.
        if "exceed_context_size" in msg or "exceeds the available context" in msg:
            return (
                "Request exceeds the model's context size. Try "
                "disabling some MCP integrations in the composer chip "
                "row, or reload the model in LM Studio with a larger "
                "context."
            )

        if msg:
            # Cap to keep the toast readable.
            return f"LM Studio error: {msg[:300]}"

        return f"LM Studio returned HTTP {resp.status_code} with no message."

    # ------------------------------------------------------------------
    # Internal: integration-degrade request rebuild (shared)
    # ------------------------------------------------------------------

    async def _build_degraded_request(
        self,
        *,
        req: CanonicalChatRequest,
        kept_integrations: list[str],
        history: list[CanonicalMessage] | None,
        profile: ModelProfile,
        failure_code: str,
    ) -> tuple[Literal["native", "compat", "responses"], str, dict] | CanonicalEvent:  # type: ignore[type-arg]
        """Rebuild + encode a request with ``kept_integrations`` in place of
        ``req.integrations``, re-selecting the surface and re-encoding.

        Shared by both integration-degrade paths that need the identical
        "drop some integrations, keep the turn going" rebuild: the HTTP 403
        permission-denial retry and the native mid-stream
        ``plugin_connection_error`` retry (see ``stream_chat``).

        Args:
            req:               The original request that failed.
            kept_integrations: ``req.integrations`` with the offending
                               plugin(s) removed (may be empty — a full
                               text-only degrade).
            history:           Prior turns, needed only if the degraded
                               request lands on the responses/compat
                               surface (dropping to no integrations AND
                               no tools can select either of those).
            profile:           The resolved ``ModelProfile`` for
                               ``req.model`` (reasoning-strip gate).
            failure_code:      The canonical error ``code`` to use if this
                               rebuild can't proceed (missing history on
                               responses/compat). Callers pass their own
                               code so the emitted code matches the
                               triggering failure class — e.g. the 403
                               path keeps its pre-existing
                               ``"403_degrade_failed"``, the plugin-error
                               paths use ``"integration_degrade_failed"``.

        Returns:
            ``(surface, url, body)`` ready to POST, or a canonical
            ``error`` event if history is required but wasn't supplied
            (can't retry on the responses/compat surface without it).
        """
        degraded_req = req.model_copy(update={"integrations": kept_integrations})

        # Re-run surface selection for the degraded request.
        degraded_surface = self.select_surface(degraded_req)
        if degraded_surface in ("responses", "compat") and history is None:
            degraded_req = degraded_req.model_copy(
                update={"tools": [], "integrations": kept_integrations}
            )
            degraded_surface = self.select_surface(degraded_req)

        # Re-encode for the degraded request.
        if degraded_surface == "native":
            degraded_url = self._native_url()
            degraded_body = encode_native(degraded_req)
        elif degraded_surface == "responses":
            if history is None:
                return _make_error_event(
                    code=failure_code,
                    message=(
                        "Cannot retry without history on responses surface "
                        "after integration degrade."
                    ),
                )
            degraded_url = self._responses_url()
            sent_history = (
                _strip_reasoning(history)
                if profile.strip_reasoning_from_history
                else history
            )
            degraded_body = encode_responses(degraded_req, sent_history)
        else:
            if history is None:
                return _make_error_event(
                    code=failure_code,
                    message=(
                        "Cannot retry without history on compat surface "
                        "after integration degrade."
                    ),
                )
            degraded_url = self._compat_url()
            sent_history = (
                _strip_reasoning(history)
                if profile.strip_reasoning_from_history
                else history
            )
            degraded_body = encode_compat(degraded_req, sent_history)

        degraded_body = self._params_service.strip_rejected(
            degraded_body, model_id=req.model
        )
        return degraded_surface, degraded_url, degraded_body

    async def _retry_with_dropped_plugins(
        self,
        *,
        req: CanonicalChatRequest,
        denied: list[str],
        history: list[CanonicalMessage] | None,
        profile: ModelProfile,
        original_surface: Literal["native", "compat", "responses"],
    ) -> tuple[Literal["native", "compat", "responses"], httpx.Response] | CanonicalEvent:
        """Rebuild the request with ``denied`` integrations dropped, POST it,
        and return the opened streaming response.

        Shared by both unserveable-plugin degrade sites in ``stream_chat``:
        the HTTP-error-body form (attempt 1 never reaches an SSE body) and
        the native mid-stream 200 ``error``-event form (attempt 1 partially
        streams before failing). Both need the identical rebuild-POST-check
        sequence; only what triggers the call differs.

        Does NOT emit the non-fatal warning event — the caller does that
        (it owns the yield points; this method is a plain coroutine).

        Args:
            req:              The original request that failed.
            denied:           The ``mcp/<name>`` integration(s) to drop.
            history:          Prior turns (passed through to
                              ``_build_degraded_request``).
            profile:          The resolved ``ModelProfile`` for ``req.model``.
            original_surface: The surface the failed attempt used (logging only).

        Returns:
            ``(surface, response)`` with an open, unread 200 response ready
            to stream, or a canonical ``error`` event if the rebuild, the
            retry POST, or the retry itself failed.
        """
        kept = [i for i in (req.integrations or []) if i not in denied]
        built = await self._build_degraded_request(
            req=req,
            kept_integrations=kept,
            history=history,
            profile=profile,
            failure_code="integration_degrade_failed",
        )
        if isinstance(built, CanonicalEvent):
            return built
        degraded_surface, degraded_url, degraded_body = built

        log.warning(
            "adapter.plugin_connection_error_degrading",
            model_id=req.model,
            dropped=sorted(denied),
            kept=kept,
            original_surface=original_surface,
            degraded_surface=degraded_surface,
        )

        try:
            response = await self._post_stream(degraded_url, degraded_body)
        except httpx.ConnectError as exc:
            log.error(
                "adapter.upstream_connect_error_on_plugin_degrade",
                model_id=req.model,
                surface=degraded_surface,
                error=str(exc),
            )
            return _make_error_event(
                code="upstream_unavailable",
                message=f"Connection to LM Studio failed on degrade retry: {exc}",
            )
        except httpx.ReadTimeout as exc:
            log.error(
                "adapter.upstream_read_timeout_on_plugin_degrade",
                model_id=req.model,
                surface=degraded_surface,
                error=str(exc),
            )
            return _make_error_event(
                code="upstream_unavailable",
                message=f"LM Studio read timeout on degrade retry: {exc}",
            )
        except httpx.HTTPError as exc:
            log.error(
                "adapter.upstream_http_error_on_plugin_degrade",
                model_id=req.model,
                surface=degraded_surface,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return _make_error_event(
                code="upstream_unavailable",
                message=(
                    f"LM Studio network error on degrade retry "
                    f"({type(exc).__name__}): {exc}"
                ),
            )

        if response.status_code != 200:
            raw_body_2 = await response.aread()
            await response.aclose()
            err_text_2 = raw_body_2.decode("utf-8", errors="replace")[:500]
            log.error(
                "adapter.upstream_error_on_plugin_degrade_terminal",
                model_id=req.model,
                surface=degraded_surface,
                status_code=response.status_code,
                body_snippet=err_text_2,
            )
            try:
                body_json_2 = json.loads(err_text_2)
                inner_err_2 = (
                    body_json_2.get("error") if isinstance(body_json_2, dict) else None
                )
                if isinstance(inner_err_2, dict):
                    normalized = _normalize_error_event(inner_err_2)
                    return _make_error_event(
                        code=normalized.get("code", str(response.status_code)),
                        message=normalized.get("message", err_text_2),
                    )
            except Exception:  # noqa: BLE001
                pass
            return _make_error_event(
                code=str(response.status_code),
                message=f"Degrade retry also failed: {err_text_2}",
            )

        return degraded_surface, response

    # ------------------------------------------------------------------
    # Public: stream_chat
    # ------------------------------------------------------------------

    async def stream_chat(
        self,
        req: CanonicalChatRequest,
        *,
        history: list[CanonicalMessage] | None = None,
        # Accepted to satisfy the ChatProvider Protocol; LM Studio routes tools
        # via request.integrations (server-side MCP), so this is ignored here.
        # Native-MCP tool execution for cloud providers is Workstream B.
        tools: list[CanonicalTool] | None = None,  # noqa: ARG002
        cumulative_tool_rounds: int = 0,
    ) -> AsyncIterator[CanonicalEvent]:
        """Stream a chat turn to LM Studio and yield canonical events.

        Implements the reactive-learn-and-retry loop:
        1. Select surface (native / compat / responses).
        2. Encode the request body for that surface.
        3. Strip any previously-rejected params via ``params_service.strip_rejected``.
        4. POST to LM Studio.
        5. On HTTP 400 with an ``unrecognized_keys`` / ``invalid_param`` error
           body: call ``params_service.record_rejection``, strip the param, retry
           ONCE.  A second 400 is surfaced as a canonical ``error`` event and the
           stream ends — no further retries.
        6. On HTTP 200: stream events through decode_native, decode_compat, or
           decode_responses depending on the surface selected.
        7. On non-400 HTTP errors: surface as canonical ``error`` event.
        8. On network errors (httpx.HTTPError subclasses): surface as canonical
           ``error`` event with ``code="upstream_unavailable"``.

        Surface selection:
        - ``integrations`` non-empty → native.
        - ``tools`` non-empty, ``integrations`` empty → responses (/v1/responses).
        - Both empty → native.

        The ``"compat"`` surface (/v1/chat/completions) is retained in the
        codebase but is not selected by ``select_surface()``.

        Args:
            req:     The canonical chat request from the SPA.
            history: Prior turns for this conversation. Required for the
                     responses surface (assembled into the Responses-API
                     ``input`` array) and the compat surface.  IGNORED for
                     the native surface (LM Studio handles history server-side
                     via ``previous_response_id``). Defaults to ``None``; the
                     caller (streaming service) only needs to load history
                     from the DB when the responses/compat surface will be
                     selected.

        Yields:
            :class:`~lmchat.lmstudio.types.CanonicalEvent` instances in wire order.

        Raises:
            ValueError: If the responses (or compat) surface is selected but
                ``history`` was not supplied. This is a precondition violation —
                the caller is expected to pass history when tools are present
                and integrations are not.
        """
        surface = self.select_surface(req)
        if surface == "native":
            url = self._native_url()
        elif surface == "responses":
            url = self._responses_url()
        else:
            url = self._compat_url()

        log.info(
            "adapter.stream_chat.start",
            surface=surface,
            model_id=req.model,
            url=url,
        )

        # Strip reasoning_content from sent history when the
        # model's profile says so. Re-sending prior reasoning bytes degrades quality
        # on Qwen / DeepSeek / Nemotron thinking templates; persisted
        # history (DB) keeps the field intact, only the wire copy strips.
        # Native surface is unaffected — LM Studio carries history server-
        # side via previous_response_id, so there's nothing to strip.
        profile = resolve_profile(req.model)

        # --- encode ---
        if surface == "native":
            body = encode_native(req)
        elif surface == "responses":
            if history is None:
                raise ValueError(
                    "responses surface requires history; caller must pass the "
                    "prior turns loaded from the DB"
                )
            sent_history = (
                _strip_reasoning(history)
                if profile.strip_reasoning_from_history
                else history
            )
            body = encode_responses(req, sent_history)
        else:
            if history is None:
                raise ValueError(
                    "compat surface requires history; caller must pass the "
                    "prior turns loaded from the DB"
                )
            sent_history = (
                _strip_reasoning(history)
                if profile.strip_reasoning_from_history
                else history
            )
            body = encode_compat(req, sent_history)

        # --- strip previously-rejected params ---
        body = self._params_service.strip_rejected(body, model_id=req.model)

        # Reactive degrade (resilience fix): guards a single unserveable-
        # plugin retry across BOTH sites that can detect it — the HTTP-
        # error-body form (attempt 1 never reaches an SSE body) and the
        # native mid-stream 200 error-event form below. Shared across sites
        # so a degraded retry that itself fails can't loop.
        _plugin_degrade_attempted = False

        # --- attempt 1 ---
        try:
            response = await self._post_stream(url, body)
        except httpx.ConnectError as exc:
            log.error(
                "adapter.upstream_connect_error",
                model_id=req.model,
                surface=surface,
                error=str(exc),
            )
            yield _make_error_event(
                code="upstream_unavailable",
                message=f"Connection to LM Studio failed: {exc}",
            )
            return
        except httpx.ReadTimeout as exc:
            log.error(
                "adapter.upstream_read_timeout",
                model_id=req.model,
                surface=surface,
                error=str(exc),
            )
            yield _make_error_event(
                code="upstream_unavailable",
                message=f"LM Studio read timeout: {exc}",
            )
            return
        except httpx.HTTPError as exc:
            log.error(
                "adapter.upstream_http_error",
                model_id=req.model,
                surface=surface,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            yield _make_error_event(
                code="upstream_unavailable",
                message=f"LM Studio network error ({type(exc).__name__}): {exc}",
            )
            return

        # --- handle 400 (attempt 1) ---
        if response.status_code == 400:
            raw_body = await response.aread()
            await response.aclose()

            rejected_param = _parse_rejection(raw_body)
            if rejected_param is None:
                # 400 but not a param-rejection we understand.
                err_text = raw_body.decode("utf-8", errors="replace")[:500]
                log.error(
                    "adapter.upstream_400_unhandled",
                    model_id=req.model,
                    surface=surface,
                    body_snippet=err_text,
                )
                # If the body is JSON with an inner error dict, run it
                # through _normalize_error_event so LM Studio error.type values
                # are mapped to stable canonical codes before yielding.
                try:
                    body_json = raw_body and __import__("json").loads(raw_body)
                    inner_err = body_json.get("error") if isinstance(body_json, dict) else None
                    if isinstance(inner_err, dict):
                        normalized = _normalize_error_event(inner_err)
                        yield _make_error_event(
                            code=normalized.get("code", "400"),
                            message=normalized.get("message", err_text),
                        )
                        return
                except Exception:  # noqa: BLE001
                    pass
                yield _make_error_event(code="400", message=err_text)
                return

            # Reactive learn: record + strip + retry once.
            await self._params_service.record_rejection(
                model_id=req.model, param=rejected_param
            )
            log.warning(
                "adapter.param_rejected_retrying",
                model_id=req.model,
                surface=surface,
                param=rejected_param,
            )
            body = self._params_service.strip_rejected(body, model_id=req.model)

            # --- attempt 2 (retry) ---
            try:
                response = await self._post_stream(url, body)
            except httpx.ConnectError as exc:
                log.error(
                    "adapter.upstream_connect_error_on_retry",
                    model_id=req.model,
                    surface=surface,
                    error=str(exc),
                )
                yield _make_error_event(
                    code="upstream_unavailable",
                    message=f"Connection to LM Studio failed on retry: {exc}",
                )
                return
            except httpx.ReadTimeout as exc:
                log.error(
                    "adapter.upstream_read_timeout_on_retry",
                    model_id=req.model,
                    surface=surface,
                    error=str(exc),
                )
                yield _make_error_event(
                    code="upstream_unavailable",
                    message=f"LM Studio read timeout on retry: {exc}",
                )
                return
            except httpx.HTTPError as exc:
                log.error(
                    "adapter.upstream_http_error_on_retry",
                    model_id=req.model,
                    surface=surface,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                yield _make_error_event(
                    code="upstream_unavailable",
                    message=f"LM Studio network error on retry ({type(exc).__name__}): {exc}",
                )
                return

            # --- handle 400 on retry → terminal ---
            if response.status_code == 400:
                raw_body = await response.aread()
                await response.aclose()
                err_text = raw_body.decode("utf-8", errors="replace")[:500]
                log.error(
                    "adapter.upstream_400_on_retry_terminal",
                    model_id=req.model,
                    surface=surface,
                    body_snippet=err_text,
                )
                # Normalize JSON error body from retry 400.
                try:
                    body_json = raw_body and __import__("json").loads(raw_body)
                    inner_err = body_json.get("error") if isinstance(body_json, dict) else None
                    if isinstance(inner_err, dict):
                        normalized = _normalize_error_event(inner_err)
                        yield _make_error_event(
                            code=normalized.get("code", "400"),
                            message=normalized.get("message", err_text),
                        )
                        return
                except Exception:  # noqa: BLE001
                    pass
                yield _make_error_event(
                    code="400",
                    message=f"LM Studio rejected request after retry: {err_text}",
                )
                return

        # --- handle 403 (attempt 1) — integration denied (graceful degrade) ---
        if response.status_code == 403:
            raw_body = await response.aread()
            await response.aclose()

            denied = _parse_denied_integrations(raw_body, req.integrations or [])
            if not denied or not req.integrations:
                # NOT an integration-denied 403 (e.g. bad API key). Fall through
                # to the generic non-200 handler below — do NOT swallow it.
                pass
            else:
                # Build degraded request with denied integrations removed —
                # shared rebuild helper also used by the native mid-stream
                # plugin_connection_error degrade below.
                kept = [i for i in req.integrations if i not in denied]
                built = await self._build_degraded_request(
                    req=req,
                    kept_integrations=kept,
                    history=history,
                    profile=profile,
                    # Preserve the pre-refactor 403 error code — this
                    # rebuild helper is now shared with the plugin-error
                    # degrade paths, which use their own "integration_
                    # degrade_failed" code (see _retry_with_dropped_plugins).
                    failure_code="403_degrade_failed",
                )
                if isinstance(built, CanonicalEvent):
                    yield built
                    return
                degraded_surface, degraded_url, degraded_body = built

                log.warning(
                    "adapter.integration_denied_degrading",
                    model_id=req.model,
                    dropped=sorted(denied),
                    kept=kept,
                    original_surface=surface,
                    degraded_surface=degraded_surface,
                )

                # Emit non-fatal warning.
                yield CanonicalEvent(
                    type="warning",
                    warning={
                        "code": "adapter.integration_denied_degraded",
                        "message": (
                            f"Integration{'s' if len(denied) > 1 else ''} "
                            f"{', '.join(sorted(denied))} denied by LM Studio. "
                            f"Continuing without {'them' if len(denied) > 1 else 'it'}."
                        ),
                    },
                )

                # --- attempt 2 (degraded retry) ---
                try:
                    response = await self._post_stream(degraded_url, degraded_body)
                except httpx.ConnectError as exc:
                    log.error(
                        "adapter.upstream_connect_error_on_degrade",
                        model_id=req.model,
                        surface=degraded_surface,
                        error=str(exc),
                    )
                    yield _make_error_event(
                        code="upstream_unavailable",
                        message=f"Connection to LM Studio failed on degrade retry: {exc}",
                    )
                    return
                except httpx.ReadTimeout as exc:
                    log.error(
                        "adapter.upstream_read_timeout_on_degrade",
                        model_id=req.model,
                        surface=degraded_surface,
                        error=str(exc),
                    )
                    yield _make_error_event(
                        code="upstream_unavailable",
                        message=f"LM Studio read timeout on degrade retry: {exc}",
                    )
                    return
                except httpx.HTTPError as exc:
                    log.error(
                        "adapter.upstream_http_error_on_degrade",
                        model_id=req.model,
                        surface=degraded_surface,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    yield _make_error_event(
                        code="upstream_unavailable",
                        message=(
                            f"LM Studio network error on degrade retry "
                            f"({type(exc).__name__}): {exc}"
                        ),
                    )
                    return

                # --- handle non-200 on degrade retry → terminal ---
                if response.status_code != 200:
                    raw_body_2 = await response.aread()
                    await response.aclose()
                    err_text_2 = raw_body_2.decode("utf-8", errors="replace")[:500]
                    log.error(
                        "adapter.upstream_403_on_degrade_terminal",
                        model_id=req.model,
                        surface=degraded_surface,
                        status_code=response.status_code,
                        body_snippet=err_text_2,
                    )
                    try:
                        body_json_2 = __import__("json").loads(err_text_2)
                        inner_err_2 = (
                            body_json_2.get("error")
                            if isinstance(body_json_2, dict)
                            else None
                        )
                        if isinstance(inner_err_2, dict):
                            normalized = _normalize_error_event(inner_err_2)
                            yield _make_error_event(
                                code=normalized.get("code", str(response.status_code)),
                                message=normalized.get("message", err_text_2),
                            )
                            return
                    except Exception:  # noqa: BLE001
                        pass
                    yield _make_error_event(
                        code=str(response.status_code),
                        message=f"Degrade retry also failed: {err_text_2}",
                    )
                    return

                # --- 200 on degrade retry → stream normally ---
                surface = degraded_surface
                url = degraded_url

        # --- non-200/400/403 errors ---
        if response.status_code != 200:
            raw_body = await response.aread()
            await response.aclose()
            err_text = raw_body.decode("utf-8", errors="replace")[:500]
            log.error(
                "adapter.upstream_non_200",
                model_id=req.model,
                surface=surface,
                status_code=response.status_code,
                body_snippet=err_text,
            )
            # MTP-suspicion: LM Studio returns HTTP 500 mid-conversation on
            # qwen3.6 + multi-token-prediction (MTP) builds after a long
            # tool-call chain. The remedy is to disable MTP in the model's
            # load config. We surface this as a structured error so the
            # frontend can guide users to disable MTP.
            if _is_mtp_suspected(
                status_code=response.status_code,
                req=req,
                cumulative_tool_rounds=cumulative_tool_rounds,
            ):
                yield _make_error_event(
                    code="mtp_suspected",
                    message="Long tool chain — possible MTP misbehavior.",
                    extra={
                        "cumulative_tool_rounds": cumulative_tool_rounds,
                        "hint": (
                            "This model's draft-MTP may be misbehaving on long tool "
                            "chains. If this happens often, try disabling MTP in "
                            "LM Studio's model load config."
                        ),
                    },
                )
                return
            # If the non-200 body is JSON with an inner error dict,
            # run it through _normalize_error_event for stable canonical codes.
            normalized: dict | None = None  # type: ignore[type-arg]
            try:
                body_json = err_text and json.loads(err_text)
                inner_err = body_json.get("error") if isinstance(body_json, dict) else None
                if isinstance(inner_err, dict):
                    normalized = _normalize_error_event(inner_err)
            except Exception:  # noqa: BLE001
                normalized = None

            # Reactive degrade (resilience fix, HTTP-error-body form): the
            # same unserveable-plugin failure the native mid-stream path
            # below degrades can also arrive as a non-streaming HTTP error
            # on attempt 1 — e.g. LM Studio rejecting the plugin before it
            # ever opens the SSE body. Degrade-once, guard shared with the
            # mid-stream site.
            if (
                normalized is not None
                and not _plugin_degrade_attempted
                and surface == "native"
                and req.integrations
                and _is_unserveable_plugin_error(
                    code=str(normalized.get("code", "")),
                    message=str(normalized.get("message", "")),
                )
            ):
                denied = _parse_unserveable_plugins(
                    str(normalized.get("message", "")), req.integrations
                )
                if denied:
                    _plugin_degrade_attempted = True
                    retried = await self._retry_with_dropped_plugins(
                        req=req,
                        denied=denied,
                        history=history,
                        profile=profile,
                        original_surface=surface,
                    )
                    if isinstance(retried, CanonicalEvent):
                        yield retried
                        return
                    degraded_surface, response = retried
                    surface = degraded_surface
                    yield _plugin_degrade_warning_event(denied)
                    # Fall through to "--- 200: stream events ---" below with
                    # the fresh 200 response — do NOT yield the original
                    # terminal error.
                else:
                    yield _make_error_event(
                        code=normalized.get("code", str(response.status_code)),
                        message=normalized.get("message", err_text),
                    )
                    return
            elif normalized is not None:
                yield _make_error_event(
                    code=normalized.get("code", str(response.status_code)),
                    message=normalized.get("message", err_text),
                )
                return
            else:
                yield _make_error_event(
                    code=str(response.status_code),
                    message=err_text,
                )
                return

        # --- 200: stream events ---
        log.info(
            "adapter.stream_chat.streaming",
            surface=surface,
            model_id=req.model,
        )
        # Track whether any error event was yielded.
        _stream_errored = False
        # Native mid-stream plugin_connection_error retry (Layer 1): a dead/
        # removed MCP plugin (e.g. a stale enabled_by_default integration LM
        # Studio no longer serves) otherwise kills the whole turn with no
        # text ever reaching the user. Guarded by the shared
        # _plugin_degrade_attempted flag declared above attempt 1 — degrade-
        # once whether it's THIS site or the HTTP-error-body site that fires.

        while True:
            _content_emitted = False
            _pending_plugin_retry: list[str] | None = None
            try:
                if surface == "native":
                    # Wrap decode_native loop so a malformed wire event
                    # doesn't crash the generator; emit a canonical decode_error.
                    try:
                        async for event in decode_native(response):
                            if event.type in _NATIVE_CONTENT_EVENT_TYPES:
                                _content_emitted = True
                            if event.type == "error":
                                _stream_errored = True
                                err = event.error or {}
                                err_code = str(err.get("code", ""))
                                err_message = str(err.get("message", ""))
                                # Reactive degrade: only safe before any content
                                # has rendered (can't un-send bytes already on
                                # the wire) and only once per turn — see module
                                # docstring "Layer 1".
                                if (
                                    not _content_emitted
                                    and not _plugin_degrade_attempted
                                    and req.integrations
                                    and _is_unserveable_plugin_error(
                                        code=err_code, message=err_message
                                    )
                                ):
                                    denied = _parse_unserveable_plugins(
                                        err_message, req.integrations
                                    )
                                    if denied:
                                        _pending_plugin_retry = denied
                                        break
                                # Check MTP-suspect on mid-stream
                                # tool_format_generation_error (200-stream path).
                                if err_code == "tool_format_generation_error" and _is_mtp_suspected(
                                    status_code=200,
                                    req=req,
                                    cumulative_tool_rounds=cumulative_tool_rounds,
                                    mid_stream_error_type="tool_format_generation_error",
                                ):
                                    log.warning(
                                        "adapter.mtp_suspected_mid_stream",
                                        surface=surface,
                                        model_id=req.model,
                                        cumulative_tool_rounds=cumulative_tool_rounds,
                                    )
                            yield event
                    except Exception as decode_exc:  # noqa: BLE001
                        _stream_errored = True
                        log.error(
                            "adapter.decode_native.unexpected_error",
                            surface=surface,
                            model_id=req.model,
                            error=str(decode_exc),
                            error_type=type(decode_exc).__name__,
                        )
                        yield _make_error_event(
                            code="decode_error",
                            message=(
                                f"Stream decode failed: "
                                f"{type(decode_exc).__name__}: {decode_exc}"
                            ),
                        )
                elif surface == "responses":
                    async for event in decode_responses(response):
                        if event.type == "error":
                            _stream_errored = True
                        yield event
                else:
                    async for event in decode_compat(response):
                        if event.type == "error":
                            _stream_errored = True
                        yield event
            finally:
                await response.aclose()

            if _pending_plugin_retry is None:
                break

            # --- reactive degrade: rebuild + retry once, reusing the same
            #     rebuild/retry helper as the HTTP-error-body degrade below ---
            _plugin_degrade_attempted = True
            _stream_errored = False
            denied = _pending_plugin_retry
            retried = await self._retry_with_dropped_plugins(
                req=req,
                denied=denied,
                history=history,
                profile=profile,
                original_surface=surface,
            )
            if isinstance(retried, CanonicalEvent):
                yield retried
                return
            degraded_surface, response = retried
            yield _plugin_degrade_warning_event(denied)

            # --- 200 on degrade retry → loop back and stream normally ---
            surface = degraded_surface

        # Emit a distinct log entry when stream ends with errors.
        if _stream_errored:
            log.warning(
                "adapter.stream_chat.complete_with_error",
                surface=surface,
                model_id=req.model,
            )
        else:
            log.info(
                "adapter.stream_chat.complete",
                surface=surface,
                model_id=req.model,
            )
