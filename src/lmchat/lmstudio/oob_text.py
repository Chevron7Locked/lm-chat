# SPDX-License-Identifier: Apache-2.0
"""Single source of truth for the ``content`` → ``reasoning_content`` fallback.

A reasoning model's non-streaming ``/v1/chat/completions`` response normally
returns the answer in ``message.content`` — but parks it in
``message.reasoning_content`` (leaving ``content`` empty) whenever the
reasoning phase consumes the token budget or the thinking-disable hint
isn't honored. A caller that reads ``content`` alone then silently
produces empty output.

This module is the ONE place that decides which field carries the text,
so behavior can't drift between callers:

- Callers that need the final TEXT (compaction summary, rolling project
  summary) call :func:`oob_message_text`.
- Callers that need a JSON ARRAY salvage the last valid array from either
  field — ``streaming_service._oob_json_array_with_reasoning_salvage``.
- Callers that need a chat TITLE extract a quoted candidate from the
  reasoning tail — ``chat_service._salvage_title_from_reasoning``.

The streaming path is unaffected: it decodes ``reasoning_content`` as
first-class ``reasoning.delta`` events and salvages via ``substance_fold``.
"""
from __future__ import annotations

from typing import Any


def oob_message_text(message: dict[str, Any]) -> str:
    """Final text from an LM Studio non-streaming message, reasoning-aware.

    Prefers ``content`` (the clean final answer); falls back to
    ``reasoning_content`` when ``content`` is empty or whitespace-only. Model-
    and scenario-agnostic — it keys only off which field is populated, never
    off a specific model id. Never raises; returns ``""`` when neither field
    carries text.

    Args:
        message: The ``choices[0].message`` dict from a non-streaming
                 ``/v1/chat/completions`` response. A missing/None field is
                 treated as empty.

    Returns:
        The stripped final text, or ``""`` when both fields are empty.
    """
    content = str(message.get("content") or "").strip()
    if content:
        return content
    return str(message.get("reasoning_content") or "").strip()
