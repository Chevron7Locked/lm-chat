# SPDX-License-Identifier: Apache-2.0
"""Streaming-service exception hierarchy for lm-chat.

Each exception maps to a specific HTTP status code or SSE error frame.

Public surface
--------------
- ``StreamingServiceError`` — base; catch-all for streaming-service failures.
- ``StreamInProgressError``  — another draft/pending row exists; → HTTP 409.
- ``SubSessionStreamInProgressError`` — an actively-streaming (draft)
  sub-session row exists for the chat_id; → HTTP 409. Blocks on ``draft``
  ONLY (not ``pending_finalization``). Independent of the main-chat check.
- ``StreamUpstreamError``    — adapter surfaced an upstream failure.
- ``StreamIdleTimeoutError`` — application-level idle timeout fired.
- ``StreamRateLimitError``   — per-user stream bucket exhausted; → HTTP 429.
"""
from __future__ import annotations


class StreamingServiceError(Exception):
    """Base class for all StreamingService failures."""


class StreamInProgressError(StreamingServiceError):
    """Another draft or pending_finalization row exists for the given chat_id.

    Raised inside the per-chat lock when the single-stream-per-chat invariant
    is violated.  The route maps this to HTTP 409 Conflict with body
    ``{"code": "stream_in_progress", "chat_id": <n>}``.

    Args:
        chat_id: The chat for which a stream is already in progress.
    """

    def __init__(self, chat_id: int) -> None:
        """Initialise with the conflicting chat_id.

        Args:
            chat_id: PK of the chat that already has an in-progress stream.
        """
        self.chat_id = chat_id
        super().__init__(
            f"A stream is already in progress for chat_id={chat_id!r}. "
            "Only one concurrent stream per chat is allowed."
        )


class SubSessionStreamInProgressError(StreamInProgressError):
    """An actively-streaming (draft) sub-session row exists for chat_id.

    Distinct subclass (not the bare main-chat ``StreamInProgressError``)
    so the route layer can tell "a MAIN-CHAT stream is in progress" apart
    from "a SUB-SESSION stream is in progress" while reusing the same 409
    mapping shape and ``chat_id`` attribute. D4 (durable sub-sessions,
    migration 0045): sub-session concurrency is independent of the
    main-chat single-stream invariant — this is raised by the
    sub-session's OWN in-progress check
    (``streaming_service._assert_no_sub_session_stream_in_progress``),
    never by ``StreamingService._assert_no_in_progress_stream``.

    Unlike the main-chat ``StreamInProgressError``, this blocks on ``draft``
    ONLY. A ``pending_finalization`` row is a completed turn awaiting the
    reaper's commit — not a live stream — so it must not block a follow-up
    finalize (see the guard's docstring for the D9 liveness rationale).
    """


class StreamUpstreamError(StreamingServiceError):
    """Wraps an upstream adapter failure (non-retryable after retry budget).

    Raised when the adapter surfaces an error event that the streaming service
    cannot recover from (e.g. second-consecutive 400 after param-strip retry,
    or a network-level error).

    Args:
        code:    Machine-readable error code (from the canonical error event).
        message: Human-readable error detail.
    """

    def __init__(self, *, code: str, message: str) -> None:
        """Initialise with code + message from the upstream error event.

        Args:
            code:    Short machine-readable error code.
            message: Human-readable detail.
        """
        self.code = code
        self.message = message
        super().__init__(f"upstream error: code={code!r} message={message!r}")


class StreamIdleTimeoutError(StreamingServiceError):
    """Application-level idle timeout fired during a stream.

    Raised when LM Studio keeps the TCP connection alive (heartbeat events)
    but no content-bearing event arrives within
    ``settings.lm_chat_stream_idle_timeout_sec`` seconds (default 60s).

    Args:
        idle_sec: Elapsed seconds since the last content-bearing event.
    """

    def __init__(self, idle_sec: float) -> None:
        """Initialise with the measured idle duration.

        Args:
            idle_sec: Seconds since the last content-bearing SSE event.
        """
        self.idle_sec = idle_sec
        super().__init__(
            f"upstream stall: no content-bearing event for {idle_sec:.1f}s"
        )


class StreamRateLimitError(StreamingServiceError):
    """Per-user stream bucket exhausted.

    Raised by the ``stream_rate_limit`` FastAPI dependency when the per-user
    token bucket for ``POST /api/chat/stream`` is empty.  The route maps this
    to HTTP 429 with a ``Retry-After`` header.

    Note: In practice this error is emitted by the dependency layer (before
    the service is invoked), not by ``StreamingService`` itself.  It lives in
    this module for taxonomy completeness and testability.

    Args:
        retry_after: Seconds until the next token is available in the bucket.
    """

    def __init__(self, retry_after: float) -> None:
        """Initialise with the retry-after duration.

        Args:
            retry_after: Seconds to wait before the next request will succeed.
        """
        self.retry_after = retry_after
        super().__init__(
            f"stream rate limit exceeded; retry after {retry_after:.1f}s"
        )
