# SPDX-License-Identifier: Apache-2.0
"""Single source of truth for the ``content`` → ``reasoning_content`` fallback.

A reasoning model's non-streaming ``/v1/chat/completions`` response normally
returns the answer in ``message.content`` — but parks it in
``message.reasoning_content`` (leaving ``content`` empty) whenever the
reasoning phase consumes the token budget or the thinking-disable hint
isn't honored. A caller that reads ``content`` alone then silently
produces empty output.

This module is the ONE place that decides which field carries the text, via
one shared primitive, :func:`oob_salvage`, parameterized by an EXTRACTOR:
try ``extract(content)`` first; if that yields nothing, try
``extract(reasoning_content)`` instead. "Yields nothing" is whatever counts
as falsy for the extractor's return type (``""``, ``[]``, ``None``, ...) —
the primitive is generic over ``T`` and never invents its own empty
sentinel, it just returns whatever the second ``extract`` call produced.

This is a strictly BETTER rule than "content empty → fall back": it falls
back whenever *extraction* comes up empty, not merely whenever the raw
field is empty. A prior implementation of the ENUM case below (C3 mode
adoption) used the weaker "field empty" rule directly instead of going
through this primitive — a model that answered "I'm not sure" in ``content``
(non-empty, but no valid token in it) while the real answer sat in
``reasoning_content`` had its mode silently dropped, because the weaker
rule never even looked at ``reasoning_content``. Fixed 2026-08-14 by
routing through :func:`oob_salvage`.

Four current shapes, three of which share ONE extractor across both fields
and fold cleanly into :func:`oob_salvage`:

- **Final TEXT** (compaction summary, rolling project summary):
  :func:`oob_message_text` — extractor is identity (``str.strip``, in
  effect). Kept as its own top-level function (not redefined in terms of
  :func:`oob_salvage`) since it predates the generic primitive and its
  existing callers' behavior must not move.
- **JSON ARRAY** — extractor is "the last valid ``[...]`` array of
  strings" (``streaming_service._last_json_array_of_strings``), applied to
  BOTH fields via ``streaming_service._oob_json_array_with_reasoning_salvage``.
  A reasoning trace can contain draft arrays before the real one, which is
  why "last", not "first", is correct even on the (rare) occasion content
  itself carries more than one candidate array.
- **Bare ENUM value** (C3 mode adoption) — extractor is "the last
  word-boundary-matched WIRE TOKEN" (a distinctive ``mode_``-prefixed
  form, never the bare enum values themselves — some of which are
  ordinary English words), applied to both fields via
  ``streaming_service._last_valid_mode_id`` — see
  ``streaming_service._infer_mode_oob``.
- **Chat TITLE** — deliberately NOT folded into :func:`oob_salvage`.
  ``chat_service._salvage_title_from_reasoning`` extracts the LAST QUOTED
  string from ``reasoning_content`` specifically; the ``content`` field is
  used as-is (whole trimmed text, no quote requirement) when non-empty —
  an asymmetric pair of extractors, not one extractor applied to both
  fields. Running "last quoted string" against ``content`` too would break
  the common case (a clean unquoted title reply has no quotes at all, so
  extraction would come up empty and wrongly fall through to
  ``reasoning_content``). The field-selection stays inline at the call
  site in ``chat_service.generate_title``.

The streaming path is unaffected: it decodes ``reasoning_content`` as
first-class ``reasoning.delta`` events and salvages via ``substance_fold``.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


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


def oob_salvage(message: dict[str, Any], extract: Callable[[str], T]) -> T:
    """Generic content → reasoning_content salvage, parameterized by extractor.

    Tries ``extract(content)`` first; if that result is FALSY (``""``,
    ``[]``, ``None``, or whatever counts as empty for ``T``), tries
    ``extract(reasoning_content)`` instead and returns that — even if it
    is ALSO empty, since this primitive never invents a "nothing found"
    sentinel of its own beyond what ``extract`` itself already returns for
    empty input.

    This is the generalization of :func:`oob_message_text` (whose
    extractor is effectively identity) to any extraction function — see
    the module docstring for the three consumers that fold into this
    shape (JSON array, bare enum token) plus the one that deliberately
    doesn't (chat title, whose two fields need genuinely different
    extractors).

    Args:
        message: The ``choices[0].message`` dict from a non-streaming
                 ``/v1/chat/completions`` response. A missing/None field
                 is treated as an empty string before ``extract`` runs.
        extract: Pure function from raw field text to the caller's result
                 type ``T``. Called with the RAW (unstripped) field value;
                 an extractor that cares about surrounding whitespace
                 strips it itself (matches every current extractor, which
                 either strips or is whitespace-insensitive already).

    Returns:
        Whichever of ``extract(content)`` / ``extract(reasoning_content)``
        first came back truthy, or the second call's result if neither did.
    """
    content = str(message.get("content") or "")
    result = extract(content)
    if result:
        return result
    reasoning = str(message.get("reasoning_content") or "")
    return extract(reasoning)
