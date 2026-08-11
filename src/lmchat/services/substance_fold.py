# SPDX-License-Identifier: Apache-2.0
"""Substance-aware fold of ``(content, reasoning_content)`` at chat.end.

Used by :mod:`lmchat.services.streaming_service` to decide what to persist
as ``messages.content`` when an upstream model parks its final answer in
``reasoning_content`` and leaves ``content`` empty or a terse stub — the
bug this closes: a chat bubble that renders blank while the real answer
sits in a collapsed "Thinking…" block (Qwen Bug #1773, DeepSeek-R1
distills, Nemotron Cascade 2 auto-think).

Returns the ``(final_content, final_reasoning)`` pair the caller persists
to ``messages.content`` / ``messages.reasoning_content``.
"""
from __future__ import annotations

import unicodedata
from typing import Final, NamedTuple

# Minimum reasoning length (chars) for a salvage to be worthwhile; below
# this, a blank bubble beats a tiny fragment. Gates the REASONING side
# only — the content side is decided by ``_has_real_answer`` (presence,
# not length).
STUB_CHARS: Final[int] = 240

# Unicode top-level categories that constitute a real answer: Letter,
# Number, Symbol (covers emoji — "✅" is a genuine reply), and Mark.
# Whitespace/Punctuation alone ("...", "—") is answerless and IS where
# the salvage belongs.
_ANSWER_CATEGORIES: Final = ("L", "N", "S", "M")


def _has_real_answer(text: str) -> bool:
    """True if ``text`` contains any letter, number, or symbol (incl. emoji).

    Its presence means the model produced a genuine reply, terse or not, so the
    reasoning belongs in its own collapsed channel and must never be pasted
    into the visible body. Whitespace/punctuation-only text returns ``False``.
    Short-circuits on the first answer-bearing character.
    """
    return any(unicodedata.category(ch)[0] in _ANSWER_CATEGORIES for ch in text)

_SALVAGE_PREFIX: Final[str] = (
    "_(reasoning surfaced because the model produced no final answer)_"
    "\n\n"
)


def substance_fold(
    content: str | None,
    reasoning: str | None,
) -> tuple[str, str | None]:
    """Return ``(final_content, final_reasoning)`` for persistence.

    Rule: keep ``content`` as the body UNLESS it carries no real answer
    (see ``_has_real_answer``) AND reasoning is substantive
    (``len(reasoning) > STUB_CHARS`` AND ``len(reasoning) > 2 *
    max(len(content), 1)``). When the fold fires, reasoning is folded into
    the body with ``_SALVAGE_PREFIX`` and zeroed so the UI doesn't
    double-display.

    A terse-but-real reply ("Done.") is a complete answer and is preserved
    verbatim — the salvage exists only for the parked-answer case (Qwen
    Bug #1773, DeepSeek-R1 distills).

    Strict ``>`` ensures the boundary value at exactly ``STUB_CHARS`` does
    not fold.
    """
    base = (content or "").strip()
    extra = (reasoning or "").strip()

    # Content with any real answer text is kept as-is — never dump reasoning
    # over a genuine reply, however terse (a lone emoji counts).
    base_has_answer = _has_real_answer(base)
    fold = (
        not base_has_answer
        and len(extra) > STUB_CHARS
        and len(extra) > 2 * max(len(base), 1)
    )
    if not fold:
        return content or "", reasoning or None

    if base:
        final_content = base + "\n\n" + _SALVAGE_PREFIX + extra
    else:
        final_content = _SALVAGE_PREFIX + extra
    return final_content, None


# ───────────────────────────────────────────────────────────────────────────
# End-of-stream terminal-content decision (centralised)
#
# substance_fold() above is content-aware but tool-BLIND — it can't tell a
# "parked answer" (DeepSeek-R1 / Qwen #1773) from a "failed tool
# deliberation". This helper adds that missing axis so every terminal site
# (chat.end, tool-loop-cut, /research sub-session) makes the same call.
# ───────────────────────────────────────────────────────────────────────────


class TerminalContent(NamedTuple):
    """The assistant body + reasoning to persist when a stream ends.

    - ``content``: final ``messages.content`` to persist / emit.
    - ``reasoning``: final ``messages.reasoning_content`` — kept in its own
      collapsed "Thinking" channel, never dumped into the visible body except
      in the ``salvaged`` (no-tools parked-answer) case.
    - ``kind``: why this outcome was chosen —
        ``"answer"``        model produced a genuine reply;
        ``"answer_capped"`` genuine reply + a tool-loop-cap note appended;
        ``"graceful"``      tools ran but no answer → actionable message,
                            reasoning preserved in its own channel;
        ``"salvaged"``      no tools, answer parked in reasoning → surfaced
                            via substance_fold (its original purpose);
        ``"empty"``         nothing to show.
    """

    content: str
    reasoning: str | None
    kind: str


def _loop_why(loop_cut_reason: str | None) -> str:
    """Plain-English reason phrase for a tool turn that never answered."""
    if loop_cut_reason == "repeat_loop":
        return "kept repeating the same tool call"
    if loop_cut_reason == "failure_streak":
        return "kept failing on the same tool"
    if loop_cut_reason is not None:
        return "kept calling tools without answering"
    # Natural end (no loop-cut): the model never converged, most often
    # because the tools were erroring or returning nothing usable.
    return "used its tools but never produced a final answer"


def _graceful_no_answer(*, tool_rounds: int, loop_cut_reason: str | None) -> str:
    """User-facing body for a tool-using turn that ended with no real answer.

    Deliberately does NOT surface the raw reasoning chain — a failed tool
    deliberation is not an answer. The reasoning stays in ``reasoning_content``
    (the collapsed "Thinking" block) for anyone who wants to inspect it.
    """
    why = _loop_why(loop_cut_reason)
    if loop_cut_reason is not None:
        return (
            f"The model {why}, so I stopped it after {tool_rounds} rounds. "
            "Try rephrasing your question, or turn off integrations for this chat."
        )
    # Natural end, tools ran, no answer. Don't assert the tools failed —
    # they may have run fine and the model just never converged (and an
    # XML-leaked tool call is recorded as a "failure", so a failure-count
    # signal here would misfire). One honest, non-blaming message covers
    # both causes.
    return (
        f"The model {why}. This can happen if a tool errors or the model "
        "doesn't converge on a reply — try rephrasing, or turn off "
        "integrations for this chat."
    )


def _loop_cap_note(*, tool_rounds: int, loop_cut_reason: str | None) -> str:
    """Note appended when a loop was cut but the model DID leave partial text."""
    why = _loop_why(loop_cut_reason)
    return (
        f"_(Stopped after {tool_rounds} tool calls without a final answer — "
        f"the model {why}. The partial result above is what it gathered. Try "
        "rephrasing, or turn off integrations for this chat.)_"
    )


def resolve_terminal_content(
    content: str | None,
    reasoning: str | None,
    *,
    had_tool_calls: bool,
    tool_rounds: int = 0,
    loop_cut_reason: str | None = None,
) -> TerminalContent:
    """Decide the assistant body to persist when a stream ends.

    The single, tool-aware end-of-stream policy shared by every terminal site:

      1. Real answer present  → keep it verbatim (append a loop-cap note if we
         cut a tool loop mid-answer).
      2. No answer, tools ran → emit an actionable graceful message; NEVER dump
         the raw reasoning as the "answer". Reasoning is preserved in its own
         channel.
      3. No answer, no tools  → the classic parked-answer case; surface the
         reasoning via ``substance_fold`` (its original, intended purpose).
    """
    base = (content or "").strip()

    # 1. Genuine reply (however terse — a lone emoji counts) → keep it.
    if _has_real_answer(base):
        if loop_cut_reason is not None:
            note = _loop_cap_note(tool_rounds=tool_rounds, loop_cut_reason=loop_cut_reason)
            return TerminalContent(
                (content or "") + "\n\n" + note, reasoning or None, "answer_capped"
            )
        return TerminalContent(content or "", reasoning or None, "answer")

    # 2. Tools ran (or were cut) but nothing answerable landed — a failed
    #    deliberation is not an answer; surface an actionable message and
    #    keep reasoning in its own channel.
    if had_tool_calls or loop_cut_reason is not None:
        return TerminalContent(
            _graceful_no_answer(tool_rounds=tool_rounds, loop_cut_reason=loop_cut_reason),
            reasoning or None,
            "graceful",
        )

    # 3. No tools, empty body — the parked-answer case substance_fold exists for.
    folded_content, folded_reasoning = substance_fold(content, reasoning)
    kind = "salvaged" if folded_content != (content or "") else "empty"
    return TerminalContent(folded_content, folded_reasoning, kind)
