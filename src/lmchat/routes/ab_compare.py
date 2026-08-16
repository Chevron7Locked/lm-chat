# SPDX-License-Identifier: Apache-2.0
"""A/B compare route for lm-chat.

Endpoint
--------
POST /api/ab/stream
    Form-encoded ``chat_id``, ``message``, ``model_a``, ``model_b``.
    Returns SSE with interleaved events from both models; each event
    carries a ``pane: "a" | "b"`` field to discriminate streams.
    Auth-gated. Auth contract identical to ``/api/chat/stream``.

Event types
-----------
All event types mirror the existing streaming surface types:
``chat.start``, ``message.delta``, ``chat.end``, ``error``.
``ab.error`` and ``ab.end`` are new A/B-specific terminators.
``pane`` is the new discriminator field on every event.

SSE wire format per event::

    event: message.delta
    data: {"type": "message.delta", "pane": "a", "delta": "..."}

"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import StreamingResponse

from lmchat.config import get_settings
from lmchat.logging import get_logger
from lmchat.routes._dependencies import require_user
from lmchat.services._token_budget import approx_token_count
from lmchat.services.ab_compare_service import AbCompareService
from lmchat.services.auth_service import User

log = get_logger(__name__)

router = APIRouter(prefix="/api/ab", tags=["ab-compare"])




def _get_ab_compare_service(request: Request) -> AbCompareService:
    """Return ``app.state.ab_compare_service``; raise ``RuntimeError`` if unset."""
    svc: AbCompareService | None = getattr(request.app.state, "ab_compare_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.ab_compare_service is not set — "
            "AbCompareService must be initialised in the app lifespan."
        )
    return svc


def _sse_frame(event_type: str, data: dict) -> str:  # type: ignore[type-arg]
    """Format one SSE frame.

    Args:
        event_type: SSE ``event:`` field value.
        data:       JSON-serialisable dict for the ``data:`` field.

    Returns:
        Formatted SSE string including the trailing double newline.
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


@router.post("/stream")
async def ab_compare_stream(
    request: Request,
    chat_id: int = Form(...),
    message: str = Form(..., min_length=1, max_length=32768),
    model_a: str = Form(..., min_length=1),
    model_b: str = Form(..., min_length=1),
    max_tokens: int | None = Form(default=None, ge=1),
    user: User = Depends(require_user),
) -> StreamingResponse:
    """Stream responses from two models simultaneously for A/B comparison.

    Accepts a form-encoded body with ``chat_id``, ``message``, ``model_a``,
    ``model_b``, and optional ``max_tokens``.  Returns a Server-Sent Events
    stream with events from BOTH models interleaved; each event carries
    ``pane: "a"`` or ``pane: "b"`` to identify which model produced it.

    Returns 400 if ``max_tokens`` or the estimated prompt-token count exceeds
    the configured A/B compare cap (``LM_CHAT_AB_MAX_OUTPUT_TOKENS``, default
    32 768).  Auth is identical to ``POST /api/chat/stream``.

    SSE wire format per event::

        event: message.delta
        data: {"type": "message.delta", "pane": "a", "delta": "..."}

    Args:
        request:    FastAPI request (for ``app.state.ab_compare_service``).
        chat_id:    Chat PK (not used for DB writes in A/B mode; passed for
                    client-side correlation).
        message:    User message text (current turn).
        model_a:    LM Studio model ID for pane A.
        model_b:    LM Studio model ID for pane B.
        max_tokens: Optional per-pane output-token limit.  Must not exceed
                    ``LM_CHAT_AB_MAX_OUTPUT_TOKENS``.
        user:       Authenticated user (auth-gated).

    Returns:
        ``StreamingResponse`` with ``Content-Type: text/event-stream``.

    Raises:
        HTTPException(400): If max_tokens or the estimated prompt-token count
                            exceeds the configured A/B compare cap.
    """
    svc = _get_ab_compare_service(request)

    # Pre-flight limit check — fires before the SSE response starts so we can
    # return a proper HTTP 400 (not an in-band SSE error event).
    cap = get_settings().lm_chat_ab_max_output_tokens
    if max_tokens is not None and max_tokens > cap:
        raise HTTPException(
            status_code=400,
            detail=(
                "max_tokens per pane exceeds AB compare limit; "
                "reduce or disable A/B compare"
            ),
        )
    prompt_token_estimate = approx_token_count(message)
    if prompt_token_estimate > cap:
        raise HTTPException(
            status_code=400,
            detail=(
                "estimated prompt tokens exceed AB compare limit; "
                "reduce or disable A/B compare"
            ),
        )

    log.info(
        "ab_compare.stream_start",
        user_id=user.id,
        chat_id=chat_id,
        model_a=model_a,
        model_b=model_b,
    )

    # Project system_prompt hoist + RAG augmentation for A/B compare.
    #
    # Composition order (mirrors ``streaming_service.py:829-985``):
    #   [RAG_context][project_prompt][chat_prompt]
    # ``chat_prompt`` is N/A for A/B compare (the route doesn't accept one
    # — the user pings two models with the same message); ``project_prompt``
    # comes from the chat's project (if any); ``RAG_context`` is gated on
    # ``rag_enabled``. A/B compare was previously REPLACING the system
    # prompt with just the RAG block — a project-scoped chat lost its
    # project's instructions silently.
    system_prompt: str | None = None
    _project_prompt = ""
    engine = getattr(request.app.state, "engine", None)
    embedding_client = getattr(request.app.state, "embedding_client", None)
    models_service = getattr(request.app.state, "models_service", None)
    memory_service = getattr(request.app.state, "memory_service", None)
    projects_service = getattr(request.app.state, "projects_service", None)

    # Hoist the project's system_prompt — runs regardless of rag_enabled.
    # Same fault tolerance as ``streaming_service``: a project lookup
    # miss is logged + ignored so the stream still runs.
    if engine is not None and projects_service is not None:
        try:
            from sqlalchemy import select as _select

            from lmchat.db.schema import chats as _chats

            async with engine.connect() as _conn:
                _row = (
                    await _conn.execute(
                        _select(_chats.c.project_id).where(
                            _chats.c.id == chat_id,
                            _chats.c.user_id == user.id,
                        )
                    )
                ).fetchone()
            _chat_project_id = _row.project_id if _row is not None else None
            if _chat_project_id is not None:
                _proj = await projects_service.get(  # type: ignore[attr-defined]
                    user_id=user.id, project_id=_chat_project_id
                )
                if _proj is None:
                    log.warning(
                        "ab_compare.project_lookup_miss",
                        chat_id=chat_id,
                        user_id=user.id,
                        project_id=_chat_project_id,
                    )
                else:
                    _project_prompt = _proj.system_prompt or ""
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ab_compare.project_prompt_hoist_failed",
                chat_id=chat_id,
                user_id=user.id,
                error=str(exc),
            )

    if (
        engine is not None
        and embedding_client is not None
        and models_service is not None
        and memory_service is not None
    ):
        try:
            from lmchat.services.rag_service import (
                augment_prompt as _rag_augment,
            )
            from lmchat.services.rag_service import (
                trim_rag_context_for_model,
            )

            augmented = await _rag_augment(
                chat_id=chat_id,
                user_id=user.id,
                current_message=message,
                engine=engine,
                embedding_client=embedding_client,
                models_service=models_service,
                memory_service=memory_service,
            )
            if augmented.context_block:
                # A/B compare runs two models against the same context. Pick
                # the SMALLER of the two models' LIVE-probed windows so
                # neither pane overflows — no per-model-name table; each
                # window comes from the same provider-agnostic probe every
                # other RAG-budget site uses (ModelsService.
                # get_max_context_length: LM Studio's live loaded_context_
                # length, or a cloud/OpenRouter-shape catalog's own
                # context_length). A pane whose probe is unresolved (0) is
                # excluded from the comparison rather than treated as
                # "smallest" — only compare what's actually known; if
                # NEITHER resolves, trim_rag_context_for_model falls back
                # to its own fixed "window unknown" floor.
                window_a = await models_service.get_max_context_length(model_a)
                window_b = await models_service.get_max_context_length(model_b)
                resolved_windows = [w for w in (window_a, window_b) if w > 0]
                tighter_ctx_window = min(resolved_windows) if resolved_windows else None
                trimmed_block, original_chars, trim_fired = (
                    trim_rag_context_for_model(
                        augmented.context_block, ctx_window=tighter_ctx_window
                    )
                )
                # Compose [RAG][project_prompt]. Mirrors
                # streaming_service.py:957-993 composition order.
                _existing_sys = _project_prompt
                system_prompt = (
                    trimmed_block
                    + ("\n\n" if _existing_sys else "")
                    + _existing_sys
                )
                log.info(
                    "ab_compare.rag_augmented",
                    chat_id=chat_id,
                    user_id=user.id,
                    memory_hits=augmented.memory_hits,
                    doc_hits=augmented.doc_hits,
                    trim_fired=trim_fired,
                    original_chars=original_chars,
                    trimmed_chars=len(trimmed_block),
                    window_a=window_a,
                    window_b=window_b,
                    tighter_ctx_window=tighter_ctx_window,
                    project_prompt_chars=len(_project_prompt),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ab_compare.rag_augment_failed",
                chat_id=chat_id,
                user_id=user.id,
                error=str(exc),
            )

    # If RAG didn't fire but a project_prompt exists, surface it on its own.
    # Without this branch a project-scoped chat with rag_enabled=False would
    # also lose its system prompt — the original bug.
    if system_prompt is None and _project_prompt:
        system_prompt = _project_prompt

    # Resolve each pane's catalog model id to its currently-loaded LM Studio
    # instance wire-id. LM Studio's native endpoint rejects a key that isn't
    # the loaded instance name (model_not_found 404) — without this both panes
    # error out. Mirrors the resolution the single-model streaming path applies
    # (streaming_service._resolve_model_and_integrations_gate) — EXCEPT both
    # panes here are ALWAYS an explicit user pick; A/B compare has no
    # implicit-default concept to silently fall back under. A pane whose
    # model isn't loaded must surface a clear error instead: the resolver's
    # fallback picks "the first loaded LLM", which can easily BE the other
    # pane's model — silently comparing a model against itself.
    resolved_a, resolved_b = model_a, model_b
    if models_service is not None:
        try:
            _ra = await models_service.resolve_to_loaded_or_fallback(model_a)
            _rb = await models_service.resolve_to_loaded_or_fallback(model_b)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ab_compare.model_resolve_failed",
                error=str(exc),
                model_a=model_a,
                model_b=model_b,
            )
        else:
            _pane_errors: list[str] = []
            if _ra.wire_id is None or _ra.substituted:
                _pane_errors.append(f"pane a ({model_a!r})")
            if _rb.wire_id is None or _rb.substituted:
                _pane_errors.append(f"pane b ({model_b!r})")
            if _pane_errors:
                log.warning(
                    "ab_compare.pane_model_unloaded",
                    user_id=user.id,
                    chat_id=chat_id,
                    model_a=model_a,
                    model_b=model_b,
                    resolved_a_substituted=_ra.substituted,
                    resolved_b_substituted=_rb.substituted,
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Model(s) not currently loaded in LM Studio: "
                        + ", ".join(_pane_errors)
                        + ". Load the model(s) in LM Studio and try again."
                    ),
                )
            resolved_a = _ra.wire_id or model_a
            resolved_b = _rb.wire_id or model_b

    async def _generate() -> AsyncGenerator[str]:
        try:
            async for ab_event in svc.stream_both(
                prompt=message,
                model_a=resolved_a,
                model_b=resolved_b,
                system_prompt=system_prompt,
                user_id=user.id,
                max_tokens=max_tokens,
            ):
                data: dict = {  # type: ignore[type-arg]
                    "type": ab_event.event_type,
                    "pane": ab_event.side,
                }
                if ab_event.delta is not None:
                    data["delta"] = ab_event.delta
                if ab_event.reasoning is not None:
                    data["reasoning"] = ab_event.reasoning
                if ab_event.response_id is not None:
                    data["response_id"] = ab_event.response_id
                if ab_event.code is not None:
                    data["code"] = ab_event.code
                if ab_event.message is not None:
                    data["message"] = ab_event.message
                yield _sse_frame(ab_event.event_type, data)
        except Exception as exc:  # noqa: BLE001
            log.warning("ab_compare.stream_error", user_id=user.id, error=str(exc))
            yield _sse_frame(
                "error",
                {"type": "error", "code": "internal_error", "message": str(exc)},
            )

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
