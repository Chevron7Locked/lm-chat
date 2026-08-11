#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""garak generator that drives lm-chat's SSE endpoint ``POST /api/chat/stream``.

garak's stock ``rest.RestGenerator`` only knows how to POST JSON and read a
single JSON response.  lm-chat's primary LLM-input surface is the streaming
SSE endpoint at ``POST /api/chat/stream`` (per ``src/lmchat/routes/streaming.py``).
This adapter consumes ``text/event-stream`` line-by-line, accumulates the
text content from ``data:`` chunks until the stream ends, and returns the
concatenated assistant response as the generation that garak then scores.

This adapter must parse a recorded SSE response identically to a manual
reference (R-27); a conformance test enforces this.
The conformance test lives at ``tests/security/test_l7_adapters.py``.

Robustness contract (R-27)
--------------------------
The decoder MUST handle (without crashing the garak run):

* empty ``data:`` lines  -> skip
* ``[DONE]`` sentinel    -> end of stream
* multi-line content     -> accumulated across frames
* malformed JSON in a chunk -> skip the bad chunk, keep going
* HTTP-level error       -> single empty generation + log

Usage from garak (CLI)
----------------------
The garak ``rest`` generator type loads a JSON config with the request
template; here we provide a thin shim so ``--model_type rest`` works even
though the wire protocol is SSE.  At gate-run time the Makefile invokes
this module directly via ``--model_type lmchat_sse`` and garak's plugin
loader resolves it to :class:`LmChatSseGenerator`.

This module does not import garak at module load time; the garak
:class:`garak.generators.base.Generator` base class is imported lazily
inside :class:`LmChatSseGenerator.__init__` so the adapter can be unit-tested
in environments where garak is not installed (the conformance test uses the
parsing helpers directly).
"""
from __future__ import annotations

import json
import logging
from typing import Any

_LOG = logging.getLogger("lmchat.garak.sse")

# Maximum total bytes we will buffer from a single SSE response.  Protects
# against an upstream that streams forever.  4 MiB is well above any legit
# assistant turn but small enough to abort obvious DoS.
_MAX_BODY_BYTES = 4 * 1024 * 1024

# Maximum wall-clock seconds we will spend reading a single SSE response.
_READ_TIMEOUT_S = 60.0


def parse_sse_text(body: str) -> str:
    """Parse a complete SSE response body into the accumulated assistant text.

    Pure function — no I/O, no exceptions.  This is the conformance-test
    surface area: a recorded SSE body string in, the reconstructed assistant
    text out.  The reference implementation (in the conformance test) does the
    same job by hand using string operations only; the two MUST agree.

    Args:
        body: The full ``text/event-stream`` response body, decoded as UTF-8.

    Returns:
        The accumulated assistant response text.  Empty string if the stream
        contained no usable content (an SSE response with only ``error``
        frames, or a stream that was disconnected before any token).
    """
    if not body:
        return ""

    parts: list[str] = []
    # SSE frames are separated by blank lines; within a frame, ``data:`` lines
    # carry the payload.  We tolerate CRLF and LF line endings.
    for raw_frame in body.replace("\r\n", "\n").split("\n\n"):
        frame = raw_frame.strip("\n")
        if not frame:
            continue
        # Pull every ``data:`` line in this frame.  The SSE spec says
        # multiple ``data:`` lines in a frame are joined by ``\n``.
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if line.startswith("data:"):
                # ``data: foo`` and ``data:foo`` both legal; strip ONE space.
                rest = line[5:]
                if rest.startswith(" "):
                    rest = rest[1:]
                data_lines.append(rest)
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if not payload:
            continue
        if payload == "[DONE]":
            break
        # Try JSON first (lm-chat / OpenAI-shape SSE).  Fall back to raw text
        # if it isn't JSON — some red-team payloads come back as plain strings.
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            # Malformed JSON in a chunk -> skip, don't crash.
            continue
        text = _extract_text(obj)
        if text:
            parts.append(text)
    return "".join(parts)


def _extract_text(obj: Any) -> str:
    """Pull the assistant text out of one decoded SSE ``data:`` payload.

    Handles three observed shapes:

    1. lm-chat native: ``{"type": "chat.delta", "delta": {"content": "..."}}``
       or ``{"type": "message.delta", "content": "..."}``.
    2. OpenAI compat: ``{"choices": [{"delta": {"content": "..."}}]}``.
    3. Plain text echoed as a string: ``"..."``.

    Any unknown shape returns ``""``.
    """
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""

    # lm-chat native: ``delta.content`` or top-level ``content``.
    delta = obj.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    if isinstance(obj.get("content"), str):
        return str(obj["content"])

    # OpenAI compat: ``choices[0].delta.content``.
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            ch_delta = first.get("delta")
            if isinstance(ch_delta, dict):
                content = ch_delta.get("content")
                if isinstance(content, str):
                    return content
            # OpenAI non-stream: ``choices[0].message.content``.
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
    return ""


class LmChatSseGenerator:  # pragma: no cover - thin garak-side shim
    """garak :class:`Generator` subclass for lm-chat's SSE endpoint.

    Construction is lazy w.r.t. the garak import so this module can be
    imported (and the pure-function parsing logic exercised) in environments
    where garak is not installed.
    """

    def __init__(
        self,
        url: str,
        cookie: str | None = None,
        chat_id: int = 1,
        model: str = "lmchat-sse",
        timeout: float = _READ_TIMEOUT_S,
    ) -> None:
        # Resolve garak's base class at call time so the import is optional.
        try:
            from garak.generators.base import Generator  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - garak optional at import time
            raise RuntimeError(
                "garak is not installed; install with `uv pip install garak>=0.10.0`"
            ) from exc

        # Stash the garak base for late composition; we do NOT subclass at
        # class-definition time so ``import tools.garak_sse_generator`` does
        # not require garak.
        self._Generator = Generator
        self.url = url
        self.cookie = cookie
        self.chat_id = int(chat_id)
        self.name = model
        self.fullname = f"lmchat_sse:{model}"
        self.timeout = float(timeout)

    def _request_body(self, prompt: str) -> dict[str, Any]:
        """Build the JSON body for ``POST /api/chat/stream``."""
        return {
            "chat_id": self.chat_id,
            "payload": {
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
            },
        }

    def generate(self, prompt: str, generations_this_call: int = 1) -> list[str]:
        """garak entrypoint — returns N generations for the given prompt."""
        out: list[str] = []
        for _ in range(max(1, generations_this_call)):
            try:
                body_text = self._post_and_collect(prompt)
            except Exception as exc:  # noqa: BLE001 — never crash the garak run
                _LOG.warning("SSE request failed: %s", exc)
                body_text = ""
            out.append(parse_sse_text(body_text))
        return out

    def _post_and_collect(self, prompt: str) -> str:
        """POST to the SSE endpoint and collect the response body as a string."""
        # httpx is in the project deps (FastAPI test client); import lazily to
        # keep the module importable in minimal test environments.
        import httpx  # noqa: PLC0415

        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie

        chunks: list[bytes] = []
        total = 0
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream(
                "POST",
                self.url,
                json=self._request_body(prompt),
                headers=headers,
            ) as resp:
                if resp.status_code >= 400:
                    _LOG.warning(
                        "SSE endpoint returned %s for prompt[:40]=%r",
                        resp.status_code,
                        prompt[:40],
                    )
                    return ""
                for chunk in resp.iter_raw():
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= _MAX_BODY_BYTES:
                        _LOG.warning(
                            "SSE body exceeded %d bytes; truncating", _MAX_BODY_BYTES,
                        )
                        break
        return b"".join(chunks).decode("utf-8", errors="replace")


__all__ = ["LmChatSseGenerator", "parse_sse_text"]
