# SPDX-License-Identifier: Apache-2.0
"""OpenAICompatProvider — ChatProvider for any OpenAI-chat-completions-compatible backend.

Part of the multi-provider / MCP foundation.

Supports any provider that speaks the OpenAI /v1/chat/completions SSE protocol:
OpenAI, OpenRouter, Groq, or any other OAI-compat backend.

Context mode: "replay" — full turn history is encoded into every request via
encode_compat / assemble_compat_messages.

LM-Studio-specific and OAI-incompatible fields are stripped by
sanitize_request_for_provider before encoding.

Tools (native MCP): the ``tools`` parameter is accepted but
not forwarded — native MCP over WebSocket is handled elsewhere; the
compat tool-loop driven by encode_compat (via req.tools) is handled by the
compat encoder when req.tools is set on the sanitized request.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from lmchat.lmstudio.compat import decode_compat, encode_compat
from lmchat.lmstudio.types import CanonicalEvent
from lmchat.logging import get_logger
from lmchat.providers.base import sanitize_request_for_provider

if TYPE_CHECKING:
    from lmchat.lmstudio.types import (
        CanonicalChatRequest,
        CanonicalMessage,
        CanonicalTool,
    )

log = get_logger(__name__)


def _normalize_base_url(base_url: str) -> str:
    """Strip trailing slashes and, if present, exactly one trailing ``/v1`` segment.

    Providers document their base URL inconsistently: OpenRouter's own docs
    show ``https://openrouter.ai/api/v1`` (already including the OpenAI-style
    ``/v1`` version segment), while other providers — and this class's own
    URL-construction call sites below — expect just the root, e.g.
    ``https://api.openai.com``, and unconditionally append ``/v1/...``
    themselves.  An un-normalized ``.../api/v1`` base_url therefore produces
    a doubled ``.../api/v1/v1/...`` path, which 404s upstream for
    OpenRouter.

    Normalizing here, at construction, means BOTH equivalent forms converge
    on the same effective URL regardless of which one an admin saved, and
    fixes an already-saved bad row immediately without a DB migration.

    Idempotent: normalizing an already-normalized value is a no-op (it no
    longer ends in ``/v1`` after the first pass), so calling this more than
    once on the same value can never double-strip.
    """
    stripped = base_url.rstrip("/")
    if stripped.endswith("/v1"):
        stripped = stripped[: -len("/v1")]
    return stripped


class OpenAICompatProvider:
    """ChatProvider for OpenAI-chat-completions-compatible backends.

    Satisfies the ChatProvider Protocol structurally (duck-typed); does NOT
    inherit the Protocol class directly.

    Args:
        name:          Short provider identifier, e.g. ``"openai"``,
                       ``"openrouter"``, ``"groq"``.
        base_url:      Root URL of the provider, e.g.
                       ``"https://api.openai.com"``.  Trailing slashes and a
                       single trailing ``/v1`` segment are stripped (see
                       :func:`_normalize_base_url`) so a base_url saved as
                       either ``".../api"`` or ``".../api/v1"`` converge on
                       the same effective URL.  The provider always POSTs to
                       ``{base_url}/v1/chat/completions``.
        api_key:       Bearer token for the Authorization header.  ``None``
                       omits the header (useful for no-auth local backends).
        http_client:   Shared :class:`httpx.AsyncClient` injected by the
                       caller (lifespan / DI).  The provider does NOT own the
                       client lifecycle.
        extra_headers: Optional dict of additional HTTP headers merged into
                       every request (e.g. ``{"X-Title": "LMChat"}`` for
                       OpenRouter).
        inherit_shared_client_auth: When ``False`` (default), a request with
                       no ``api_key`` of its own explicitly sends an empty
                       ``Authorization`` override so it can NEVER silently
                       inherit a DIFFERENT provider's bearer token that may
                       be set as a default header on a shared
                       ``http_client`` (the lifespan-shared client is scoped
                       to LM Studio's own key — see ``app.py``).  Set
                       ``True`` only for the one legitimate case where this
                       class is presenting a provider's OWN shared
                       connection back to itself (see
                       ``LmstudioAdapter.as_openai_compat_provider``).
    """

    #: ChatProvider protocol attribute — stream dispatch mode.
    context_mode: str = "replay"

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        http_client: httpx.AsyncClient,
        extra_headers: dict[str, str] | None = None,
        inherit_shared_client_auth: bool = False,
    ) -> None:
        self.name = name
        self._base_url = _normalize_base_url(base_url)
        self._api_key = api_key
        self._http_client = http_client
        self._extra_headers = extra_headers or {}
        self._inherit_shared_client_auth = inherit_shared_client_auth

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        """Return the Authorization header dict, or an empty dict if no key.

        Returns:
            ``{"Authorization": "Bearer <key>"}`` when an API key is set,
            otherwise ``{}``.
        """
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _request_headers(self, *, content_type: str | None = None) -> dict[str, str]:
        """Build the outbound header dict for a request to this provider.

        Merges :meth:`auth_headers` and ``extra_headers``.  The shared
        ``http_client`` injected at construction may be scoped to a
        DIFFERENT provider with its own default ``Authorization`` header
        (e.g. the lifespan-shared client is scoped to LM Studio's own
        bearer key — see ``app.py``).  httpx merges per-request headers
        over the client's defaults by header name: an explicit per-request
        value replaces the client default, but an ABSENT key lets the
        default flow through unmodified.  So when neither
        :meth:`auth_headers` nor ``extra_headers`` supplies an
        ``Authorization`` value, an explicit empty override is added here
        — UNLESS this provider was built with
        ``inherit_shared_client_auth=True`` (the one legitimate case:
        :meth:`~lmchat.services.lmstudio_adapter.LmstudioAdapter.as_openai_compat_provider`
        presents LM Studio's OWN connection through this class and is
        meant to keep using the shared client's own default auth).

        Args:
            content_type: Optional ``Content-Type`` header value to include
                (``stream_chat`` sends JSON; the GET-based model-list calls
                do not need one).

        Returns:
            The merged header dict for this request.
        """
        headers: dict[str, str] = {**self.auth_headers(), **self._extra_headers}
        if "Authorization" not in headers and not self._inherit_shared_client_auth:
            headers["Authorization"] = ""
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    def set_http_client(self, http_client: httpx.AsyncClient) -> None:
        """Rebind the shared HTTP client used for all requests.

        Called by
        :meth:`~lmchat.services.provider_registry.ProviderRegistry.reconfigure_http_client`
        after a rewire event so that already-built provider instances track the
        new client rather than the old (eventually-closed) one.  The registry
        is the sole caller; application code should not call this directly.

        Args:
            http_client: The new shared :class:`httpx.AsyncClient` created by
                ``rewire_singletons``.
        """
        self._http_client = http_client

    # ------------------------------------------------------------------
    # Detailed model listing (catalog merge — W1)
    # ------------------------------------------------------------------

    async def list_models_detailed(
        self,
    ) -> tuple[list[dict[str, object]], int | None, str | None]:
        """Return rich model metadata from ``{base_url}/v1/models``.

        Returns the full per-model dict (not just model ids) so the catalog
        layer can map fields to
        :class:`~lmchat.services.models_service.Capabilities` and
        ``max_context_length``.

        Returns:
            A 3-tuple ``(items, http_status, error_str)`` where *items* is the
            raw ``data`` array from the provider (possibly ``[]``), *http_status*
            is the HTTP status code (or ``None`` on network failure), and
            *error_str* is a short description on failure (or ``None`` on
            success).

        Callers MUST handle the error tuple and treat a non-None *error_str*
        as an unreachable/auth-failed signal.  A 401/403 response is
        considered an auth failure and must invalidate the caller's cache.
        """
        # The URL is ``{self._base_url}/v1/models`` — admin-configured base_url,
        # not user-controlled.  This call site is safe: the same URL pattern is
        # used by list_models() above (already covered by the SSRF allowlist as
        # the ``url`` local variable built from ``self._base_url``).
        url = f"{self._base_url}/v1/models"
        headers: dict[str, str] = self._request_headers()
        try:
            response = await self._http_client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "openai_compat.list_models_detailed.error",
                provider=self.name,
                url=url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return [], None, str(exc)

        if response.status_code != 200:
            log.warning(
                "openai_compat.list_models_detailed.non_200",
                provider=self.name,
                url=url,
                status_code=response.status_code,
            )
            return [], response.status_code, f"HTTP {response.status_code}"

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "openai_compat.list_models_detailed.json_parse_error",
                provider=self.name,
                url=url,
                error=str(exc),
            )
            return [], response.status_code, f"JSON parse error: {exc}"

        if not isinstance(body, dict):
            return [], response.status_code, "Unexpected response shape (not a dict)"

        data = body.get("data")
        if not isinstance(data, list):
            return [], response.status_code, "Response missing 'data' array"

        items = [item for item in data if isinstance(item, dict)]
        return items, response.status_code, None

    # ------------------------------------------------------------------
    # ChatProvider.stream_chat
    # ------------------------------------------------------------------

    async def stream_chat(  # type: ignore[override]
        self,
        request: CanonicalChatRequest,
        *,
        history: list[CanonicalMessage] | None,
        tools: list[CanonicalTool] | None = None,
        cumulative_tool_rounds: int = 0,
    ) -> AsyncIterator[CanonicalEvent]:
        """Stream one chat turn and yield CanonicalEvents.

        Steps:
        1. Strip LM-Studio-specific / OAI-incompatible fields via
           ``sanitize_request_for_provider``.
        2. Encode the full-replay messages body via ``encode_compat``.
        3. POST to ``{base_url}/v1/chat/completions`` with auth + extra headers.
        4. On non-200: parse the OpenAI error envelope and yield one error event.
        5. On 200: pipe the SSE stream through ``decode_compat`` and yield
           each CanonicalEvent as it arrives.

        The ``tools`` parameter is accepted for interface compatibility
        (Workstream B will wire native MCP over WebSocket separately);
        non-empty values do not cause failures.

        Args:
            request:                Canonical chat request from the SPA.
            history:                Prior turns for replay-mode context
                                    assembly.  ``None`` is treated as an empty
                                    list (no prior context).
            tools:                  Native-MCP tool definitions (Workstream B);
                                    accepted but not forwarded in this revision.
            cumulative_tool_rounds: Agentic loop round counter; accepted for
                                    interface compatibility, not used here.

        Yields:
            :class:`~lmchat.lmstudio.types.CanonicalEvent` instances in wire
            order.
        """
        url = f"{self._base_url}/v1/chat/completions"

        # 1. Strip LM-Studio-specific / incompatible sampler fields.
        sanitized = sanitize_request_for_provider(request, context_mode="replay")

        # 2. Encode the full-replay body.
        resolved_history: list[CanonicalMessage] = history or []
        body = encode_compat(sanitized, resolved_history)

        # 3. Build headers.
        headers: dict[str, str] = self._request_headers(content_type="application/json")

        log.info(
            "openai_compat.stream_chat.start",
            provider=self.name,
            model=sanitized.model,
            url=url,
            history_len=len(resolved_history),
        )

        # 4+5. POST with streaming; handle non-200 inline.
        try:
            async with self._http_client.stream(
                "POST",
                url,
                content=json.dumps(body),
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    raw = await response.aread()
                    error_text = raw.decode("utf-8", errors="replace")

                    log.error(
                        "openai_compat.stream_chat.non_200",
                        provider=self.name,
                        model=sanitized.model,
                        status_code=response.status_code,
                        body_snippet=error_text[:500],
                    )

                    # Parse the OpenAI error envelope: {"error": {"type": ..., "message": ...}}
                    code = str(response.status_code)
                    message = error_text[:500]
                    try:
                        err_json = json.loads(raw)
                        inner = err_json.get("error") if isinstance(err_json, dict) else None
                        if isinstance(inner, dict):
                            code = inner.get("type") or code
                            message = inner.get("message") or message
                    except (json.JSONDecodeError, AttributeError):
                        pass

                    yield CanonicalEvent(
                        type="error",
                        error={"code": code, "message": message},
                    )
                    return

                # 200 — pipe SSE stream through the compat decoder.
                async for ev in decode_compat(response):
                    yield ev

        except httpx.ConnectError as exc:
            log.error(
                "openai_compat.stream_chat.connect_error",
                provider=self.name,
                model=sanitized.model,
                error=str(exc),
            )
            yield CanonicalEvent(
                type="error",
                error={
                    "code": "upstream_unavailable",
                    "message": f"Connection to {self.name} failed: {exc}",
                },
            )
        except httpx.ReadTimeout as exc:
            log.error(
                "openai_compat.stream_chat.read_timeout",
                provider=self.name,
                model=sanitized.model,
                error=str(exc),
            )
            yield CanonicalEvent(
                type="error",
                error={
                    "code": "upstream_unavailable",
                    "message": f"{self.name} read timeout: {exc}",
                },
            )
        except httpx.HTTPError as exc:
            log.error(
                "openai_compat.stream_chat.http_error",
                provider=self.name,
                model=sanitized.model,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            yield CanonicalEvent(
                type="error",
                error={
                    "code": "upstream_unavailable",
                    "message": f"{self.name} network error ({type(exc).__name__}): {exc}",
                },
            )
