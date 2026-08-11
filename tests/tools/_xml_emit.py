# SPDX-License-Identifier: Apache-2.0
"""XML tool-call emission helpers for property-based round-trip testing.

Two dialects matching :mod:`lmchat.services.tool_args`:

* **Closing-tag dialect** (``_XML_FUNC_RE`` / ``_XML_PARAM_RE``) —
  canonical Qwen3-Coder format, vLLM-verified.
* **Opening-only dialect** (``_XML_FUNC_OPEN_RE`` / ``_XML_PARAM_OPEN_RE``) —
  observed 2026-06-11 degradation on
  ``qwen3.5-9b-polaris-highiq-thinking``.

Usage
-----
>>> from tests.tools._xml_emit import emit_closing_tag_xml
>>> emit_closing_tag_xml("search", {"q": "hello", "limit": 5})
'<tool_call>\\n<function=search>\\n<parameter=q>\\nhello\\n<parameter=limit>\\n5\\n</tool_call>'
"""

from __future__ import annotations

import json
from typing import Any


def emit_closing_tag_xml(name: str, args: dict[str, Any]) -> str:
    """Emit a closing-tag ``<tool_call><function=…>…</function></tool_call>``.

    Each parameter value is wrapped in
    ``<parameter=K>\\nVALUE\\n</parameter>`` — one leading and one trailing
    newline, which is what the parser strips, so the round-trip is lossless
    for strings and correctly JSON-parsed for non-strings.
    """
    params = "".join(
        f"<parameter={k}>\n{_val_str(v)}\n</parameter>" for k, v in args.items()
    )
    return f"<tool_call>\n<function={name}>\n{params}</function>\n</tool_call>"


def emit_closing_tag_xml_multi(
    calls: list[tuple[str, dict[str, Any]]],
) -> str:
    """Emit multiple tool calls, each in its own ``<tool_call>`` wrapper."""
    return "".join(emit_closing_tag_xml(name, args) for name, args in calls)


def emit_opening_only_xml(name: str, args: dict[str, Any]) -> str:
    """Emit an opening-only dialect block (no ``</function>`` nor
    ``</parameter>``).

    Format::

        <tool_call> <function=NAME> <parameter=K> VALUE … </tool_call>

    Values are separated by a single space after each ``<parameter=K>``;
    the parser calls ``.strip()`` + ``json.loads`` attempt.
    """
    parts = [f" <function={name}>"]
    for k, v in args.items():
        parts.append(f" <parameter={k}> {_val_str(v)}")
    parts.append(" </tool_call>")
    return "<tool_call>" + "".join(parts)


def emit_opening_only_xml_multi(
    calls: list[tuple[str, dict[str, Any]]],
) -> str:
    """Emit multiple tool calls in ONE wrapper (the D1 multi-function surface).

    Format::

        <tool_call> <function=NAME> <parameter=K> VAL
                    <function=NAME2> <parameter=K2> VAL2
                    </tool_call>

    Each function's parameter scan MUST be bounded at the next ``<function=``
    opener — the D1 bug was that function 1's scan swallowed function 2's
    parameters, so function 2 re-emitted them, duplicating args.
    """
    parts = ["<tool_call>"]
    for name, args in calls:
        parts.append(f" <function={name}>")
        for k, v in args.items():
            parts.append(f" <parameter={k}> {_val_str(v)}")
    parts.append(" </tool_call>")
    return "".join(parts)


def _val_str(v: Any) -> str:
    """Render a single parameter value for XML emission.

    Strings pass through verbatim — the parser's ``json.loads`` attempt will
    keep them as strings when it fails (which it will for most plain-text
    strings).  Non-strings are JSON-encoded so the parser round-trips them
    to the same Python type.
    """
    if isinstance(v, str):
        return v
    return json.dumps(v)