# SPDX-License-Identifier: Apache-2.0
"""Tolerant tool-call argument parsing for the LM Studio stream.

LMChat's :class:`~lmchat.services.lmstudio_streaming_client._ToolCallAccumulator`
parses each tool call's ``arguments`` string at ``tool_call.success`` /
``tool_call.failure`` time. A bare :func:`json.loads` fails on the dialect
slips real local seats emit — single-quoted keys, code fences, trailing prose
after the JSON, or a tool call truncated mid-emit because thinking ate the
token budget. On any :class:`json.JSONDecodeError` the streaming client raises
:class:`MalformedToolCallError`, emits a canonical ``error`` event, and the
sub-session / chat stream terminates without ever invoking the tool. A small reasoning
model running a multi-round research chain hits this on round 3+ when its
structured-output discipline degrades — for example, LM Studio emitting
``tool_format_generation_error`` after two successful firecrawl tool
calls; the 438-char partial answer the model HAD produced was discarded by
the canonical client even though :mod:`lmchat.routes.chats._sub_session_sse`
holds a salvage path for it.

:func:`coerce_tool_args` is the recovery layer the accumulator calls first.
Order: already-a-dict → strict ``json.loads`` → strip a code fence + trim
trailing prose via a balanced-object scan → flip single→double quotes →
:func:`repair_truncated_json` (close the open string + any open brackets, or
trim back to the last complete ``key:value`` pair and close). Returns
:data:`None` only when every repair fails — caller (the accumulator) keeps
the strict ``MalformedToolCallError`` path for that case, so structural
errors still terminate the stream loudly.

**Provenance.** The core coercer (tolerant tool-call argument parsing + truncation-tolerant
repair) is adapted from qwen-code. :func:`recover_xml_tool_calls` handles a related failure:
Qwen3-Coder-derived reasoning models emit XML tool-call format inside ``message.delta``
content when their template degrades, and LM Studio's native ``/api/v1/chat`` passes it
through as plain text without firing ``tool_call.*`` events. LM Studio's native API does
not always emit JSON tool calls — confirmed by observing raw
``<tool_call><function=list_directory>...</tool_call>`` rendered in the chat as plain text.

**Dialect-general, keyed by nothing.** No ``"qwen" in model`` switches. The
coercer just makes the existing parse forgiving for the union of slips we see
in practice. Per-dialect parsers (vLLM-style) are NOT ported — too brittle.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)
# single-quoted "key": only flip quotes around a key token, never inside a value.
_SQ_KEY_RE = re.compile(r"([{,]\s*)'([^'\"]*?)'(\s*:)")
# single-quoted value: '...'  → "..."  (no embedded double-quote in the value).
_SQ_VAL_RE = re.compile(r"(:\s*)'([^'\"]*?)'(\s*[,}])")


def find_json_object(text: str) -> str | None:
    """Return the first balanced top-level ``{...}`` (or ``[...]``) in ``text``,
    or :data:`None`. Trims trailing prose a seat appends after the JSON args."""
    start = next((i for i, c in enumerate(text) if c in "{["), -1)
    if start == -1:
        return None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def repair_truncated_json(s: str) -> str | None:
    """Best-effort repair of a TRUNCATED JSON object.

    A tool call cut off mid-emit (thinking ate the budget, choppy stream) has
    no balanced ``{...}`` so :func:`find_json_object` returns None. Two
    attempts: (1) close an unclosed string + every open brace/bracket LIFO;
    (2) if that still won't parse, trim back to the last complete top-level
    ``key:value`` pair and close. Returns a parseable object string or
    :data:`None`.

    Adapted from qwen-code's streaming auto-repair, broadened with the
    trim-to-last-pair fallback (which catches the dangling-``key:`` shape that
    closing alone can't fix). Model-agnostic — any seat that runs out of
    budget mid-tool-call benefits; Qwen / Nemotron overthink-then-truncate is
    the motivating case but GPT-class models hit it too.
    """
    start = s.find("{")
    if start == -1:
        return None
    s = s[start:]

    stack: list[str] = []
    in_string = False
    escape = False
    last_pair_end: int | None = None  # index of the last depth-1 `,` (safe trim point)
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_string:
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
        elif ch == "," and len(stack) == 1:
            last_pair_end = i

    # attempt 1 — close the open string (if any) + every open bracket (LIFO).
    cand = s + ('"' if in_string else "") + "".join(reversed(stack))
    try:
        if isinstance(json.loads(cand), dict):
            return cand
    except json.JSONDecodeError:
        pass

    # attempt 2 — drop the truncated trailing fragment, keep complete pairs.
    if last_pair_end is not None:
        cand = s[:last_pair_end] + "}"
        try:
            if isinstance(json.loads(cand), dict):
                return cand
        except json.JSONDecodeError:
            pass
    return None


def coerce_tool_args(raw: Any) -> dict[str, Any] | None:
    """Parse a tool-call ``arguments`` value into a dict, tolerating common slips.

    Returns the dict, or :data:`None` if every repair fails (the caller keeps
    its own error path — the accumulator raises
    :class:`MalformedToolCallError` so structural failures still terminate the
    stream).

    Order:

    1. Already a dict → return as-is.
    2. Strict :func:`json.loads`.
    3. Strip a code fence + take the first balanced ``{...}`` (drops trailing
       prose).
    4. Single → double quotes on keys + values, then retry.
    5. :func:`repair_truncated_json` (qwen-code-adapted) — rescues a wasted
       round when an overthinking seat truncates its tool call at the token
       cap.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return {}

    # 1. strict
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass

    # 2. strip a code fence, then take the first balanced {...} (drops trailing prose)
    candidate = _FENCE_RE.sub("", s).strip()
    obj = find_json_object(candidate)
    if obj:
        try:
            v = json.loads(obj)
            if isinstance(v, dict):
                return v
            candidate = obj  # keep going (e.g. an array — rare for args)
        except json.JSONDecodeError:
            candidate = obj

    # 3. single → double quotes on keys/values, then retry
    repaired = _SQ_VAL_RE.sub(r'\1"\2"\3', _SQ_KEY_RE.sub(r'\1"\2"\3', candidate))
    try:
        v = json.loads(repaired)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass

    # 4. truncation repair (qwen-code-adapted): the tool call was cut off mid-emit, so
    #    no balanced object exists. Close/trim and retry — rescues a wasted round when
    #    an overthinking seat truncates its tool call at the token cap.
    fixed = repair_truncated_json(repaired)
    if fixed:
        try:
            v = json.loads(fixed)
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            pass
    return None


# ─── XML tool-call recovery ─────────────────────────────────────────────────
#
# Qwen3-Coder native tool-call format. Qwen3-Coder-derived reasoning models
# (including qwen3-coder-next) emit tool calls as XML
# (``<tool_call><function=NAME><parameter=K>\nVALUE\n</parameter></function></tool_call>``)
# not JSON. LM Studio's native ``/api/v1/chat`` is supposed to convert it to an
# OpenAI ``tool_calls`` array but sometimes FAILS, leaving the raw XML in
# ``message.delta`` content with the ``tool_call.*`` events never fired —
# the raw XML appears in the rendered chat as plain text.
# When this happens a consumer reading only ``tool_call.*``
# events sees a no-tool turn and the model's next round waits for a result
# that will never come. We recover it: parse the XML and synthesize
# ``CanonicalToolCall`` objects on the consumer side.
#
# Format verified against vLLM's ``test_qwen3coder_tool_parser.py``.
_XML_TC_WRAP_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.S)
_XML_FUNC_RE = re.compile(r"<function=([^>\s]+)\s*>(.*?)</function>", re.S)
_XML_PARAM_RE = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)</parameter>", re.S)

# Opening-only XML variant — observed on a reasoning-tuned model after a
# successful first firecrawl_search tool round. The model degraded to
# emitting only OPENING
# tags inside a closed ``<tool_call>`` wrapper:
#
#   <tool_call> <function=NAME> <parameter=K> VALUE <parameter=K2> VALUE2 </tool_call>
#
# No ``</function>``, no ``</parameter>``. Parameter VALUES run until the
# next ``<parameter=`` opener or the end of the wrapper. The strict
# ``_XML_FUNC_RE`` above won't match this dialect, so without these
# fallbacks the XML leaks into displayed content as plain text — exactly
# the "(reasoning surfaced because the model produced
# no final answer)" turn that ended without a real reply.
_XML_FUNC_OPEN_RE = re.compile(r"<function=([^>\s]+)\s*>", re.S)
_XML_PARAM_OPEN_RE = re.compile(r"<parameter=([^>\s]+)\s*>", re.S)


def recover_xml_tool_calls(
    content: str,
) -> tuple[list[dict[str, Any]], str] | None:
    """Recover Qwen3-Coder XML tool calls left in ``content`` (provider parser miss).

    Returns ``(tool_calls, cleaned_content)`` in OpenAI shape — ``arguments``
    is a JSON string so the existing dispatch path (:func:`coerce_tool_args`)
    handles it unchanged — or :data:`None` when no ``<function=...>`` block is
    present. Parameter values are JSON-parsed when they look like JSON
    (numbers / bools / objects), else kept as raw strings.

    Requires the full ``<tool_call>`` WRAPPER (the model's actual emission),
    not a bare ``<function=...>`` — so a chat message that quotes tool-call
    syntax (e.g. a documentation paste, a review finding) can't false-trigger
    a spurious call. Only function blocks INSIDE a wrapper are taken.
    """
    if not content or "<tool_call>" not in content:
        return None
    calls: list[dict[str, Any]] = []
    fi = 0
    for blk in _XML_TC_WRAP_RE.findall(content):
        # First pass — strict closing-tag variant
        # (Qwen3-Coder canonical, vLLM-verified format).
        any_matched = False
        for m in _XML_FUNC_RE.finditer(blk):
            any_matched = True
            name = m.group(1).strip()
            args: dict[str, Any] = {}
            for p in _XML_PARAM_RE.finditer(m.group(2)):
                key = p.group(1).strip()
                # Match vLLM's qwen3coder parser EXACTLY: strip ONE leading +
                # ONE trailing newline (the format delimiters), NOT a full
                # ``.strip()`` — so intentional surrounding whitespace in a
                # string arg survives and our recovery yields the same args
                # the provider's parser would.
                val = p.group(2)
                if val.startswith("\n"):
                    val = val[1:]
                if val.endswith("\n"):
                    val = val[:-1]
                try:
                    args[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    args[key] = val
            calls.append({
                "id": f"xmlcall_{fi}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            })
            fi += 1
        # Fallback — opening-only variant (observed on
        # qwen3.5-9b-polaris-highiq-thinking after a successful first tool
        # round). The model emits <function=NAME> and <parameter=K> as
        # OPENING tags only; parameter values run until the next
        # <parameter= opener or the end of the function body.
        if not any_matched:
            # Collect ALL function openers in the wrapper, then iterate
            # pairwise so each function's body is BOUNDED at the next
            # <function=> opener (or end of wrapper for the last). A naive
            # scan that ran to the end of the wrapper for every function
            # would let function 1's parameter scan swallow function 2's
            # parameters, so function 2 would emit them again — duplicating
            # args on every recovered call in a multi-function wrapper.
            func_openers = list(_XML_FUNC_OPEN_RE.finditer(blk))
            for i, fmatch in enumerate(func_openers):
                name = fmatch.group(1).strip()
                body_start = fmatch.end()
                if i + 1 < len(func_openers):
                    body_end = func_openers[i + 1].start()
                else:
                    body_end = len(blk)
                func_body = blk[body_start:body_end]
                # Split on <parameter= openers within THIS function's
                # bounded body; iterate pairwise so each value extends
                # to the next opener (or the end of the body).
                param_openers = list(_XML_PARAM_OPEN_RE.finditer(func_body))
                args = {}
                for j, pm in enumerate(param_openers):
                    key = pm.group(1).strip()
                    val_start = pm.end()
                    if j + 1 < len(param_openers):
                        val_end = param_openers[j + 1].start()
                    else:
                        val_end = len(func_body)
                    val = func_body[val_start:val_end]
                    # Opening-only dialect surrounds values with whitespace
                    # (e.g. ``<parameter=query> Paris ... <parameter=...``).
                    # Trim full whitespace, not just one newline, because
                    # the dialect doesn't preserve intentional padding.
                    val = val.strip()
                    try:
                        parsed = json.loads(val)
                    except (json.JSONDecodeError, ValueError):
                        parsed = val
                    # Opening-only values are raw text with no reference
                    # parser to match (unlike the closing-tag variant above,
                    # which deliberately mirrors vLLM's real qwen3coder
                    # parser). Only accept a json.loads result that is a
                    # genuine non-string JSON type (number / bool / object /
                    # array / null) per this function's docstring contract.
                    # A `str` result means json.loads unwrapped a JSON
                    # string LITERAL (e.g. `""` -> ``, `"hi"` -> `hi`),
                    # which would silently lose the raw value and break the
                    # roundtrip contract — keep the raw text instead.
                    args[key] = val if isinstance(parsed, str) else parsed
                calls.append({
                    "id": f"xmlcall_{fi}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                })
                fi += 1
    if not calls:
        return None
    # Strip the recovered <tool_call> wrappers from the content (keep
    # surrounding prose).
    cleaned = _XML_TC_WRAP_RE.sub("", content).strip()
    return calls, cleaned


__all__ = [
    "coerce_tool_args",
    "find_json_object",
    "recover_xml_tool_calls",
    "repair_truncated_json",
]
