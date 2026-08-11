# SPDX-License-Identifier: Apache-2.0
"""Rolling per-project auto-summary (Wave 3 #10).

A per-project digest accumulates understanding of a project's
conversations over time and feeds it back into future chats as ambient
context. This module: gather a bounded slice of a project's recent
conversation content, ask an out-of-band LLM call to fold it (plus the
existing summary, if any) into an UPDATED digest, and persist it on the
``projects`` row (migration 0039).

Mirrors the auto-memory distillation OOB pattern
(``streaming_service._distill_memory_oob`` / ``_safe_distill_memory``):
a direct non-streaming ``/v1/chat/completions`` call on the admin-pinned
background model, total isolation from the triggering turn, and a
fail-soft contract everywhere — this feature must never be able to
break a chat.

Callers
-------
- ``StreamingService._safe_refresh_project_summary`` — fire-and-forget,
  throttled (see :func:`should_refresh`), fired after a completed
  PROJECT-chat turn.
- ``POST /api/projects/{id}/regenerate-summary`` (``routes/projects.py``)
  — synchronous, explicit user action; bypasses the throttle.

Both funnel through :func:`refresh_project_summary`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.schema import chats, messages
from lmchat.lmstudio.oob_text import oob_message_text
from lmchat.logging import get_logger
from lmchat.services.models_service import resolve_background_model_id
from lmchat.services.projects_service import Project, ProjectsService
from lmchat.utils.text_input_policy import DEFAULT_MAX_LENGTH

if TYPE_CHECKING:
    from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient
    from lmchat.services.models_service import ModelsService

log = get_logger(__name__)

# Throttle: the auto-refresh trigger only regenerates once this many NEW
# messages have accumulated in the project since the last regeneration
# (or immediately when no summary exists yet). Keeps a fast back-and-forth
# from firing the OOB call on every single turn.
_REFRESH_EVERY: Final[int] = 6

# Gather bounds — keep the OOB call cheap regardless of project size.
_MAX_MESSAGES: Final[int] = 40
_MAX_CHARS: Final[int] = 6000
_PER_CHAT_CAP: Final[int] = 15

# Generation budget for the summary call. Sized for a reasoning model:
# enough headroom for the reasoning phase (~1-2k tokens) plus the 200-400
# word summary. The prompt + the DEFAULT_MAX_LENGTH cap keep the stored
# summary short regardless of this value.
_SUMMARY_MAX_TOKENS: Final[int] = 4096

# Rolling-summary system prompt. Kept module-level so tests can assert
# against it, same convention as streaming_service._DISTILL_SYSTEM.
_SUMMARY_SYSTEM = (
    "You maintain a rolling summary of an ongoing project, the way a "
    "well-kept README maintains a living digest of a "
    "workspace. You read the existing summary (if any) plus recent "
    "conversation excerpts from the project's chats and write an UPDATED "
    "summary that reflects everything worth remembering.\n\n"
    "Capture: the project's purpose, recurring topics, decisions made, and "
    "durable facts established along the way. Write it as a digest a "
    "newcomer could read to get oriented — NOT a transcript, and NOT a "
    "turn-by-turn narration of the excerpts you were given.\n\n"
    "Keep it to roughly 200-400 words of plain prose (no markdown headers, "
    "no bullet lists). Output ONLY the summary text — no preamble like "
    '"Here is the summary:", no surrounding quotes.'
)


def should_refresh(project: Project, current_message_count: int) -> bool:
    """Should the throttled auto-refresh regenerate *project*'s summary?

    True when no summary exists yet, or when the project has
    accumulated at least ``_REFRESH_EVERY`` new messages since
    ``project.summary_message_watermark`` was last set.
    """
    if not (project.summary or "").strip():
        return True
    return (current_message_count - project.summary_message_watermark) >= _REFRESH_EVERY


async def count_project_messages(
    engine: AsyncEngine, *, user_id: int, project_id: int
) -> int:
    """Total message count across every chat in *project_id*.

    The watermark unit — compared against
    ``project.summary_message_watermark`` by :func:`should_refresh`.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.count())
            .select_from(messages)
            .join(chats, messages.c.chat_id == chats.c.id)
            .where(chats.c.project_id == project_id, chats.c.user_id == user_id)
        )
        return int(result.scalar() or 0)


async def _gather_conversation_lines(
    engine: AsyncEngine, *, user_id: int, project_id: int
) -> list[str]:
    """Collect recent conversation lines across a project's chats, oldest-first.

    Bounded to ``_MAX_MESSAGES`` lines / ``_MAX_CHARS`` characters so the
    OOB summarizer call stays cheap regardless of project size. Chats are
    walked oldest-updated-first, each contributing at most
    ``_PER_CHAT_CAP`` of its own most-recent user/assistant messages; the
    combined list is then trimmed to the most recent slice.
    """
    async with engine.connect() as conn:
        chat_rows = (
            await conn.execute(
                select(chats.c.id)
                .where(chats.c.project_id == project_id, chats.c.user_id == user_id)
                .order_by(chats.c.updated_at.asc())
            )
        ).fetchall()

        lines: list[str] = []
        for chat_row in chat_rows:
            msg_rows = (
                await conn.execute(
                    select(messages.c.role, messages.c.content)
                    .where(
                        messages.c.chat_id == chat_row.id,
                        messages.c.role.in_(("user", "assistant")),
                    )
                    .order_by(messages.c.id.desc())
                    .limit(_PER_CHAT_CAP)
                )
            ).fetchall()
            for row in reversed(msg_rows):  # oldest-first within the chat
                text = (row.content or "").strip()
                if text:
                    lines.append(f"{row.role.capitalize()}: {text}")

    lines = lines[-_MAX_MESSAGES:]
    total_chars = sum(len(line) for line in lines)
    while lines and total_chars > _MAX_CHARS:
        total_chars -= len(lines.pop(0))
    return lines


async def _resolve_summary_model(
    *,
    engine: AsyncEngine,
    models_service: ModelsService | None,
    hint_model_id: str | None,
) -> str | None:
    """Resolve the wire-id the OOB summarizer call should use.

    Prefers the admin-pinned background model (same resolver as
    auto-memory distillation), falling back to *hint_model_id* — the
    triggering turn's chat model, when the caller has one — exactly like
    ``_safe_distill_memory``. The ``POST .../regenerate-summary`` route
    has no such turn, so it calls with ``hint_model_id=None``; in that
    case, an unset background model falls through to any other loaded
    non-coder/embedding LLM rather than ``resolve_background_model_id``'s
    empty-string shortcut (which is only safe when a real chat model is
    always available).

    Returns None when nothing usable is loaded — the caller skips the
    refresh for this cycle (fail-soft, never raises).
    """
    background_model_key = await resolve_background_model_id(
        engine=engine,
        models_service=models_service,
        chat_model_id=hint_model_id or "",
    )
    if not background_model_key.strip():
        if models_service is None:
            return None
        loaded = await models_service.list_loaded()
        eligible = [
            m
            for m in loaded
            if m.type == "llm"
            and m.loaded_instance_ids
            and "coder" not in m.key.lower()
            and "embed" not in m.key.lower()
        ]
        if not eligible:
            return None
        background_model_key = eligible[0].key

    if models_service is None:
        return background_model_key
    resolved = await models_service.resolve_to_loaded_or_fallback(background_model_key)
    return resolved.wire_id


async def _summarize_oob(
    *,
    lm_client: LmstudioStreamingClient,
    model: str,
    existing_summary: str,
    conversation_lines: list[str],
    timeout_sec: float = 45.0,
) -> str:
    """Out-of-band rolling project-summary generation.

    Mirrors :func:`streaming_service._distill_memory_oob`'s direct
    non-streaming ``/v1/chat/completions`` call — isolated from any
    turn, fail-soft (``""`` on any error so the caller treats it as
    "nothing to persist"). Unlike distillation's JSON array, the model
    returns free text: the summary itself.
    """
    from lmchat.services.lmstudio_adapter import LmstudioAdapter  # noqa: PLC0415

    try:
        adapter = lm_client._adapter  # type: ignore[attr-defined]
        if not isinstance(adapter, LmstudioAdapter):
            # Replay / cloud provider path — skip OOB summarization for now.
            return ""

        http_client = adapter._http_client  # type: ignore[attr-defined]
        base_url = adapter._base_url  # type: ignore[attr-defined]
        url = f"{base_url}/v1/chat/completions"

        convo_text = (
            "\n".join(conversation_lines) if conversation_lines else "(no messages yet)"
        )
        existing_block = (
            f"Existing summary:\n{existing_summary.strip()}\n\n"
            if existing_summary.strip()
            else ""
        )
        user_instruction = (
            f"{existing_block}Recent project activity:\n{convo_text}\n\n"
            "Produce the UPDATED rolling summary, following your instructions."
        )
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": user_instruction},
            ],
            "stream": False,
            # Generous budget: reasoning models spend the first ~1-2k tokens
            # on the reasoning phase before emitting the summary, so a tight
            # cap truncates mid-reasoning and leaves content empty (then
            # oob_message_text salvages raw reasoning). The prompt bounds the
            # summary length and the result is capped at DEFAULT_MAX_LENGTH
            # below, so a large max_tokens can't bloat the stored summary.
            "max_tokens": _SUMMARY_MAX_TOKENS,
            "temperature": 0.2,
            "thinking": {"type": "disabled"},
        }

        # ``url`` derives from the admin-configured adapter ``_base_url``
        # (never user-controlled) — same SSRF-exempt outbound path as the
        # distillation / followups OOB calls.
        resp = await http_client.post(url, json=body, timeout=timeout_sec)
        resp.raise_for_status()
        result = resp.json()
        # content, falling back to reasoning_content on reasoning models
        # (shared primitive — see lmstudio/oob_text.py). Previously read
        # content alone, so a reasoning background model that parked the
        # summary in reasoning_content silently produced an empty summary.
        return oob_message_text(result.get("choices", [{}])[0].get("message", {}))
    except Exception as exc:  # noqa: BLE001
        log.warning("project_summary.oob_failed", error=str(exc))
        return ""


async def refresh_project_summary(
    *,
    engine: AsyncEngine,
    projects_service: ProjectsService,
    lm_client: LmstudioStreamingClient,
    models_service: ModelsService | None,
    user_id: int,
    project_id: int,
    hint_model_id: str | None = None,
) -> Project | None:
    """Gather + summarize + persist the rolling summary for *project_id*.

    Fail-soft: never raises. Returns the project unchanged on any
    internal failure (gather error, no loaded model, empty OOB output),
    or None only when *project_id* doesn't exist / isn't owned by
    *user_id* (callers map that to 404).

    Called both by the throttled auto-refresh trigger
    (``StreamingService._safe_refresh_project_summary``, passing
    ``hint_model_id`` from the just-completed turn) and the explicit
    ``POST /api/projects/{id}/regenerate-summary`` route (no throttle —
    an explicit user action always runs).
    """
    project = await projects_service.get(user_id=user_id, project_id=project_id)
    if project is None:
        return None
    try:
        lines = await _gather_conversation_lines(
            engine, user_id=user_id, project_id=project_id
        )
        if not lines and not (project.summary or "").strip():
            # Empty project, nothing generated yet — leave as-is.
            return project

        wire_id = await _resolve_summary_model(
            engine=engine, models_service=models_service, hint_model_id=hint_model_id
        )
        if wire_id is None:
            log.info("project_summary.skipped_no_loaded_model", project_id=project_id)
            return project

        new_summary = await _summarize_oob(
            lm_client=lm_client,
            model=wire_id,
            existing_summary=project.summary or "",
            conversation_lines=lines,
        )
        if not new_summary:
            return project
        new_summary = new_summary[:DEFAULT_MAX_LENGTH]

        message_count = await count_project_messages(
            engine, user_id=user_id, project_id=project_id
        )
        updated = await projects_service.set_summary(
            user_id=user_id,
            project_id=project_id,
            summary=new_summary,
            message_watermark=message_count,
        )
        log.info(
            "project_summary.refreshed",
            project_id=project_id,
            user_id=user_id,
            summary_len=len(new_summary),
            message_count=message_count,
        )
        return updated or project
    except Exception as exc:  # noqa: BLE001
        log.error(
            "project_summary.refresh_failed",
            project_id=project_id,
            user_id=user_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return project
