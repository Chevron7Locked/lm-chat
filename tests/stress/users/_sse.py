# SPDX-License-Identifier: Apache-2.0
"""Minimal Server-Sent Events parser for Locust's requests-backed client.

Locust ships ``HttpUser.client`` which is a ``requests.Session`` patched
with the geventhttpclient adapter.  Streaming responses expose
``iter_lines`` which yields raw bytes per line.  The ``text/event-stream``
wire format (W3C SSE) is a tiny line-based protocol; rolling a focused
parser here avoids depending on an unmaintained external plugin (PLAN
§ Tool selection notes).

Wire shape (per docs/LM_STUDIO_HARNESS.md + tests/stress/test_50_concurrent_streams.py):

    event: <type>
    data: {"type": "<type>", "msg_id": <int>, ...}
    <blank line>

Multi-line ``data:`` continuations are folded with a leading newline per
spec.  Comment lines (`:`-prefixed keepalives) are ignored.

This parser is intentionally tolerant of CRLF, missing event field,
trailing whitespace.  It raises ``SSEParseError`` only on JSON decode
failure, never on framing oddities.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any


class SSEParseError(ValueError):
    """Raised when a data line fails JSON decoding."""


@dataclass(frozen=True)
class SSEEvent:
    """One decoded SSE frame.

    Args:
        event: The ``event:`` field value, or empty string when absent.
        data: The parsed JSON payload from ``data:``; may be any JSON
              value (dict / list / primitive).
        raw_data: The raw concatenated ``data:`` lines for diagnostic
              logging when JSON decoding fails downstream.
    """

    event: str
    data: Any
    raw_data: str


def iter_sse(
    lines: Iterable[bytes],
    *,
    decode_json: bool = True,
) -> Iterator[SSEEvent]:
    """Iterate decoded SSE events from a byte-line iterator.

    Args:
        lines: An iterator of bytes lines as produced by
               ``requests.Response.iter_lines(decode_unicode=False)``.
        decode_json: When True (default), ``data:`` payloads are parsed
               as JSON; failures raise ``SSEParseError``.  When False,
               the raw concatenated data string is returned as
               ``event.data`` unchanged.

    Yields:
        ``SSEEvent`` instances.  A frame is emitted on a blank line
        (or when the iterator ends with a pending buffer).
    """
    event_name = ""
    data_lines: list[str] = []

    def _flush() -> SSEEvent | None:
        nonlocal event_name, data_lines
        if not data_lines and not event_name:
            return None
        raw = "\n".join(data_lines)
        if decode_json and raw:
            try:
                payload: Any = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SSEParseError(
                    f"non-JSON data payload: {raw[:80]!r}"
                ) from exc
        else:
            payload = raw
        out = SSEEvent(event=event_name, data=payload, raw_data=raw)
        event_name = ""
        data_lines = []
        return out

    for raw_line in lines:
        # iter_lines yields bytes; allow str pass-through for tests.
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        # Trim CR (some clients fold CRLF -> LF imperfectly).
        line = line.rstrip("\r")

        if line == "":
            frame = _flush()
            if frame is not None:
                yield frame
            continue

        if line.startswith(":"):
            # SSE comment / keepalive.
            continue

        if line.startswith("event:"):
            event_name = line[6:].lstrip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # Other SSE fields (id:, retry:) — we don't need them; ignore.

    # Final flush in case the stream ended without a trailing blank line.
    tail = _flush()
    if tail is not None:
        yield tail
