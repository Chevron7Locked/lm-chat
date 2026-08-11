# SPDX-License-Identifier: Apache-2.0
"""ChatProvider Protocol — the seam every provider implementation satisfies.

Part of the multi-provider / MCP foundation.

This module is intentionally pure-types + one helper.  No runtime
side-effects, no DB imports, no FastAPI dependencies.  Import freely from
lifespan, services, and tests without cycle risk.

Context modes
-------------
``"chain"``  — LM Studio native: multi-turn context is threaded server-side
               via ``previous_response_id``.  History is NOT replayed on the
               wire; the request is forwarded as-is.

``"replay"`` — OpenAI-compatible cloud (OpenAI, OpenRouter, Groq, …): full
               turn history is encoded into the request on every turn.  LM-
               Studio-specific fields (``store``, ``integrations``,
               ``previous_response_id``) and sampler params that the
               OpenAI-compatible surface rejects (``top_k``, ``min_p``,
               ``repeat_penalty``) must be stripped before sending.

Design points
-------------
- History seam — ``stream_chat`` carries a ``history`` kwarg.
- ``sanitize_request_for_provider`` strips the static replay-incompatible
  fields to avoid 400s.
- ChatProvider migration strategy — concrete ``LmstudioAdapter``
  satisfies this Protocol structurally (duck-typed via
  ``@runtime_checkable``) without inheriting it.

Field note (verified against ``lmstudio/types.py``):
``CanonicalChatRequest`` carries ``store``, ``integrations``, and
``previous_response_id`` which are LM-Studio-specific.  The sampler fields
``top_k`` / ``min_p`` / ``repeat_penalty`` are LM-Studio-accepted but
rejected by OAI-compat surfaces.  ``presence_penalty`` IS accepted by
OpenAI so it is NOT stripped.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Literal, runtime_checkable

from typing_extensions import Protocol

# NOTE: `ChatProvider` is the streaming-DISPATCH seam only (name + context_mode
# + stream_chat). Model listing and auth-header construction are NOT here — they
# live in the catalog/registry layer, where a concrete provider
# (e.g. OpenAICompatProvider) probes its own /v1/models and builds its own
# authed client. Forcing them onto this seam would couple it to models_service
# and the per-provider HTTP client, which the dispatch path doesn't need.
if TYPE_CHECKING:
    from lmchat.lmstudio.types import (
        CanonicalChatRequest,
        CanonicalEvent,
        CanonicalMessage,
        CanonicalTool,
    )

# ---------------------------------------------------------------------------
# Context mode
# ---------------------------------------------------------------------------

ContextMode = Literal["chain", "replay"]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ChatProvider(Protocol):
    """Contract every provider implementation must satisfy.

    Structural (duck-typed) — concrete classes do NOT need to inherit this.
    The ``@runtime_checkable`` decorator allows ``isinstance`` checks for
    test assertions and registry validation.

    Attributes:
        name:         Short provider identifier, e.g. ``"lmstudio"``,
                      ``"openai"``, ``"openrouter"``, ``"groq"``.
        context_mode: ``"chain"`` for LM Studio (server-side history);
                      ``"replay"`` for all cloud / OAI-compat providers.
    """

    name: str
    context_mode: ContextMode

    def stream_chat(
        self,
        request: CanonicalChatRequest,
        /,
        *,
        history: list[CanonicalMessage] | None,
        tools: list[CanonicalTool] | None = None,
        cumulative_tool_rounds: int = 0,
    ) -> AsyncIterator[CanonicalEvent]:
        """Stream one chat turn and yield canonical events.

        Args:
            request:                Canonical request (may include LM-Studio-
                                    specific fields; call
                                    ``sanitize_request_for_provider`` before
                                    forwarding to a replay provider).
            history:                Prior turns for replay-mode context
                                    assembly.  ``None`` in chain mode (LM
                                    Studio handles history server-side).
            tools:                  Native-MCP tool definitions (Workstream B).
                                    ``None`` / empty when no tools configured.
            cumulative_tool_rounds: How many agentic tool rounds have already
                                    completed for this turn (used for loop-
                                    guard / budget accounting).

        Yields:
            :class:`~lmchat.lmstudio.types.CanonicalEvent` instances in wire
            order.  The event shape is provider-neutral; the FE + streaming
            service consume ``CanonicalEvent`` regardless of which provider
            produced it.
        """
        ...


# ---------------------------------------------------------------------------
# Request sanitization helper
# ---------------------------------------------------------------------------


def sanitize_request_for_provider(
    req: CanonicalChatRequest,
    *,
    context_mode: ContextMode,
) -> CanonicalChatRequest:
    """Return a sanitized copy of *req* safe to send to the target provider.

    For ``context_mode="chain"`` (LM Studio): the request is returned
    unchanged — all LM-Studio-specific fields are intentional.

    For ``context_mode="replay"`` (OAI-compat cloud): returns a
    ``model_copy`` that:

    - Sets ``store=None`` — LM Studio stateful retention opt-out flag;
      meaningless and possibly rejected by cloud surfaces.
    - Sets ``integrations=[]`` — LM Studio MCP integration list;
      completely foreign to OAI surfaces.
    - Sets ``previous_response_id=None`` — LM Studio chain pointer; a
      replay provider assembles context from the ``history`` arg instead.
    - Sets ``top_k=None`` — LM Studio / llama.cpp sampler; not in the
      OAI spec, triggers 400 on OpenAI / OpenRouter.
    - Sets ``min_p=None`` — same.
    - Sets ``repeat_penalty=None`` — same.
    - Sets ``max_output_tokens=None`` — LM Studio alias for ``max_tokens``;
      OpenAI/Groq reject the ``max_output_tokens`` key (they use
      ``max_tokens``). ``encode_compat`` emits ``max_output_tokens`` via
      ``_COMPAT_SCALAR_PARAMS``, so it must be stripped here before the
      request reaches any OAI-compat surface.

    Note: ``presence_penalty`` is intentionally kept — it IS part of the
    OpenAI chat-completions spec.

    This is the static per-provider param strip list.

    Args:
        req:          The original ``CanonicalChatRequest``.
        context_mode: The target provider's context mode.

    Returns:
        The original request unchanged (chain mode) or a sanitized copy
        (replay mode).  The returned object is always a
        ``CanonicalChatRequest`` (Pydantic frozen model).
    """
    if context_mode == "chain":
        return req

    # replay: strip LM-Studio-specific + OAI-incompatible sampler fields.
    return req.model_copy(
        update={
            "store": None,
            "integrations": [],
            "previous_response_id": None,
            "top_k": None,
            "min_p": None,
            "repeat_penalty": None,
            "max_output_tokens": None,
        }
    )
