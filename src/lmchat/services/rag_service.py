# SPDX-License-Identifier: Apache-2.0
"""RAG augmentation service for lm-chat.

Composes message-history recall (``memory_service.recall``) and document
retrieval (``retrieval_service.retrieve``) into a context block prepended to
the system message. Called by the streaming service via ``augment_prompt``
when ``settings.rag_enabled == True``.

Ownership: memory_service owns the message-embedding index; documents_service
owns the document-embedding index. This module composes both without
duplicating either.

Pinned insights are injected unconditionally — explicit user preferences, not
semantic recall — while memory recall and document retrieval stay gated by
``rag_enabled``. A surface that returns empty results is simply omitted; no
error is raised.
"""
from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.config import get_settings
from lmchat.db.schema import chats, projects
from lmchat.embedding.client import EmbeddingClient
from lmchat.logging import get_logger
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import ModelsService
from lmchat.services.retrieval_service import retrieve

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


class AugmentedPrompt(BaseModel):
    """Result of ``augment_prompt``.

    Attributes:
        context_block: Formatted context string to prepend to the system
            message; empty if no relevant context was found.
        memory_hits: Number of message-history recalls included.
        doc_hits: Number of document chunk hits included.
        pinned_hits: Number of pinned insights included.
        degraded_surfaces: Stable tags (e.g. "memory_recall", "documents")
            for surfaces whose retrieval raised and was swallowed — never
            for a surface that ran cleanly and simply returned nothing
            (the empty-vs-degraded distinction).
    """

    model_config = ConfigDict(from_attributes=True)

    context_block: str
    memory_hits: int
    doc_hits: int
    pinned_hits: int = 0
    degraded_surfaces: list[str] = []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _resolve_chat_ctx_window(
    *,
    chat_model_id: str | None,
    models_service: ModelsService,
) -> int:
    """Resolve the active model's real context window for RAG-mode selection.

    Reuses :meth:`ModelsService.get_max_context_length` — the same lookup the
    streaming service's integrations budget gate uses. Prefers the chat's
    persisted ``chats.model_id``; falls back to the first loaded LLM instance.

    Fail-soft: any resolution failure returns 0. Callers treat 0 as "context
    window unknown" and skip INLINE eligibility — :func:`compute_rag_threshold`
    degenerates to a 1-token floor when ``ctx_window <= 0``, which would make
    even a near-empty corpus spuriously "fit".

    Args:
        chat_model_id:  ``chats.model_id`` for this chat, or ``None``.
        models_service: For the loaded-model / context-length lookup.

    Returns:
        The resolved context window in tokens, or 0 when unknown.
    """
    try:
        resolved_model_id = chat_model_id
        if not resolved_model_id:
            loaded = await models_service.list_loaded()
            llm_models = [
                m for m in loaded if m.type == "llm" and m.loaded_instance_ids
            ]
            if llm_models:
                resolved_model_id = llm_models[0].key
        if not resolved_model_id:
            return 0
        return await models_service.get_max_context_length(resolved_model_id)
    except Exception:  # noqa: BLE001
        return 0


def _inject_full_text_chunks(
    chunks: list[dict],
    *,
    budget_tokens: int,
    source_fn: Callable[[dict], str],
) -> list[str]:
    """Format chunk dicts as full-text doc_sections within a token budget.

    Appends whole chunks in order until the next would exceed
    ``budget_tokens``. A chunk that alone exceeds the remaining budget is
    token-sliced (via the same ``cl100k_base`` encoder used to chunk it,
    never a raw char slice) and injection stops; later chunks are dropped.

    Args:
        chunks:        Ordered chunk dicts carrying a ``text`` key (from
                       ``get_project_chunks``/``get_document_chunks``
                       called with ``full_text=True``).
        budget_tokens: Token ceiling for this mode's injection — see
                       ``rag_mode_resolver.rag_inject_budget``.
        source_fn:     Formats the trailing ``source: ...`` value for a
                       chunk (everything after ``"source: "``).

    Returns:
        Formatted ``"text\\nsource: ..."`` sections, ready to extend
        ``doc_sections``.
    """
    from lmchat.services.documents_service import _ENCODING  # noqa: PLC0415

    sections: list[str] = []
    used_tokens = 0
    for chunk in chunks:
        remaining = budget_tokens - used_tokens
        if remaining <= 0:
            break
        text = str(chunk["text"])
        chunk_tokens = _ENCODING.encode(text)
        if len(chunk_tokens) <= remaining:
            body = text
            used_tokens += len(chunk_tokens)
        else:
            body = _ENCODING.decode(chunk_tokens[:remaining])
            used_tokens = budget_tokens
        flat_body = body.replace("\n", " ")
        sections.append(f"{flat_body}\nsource: {source_fn(chunk)}")
    return sections


async def augment_prompt(
    *,
    chat_id: int,
    user_id: int,
    current_message: str,
    engine: AsyncEngine,
    embedding_client: EmbeddingClient,
    models_service: ModelsService,
    memory_service: MemoryService,
    top_k: int = 4,
) -> AugmentedPrompt:
    """Build the RAG context block for a chat message.

    Pinned insights are injected unconditionally — explicit user
    preferences, not semantic recall. The semantic-recall surfaces
    (message history + document retrieval) remain gated by ``rag_enabled``.

    Args:
        chat_id:          PK of the chat (for settings lookup).
        user_id:          Owning user PK (for tenant-isolated retrieval).
        current_message:  The user's current message (query for retrieval).
        engine:           Async SQLAlchemy engine.
        embedding_client: Embedding client.
        models_service:   For resolving the embedding model.
        memory_service:   MemoryService for message-history recall.
        top_k:            Number of results per retrieval surface.

    Returns:
        :class:`AugmentedPrompt` with ``context_block`` and hit counts.
    """
    from sqlalchemy import select

    # Fetch chats.project_id too so retrieval can be project-scoped.
    # Defense-in-depth: also gate on chats.user_id == user_id so a caller
    # passing a chat_id it doesn't own can't leak the foreign project_id.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    chats.c.settings, chats.c.project_id, chats.c.model_id
                ).where(
                    chats.c.id == chat_id,
                    chats.c.user_id == user_id,
                )
            )
        ).fetchone()

    if row is None:
        log.warning(
            "rag_service.chat_not_found",
            chat_id=chat_id,
            user_id=user_id,
        )
        return AugmentedPrompt(context_block="", memory_hits=0, doc_hits=0)

    settings: dict = row.settings if row.settings is not None else {}  # type: ignore[assignment]
    chat_project_id: int | None = row.project_id  # type: ignore[assignment]
    chat_model_id: str | None = row.model_id  # type: ignore[assignment]

    # rag_enabled: an explicit per-chat toggle always wins; unset defaults ON
    # when an embedding model is available. Fail-soft: resolver error → False.
    if "rag_enabled" in settings:
        rag_enabled: bool = bool(settings["rag_enabled"])
    else:
        try:
            from lmchat.services.memory_service import (  # noqa: PLC0415
                resolve_active_embedding_model_key,
            )

            await resolve_active_embedding_model_key(
                engine=engine,
                models_service=models_service,
                persist_default=False,
            )
            rag_enabled = True
        except Exception:  # noqa: BLE001
            rag_enabled = False

    # Pinned insights — unconditional, bypasses rag_enabled. Capped by
    # settings.lm_chat_pinned_insights_cap; gated on chat.project_id so
    # insights from other projects don't bleed in.
    degraded_surfaces: list[str] = []

    pinned_sections: list[str] = []
    try:
        pinned = await memory_service.list_pinned(
            user_id=user_id, project_id=chat_project_id
        )
        cap: int = get_settings().lm_chat_pinned_insights_cap
        for insight in pinned[:cap]:
            pinned_sections.append(insight.text)
    except Exception as exc:  # noqa: BLE001
        degraded_surfaces.append("pinned")
        log.warning(
            "rag_service.pinned_list_failed",
            chat_id=chat_id,
            user_id=user_id,
            error=str(exc),
        )

    # Message-history recall (gated by rag_enabled).
    memory_sections: list[str] = []
    if rag_enabled:
        try:
            recalled = await memory_service.recall(
                user_id=user_id,
                query=current_message,
                top_k=top_k,
                project_id=chat_project_id,
            )
            for hit in recalled:
                excerpt = hit.content[:300].replace("\n", " ")
                memory_sections.append(
                    f"{excerpt}\nsource: message:{hit.message_id} | chat:{hit.chat_id}"
                )
        except Exception as exc:  # noqa: BLE001
            degraded_surfaces.append("memory_recall")
            log.warning(
                "rag_service.memory_recall_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(exc),
            )

    # Auto-distilled insight recall — unconditional like pinned insights;
    # looked up by score, not query vector, so no embedder is required.
    insight_sections: list[str] = []
    try:
        scored_insights = await memory_service.recall_insights(
            user_id=user_id,
            top_k=top_k,
            project_id=chat_project_id,
        )
        for ins in scored_insights:
            if ins.pinned:
                # already injected above
                continue
            insight_sections.append(ins.text)
    except Exception as exc:  # noqa: BLE001
        degraded_surfaces.append("insights")
        log.warning(
            "rag_service.recall_insights_failed",
            chat_id=chat_id,
            user_id=user_id,
            error=str(exc),
        )

    # Document retrieval (gated by rag_enabled) — three modes via
    # rag_mode_resolver.resolve_rag_mode(): FOCUSED (pinned doc, bypass
    # retrieval), INLINE (small project corpus, inject all chunks, bypass
    # retrieve()), HYBRID (fall through to retrieve()).
    doc_sections: list[str] = []
    if rag_enabled:
        try:
            from lmchat.services.rag_mode_resolver import (  # noqa: PLC0415
                RagMode,
                rag_inject_budget,
                resolve_rag_mode,
            )

            # Per-project threshold override (projects.rag_threshold); only
            # fetched when the chat is scoped to a project.
            project_rag_threshold: int | None = None
            if chat_project_id is not None:
                async with engine.connect() as conn:
                    threshold_row = (
                        await conn.execute(
                            select(projects.c.rag_threshold).where(
                                projects.c.id == chat_project_id,
                                projects.c.user_id == user_id,
                            )
                        )
                    ).fetchone()
                if threshold_row is not None:
                    project_rag_threshold = threshold_row.rag_threshold

            # ctx_window: the active model's real loaded context window; see
            # _resolve_chat_ctx_window for the fallback chain and the
            # fail-soft "0 == unknown" contract.
            chat_ctx_window = await _resolve_chat_ctx_window(
                chat_model_id=chat_model_id,
                models_service=models_service,
            )

            # Only estimated when ctx_window is known — an unresolved
            # ctx_window must not be treated as a real 1-token threshold;
            # None preserves resolve_rag_mode's "caller doesn't know →
            # HYBRID" fallback.
            project_corpus_tokens: int | None = None
            if chat_project_id is not None and chat_ctx_window > 0:
                from lmchat.services.documents_service import (  # noqa: PLC0415
                    _estimate_project_corpus_tokens,
                )

                project_corpus_tokens = await _estimate_project_corpus_tokens(
                    engine=engine,
                    user_id=user_id,
                    project_id=chat_project_id,
                )

            decision = resolve_rag_mode(
                project_id=chat_project_id,
                chat_settings=settings,
                ctx_window=chat_ctx_window,
                project_corpus_tokens=project_corpus_tokens,
                project_rag_threshold_override=project_rag_threshold,
            )

            if decision.mode == RagMode.FOCUSED:
                # Inject full text of the pinned doc, bounded by
                # rag_inject_budget (no upstream size check, so this is
                # the only backstop).
                from lmchat.services.documents_service import (  # noqa: PLC0415
                    get_document_chunks,
                )

                focused_id = decision.focused_document_id
                if focused_id is not None:
                    chunks = await get_document_chunks(
                        document_id=focused_id,
                        user_id=user_id,
                        engine=engine,
                        full_text=True,
                    )
                    doc_sections.extend(
                        _inject_full_text_chunks(
                            chunks,
                            budget_tokens=rag_inject_budget(chat_ctx_window),
                            source_fn=lambda c: (
                                f"doc:{focused_id} | chunk:{c['ordinal']} | focused"
                            ),
                        )
                    )
            elif decision.mode == RagMode.INLINE:
                # Corpus fits under the threshold; inject it all instead
                # of retrieving. rag_inject_budget below is a defensive
                # re-check, not the primary gate.
                from lmchat.services.documents_service import (  # noqa: PLC0415
                    get_project_chunks,
                )

                if chat_project_id is not None:
                    chunks = await get_project_chunks(
                        project_id=chat_project_id,
                        user_id=user_id,
                        engine=engine,
                        full_text=True,
                    )
                    doc_sections.extend(
                        _inject_full_text_chunks(
                            chunks,
                            budget_tokens=rag_inject_budget(chat_ctx_window),
                            source_fn=lambda c: (
                                f"doc:{c['document_id']} | chunk:{c['ordinal']} | inline"
                            ),
                        )
                    )
            else:
                # HYBRID fall-through — legacy un-projected chats, large
                # corpora, or an unresolved ctx_window.
                doc_hits = await retrieve(
                    query=current_message,
                    user_id=user_id,
                    top_k=top_k,
                    engine=engine,
                    embedding_client=embedding_client,
                    models_service=models_service,
                    project_id=chat_project_id,
                )
                for hit in doc_hits:
                    content_excerpt = hit.content[:500].replace("\n", " ")
                    doc_sections.append(
                        f"{content_excerpt}\n"
                        f"source: doc:{hit.document_id} | "
                        f"chunk:{hit.ordinal} | "
                        f"title: {hit.document_title}"
                    )
        except Exception as exc:  # noqa: BLE001
            degraded_surfaces.append("documents")
            log.warning(
                "rag_service.document_retrieval_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(exc),
            )

    # Reported once regardless of outcome — a fully-degraded turn also
    # has empty *_sections.
    if degraded_surfaces:
        log.warning(
            "rag_service.augment_degraded",
            chat_id=chat_id,
            user_id=user_id,
            degraded_surfaces=degraded_surfaces,
        )

    # Assemble context block.
    if (
        not pinned_sections
        and not memory_sections
        and not doc_sections
        and not insight_sections
    ):
        return AugmentedPrompt(
            context_block="",
            memory_hits=0,
            doc_hits=0,
            pinned_hits=0,
            degraded_surfaces=degraded_surfaces,
        )

    parts: list[str] = []

    if pinned_sections:
        parts.append("## Pinned context\n")
        parts.extend(pinned_sections)
        parts.append("")

    if memory_sections or doc_sections or insight_sections:
        parts.append("## Retrieved context\n")

        if memory_sections:
            parts.append("### Memory (past conversations)\n")
            parts.extend(memory_sections)
            parts.append("")

        if insight_sections:
            parts.append("### Resurfaced insights\n")
            parts.extend(insight_sections)
            parts.append("")

        if doc_sections:
            parts.append("### Documents\n")
            parts.extend(doc_sections)
            parts.append("")

    parts.append("---")

    context_block = "\n".join(parts)

    log.info(
        "rag_service.augmented",
        chat_id=chat_id,
        user_id=user_id,
        pinned_hits=len(pinned_sections),
        memory_hits=len(memory_sections),
        doc_hits=len(doc_sections),
        insight_hits=len(insight_sections),
    )
    return AugmentedPrompt(
        context_block=context_block,
        memory_hits=len(memory_sections),
        doc_hits=len(doc_sections),
        pinned_hits=len(pinned_sections),
        degraded_surfaces=degraded_surfaces,
    )


# ───────────────────────────────────────────────────────────────────────────
# Window-aware context-block trim
# ───────────────────────────────────────────────────────────────────────────


# Conservative chars/token estimate (real prompts run ~3.5-4.0) so the cap
# fires before the model actually overflows; advisory only.
_CHARS_PER_TOKEN: float = 3.0


def compute_rag_budget_chars(model_id: str | None) -> int:
    """Char budget for the RAG context block, derived from the model's window.

    Resolves ``model_id`` via :func:`resolve_profile` (out-of-band import to
    avoid a circular import). Shares ``_RAG_CONTEXT_BUDGET_FRACTION`` — the
    "how much of the window RAG may occupy" ratio — with
    ``rag_mode_resolver.rag_inject_budget`` as the single source of truth
    for occupancy. Returns a char count to compare ``context_block`` length
    against.
    """
    from lmchat.services.model_profile import resolve_profile
    from lmchat.services.rag_mode_resolver import _RAG_CONTEXT_BUDGET_FRACTION

    profile = resolve_profile(model_id)
    return int(profile.context_window * _RAG_CONTEXT_BUDGET_FRACTION * _CHARS_PER_TOKEN)


def trim_rag_context_for_model(
    context_block: str,
    model_id: str | None,
) -> tuple[str, int, bool]:
    """Cap ``context_block`` length to the model's RAG budget.

    Returns ``(trimmed_block, original_chars, trim_fired)``. Keeps the head
    (pinned-context + retrieval headers) intact and truncates the tail — a
    hard char cap, not per-section accounting, since the assembler doesn't
    expose structured input for that. Runs once per chat turn; no
    re-eviction across rounds.
    """
    if not context_block:
        return ("", 0, False)
    budget = compute_rag_budget_chars(model_id)
    original_chars = len(context_block)
    if original_chars <= budget:
        return (context_block, original_chars, False)
    # Tag the truncation point so it's visible in logs rather than
    # silently shortened.
    trimmed = context_block[:budget].rstrip() + (
        "\n\n[…retrieval truncated to fit model window]"
    )
    return (trimmed, original_chars, True)
