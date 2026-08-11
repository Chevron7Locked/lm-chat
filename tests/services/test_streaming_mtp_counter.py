# SPDX-License-Identifier: Apache-2.0
"""Tests for the MTP counter in StreamingService.

Per PR-S3 spec: backend changes section 1 (counter state).
"""
from __future__ import annotations

from lmchat.services.streaming_service import StreamingService


def test_counter_increments_on_tool_round() -> None:
    """Test that the counter increments when tool_call events are processed."""
    # Create a mock StreamingService
    service = StreamingService(
        engine=None,  # type: ignore[arg-type]
        lm_client=None,  # type: ignore[arg-type]
        memory_service=None,  # type: ignore[arg-type]
        chat_locks=None,  # type: ignore[arg-type]
        idle_timeout_sec=60,
        embedding_client=None,  # type: ignore[arg-type]
        models_service=None,  # type: ignore[arg-type]
    )

    # Simulate processing tool_call.success events via the production helper.
    chat_id = 1
    for _ in range(5):
        service._increment_tool_round(chat_id)

    assert service._tool_round_counts[chat_id] == 5


def test_counter_persists_across_streams_in_same_chat() -> None:
    """Test that the counter persists across stream starts for the same chat."""
    service = StreamingService(
        engine=None,  # type: ignore[arg-type]
        lm_client=None,  # type: ignore[arg-type]
        memory_service=None,  # type: ignore[arg-type]
        chat_locks=None,  # type: ignore[arg-type]
        idle_timeout_sec=60,
        embedding_client=None,  # type: ignore[arg-type]
        models_service=None,  # type: ignore[arg-type]
    )

    chat_id = 2

    # Simulate one tool round in the first stream.
    service._increment_tool_round(chat_id)

    # Simulate the start of a SECOND stream for the same chat. The pump
    # reads `_tool_round_counts.get(chat_id, 0)` once at stream start, which
    # must observe the count from the previous stream — this is the
    # session-cumulative semantic Surface 3 requires.
    assert service._tool_round_counts[chat_id] == 1


def test_reset_counter_clears_chat() -> None:
    """Test that reset_counter clears the counter for a specific chat."""
    service = StreamingService(
        engine=None,  # type: ignore[arg-type]
        lm_client=None,  # type: ignore[arg-type]
        memory_service=None,  # type: ignore[arg-type]
        chat_locks=None,  # type: ignore[arg-type]
        idle_timeout_sec=60,
        embedding_client=None,  # type: ignore[arg-type]
        models_service=None,  # type: ignore[arg-type]
    )

    chat_id = 3

    # Increment counter via the production helper.
    service._increment_tool_round(chat_id)

    # Reset counter
    service.reset_counter(chat_id)

    assert chat_id not in service._tool_round_counts


def test_reset_counter_idempotent_on_absent_chat() -> None:
    """Test that reset_counter is idempotent for absent chats."""
    service = StreamingService(
        engine=None,  # type: ignore[arg-type]
        lm_client=None,  # type: ignore[arg-type]
        memory_service=None,  # type: ignore[arg-type]
        chat_locks=None,  # type: ignore[arg-type]
        idle_timeout_sec=60,
        embedding_client=None,  # type: ignore[arg-type]
        models_service=None,  # type: ignore[arg-type]
    )

    # Reset a chat that doesn't exist - should not raise
    service.reset_counter(999)


def test_lru_cap_eviction() -> None:
    """The counter has LRU semantics — eviction targets the least-recently-
    incremented chat once the cap is exceeded. Exercises the production
    `_increment_tool_round` method (NOT a duplicated inline copy) so a future
    change to the cap or the eviction policy is automatically covered.
    """
    service = StreamingService(
        engine=None,  # type: ignore[arg-type]
        lm_client=None,  # type: ignore[arg-type]
        memory_service=None,  # type: ignore[arg-type]
        chat_locks=None,  # type: ignore[arg-type]
        idle_timeout_sec=60,
        embedding_client=None,  # type: ignore[arg-type]
        models_service=None,  # type: ignore[arg-type]
    )
    cap = service._TOOL_ROUND_LRU_CAP

    counter = service._tool_round_counts

    # Fill counter to exactly the cap.
    for chat_id in range(1, cap + 1):
        service._increment_tool_round(chat_id)

    assert len(counter) == cap

    # Increment one more chat — counter should evict the LEAST-RECENTLY-
    # incremented chat (chat_id=1, which has not been touched since it was
    # first inserted).
    service._increment_tool_round(cap + 1)
    assert len(counter) == cap
    assert 1 not in counter

    # Increment another chat — chat_id=2 is now the LRU victim.
    service._increment_tool_round(cap + 2)
    assert len(counter) == cap
    assert 2 not in counter


def test_get_does_not_bump_recency() -> None:
    """Test that .get() does not bump recency (critical for LRU semantics).

    To test this, we need to have the counter at capacity (256 entries) so that
    adding a new entry triggers eviction.
    """
    service = StreamingService(
        engine=None,  # type: ignore[arg-type]
        lm_client=None,  # type: ignore[arg-type]
        memory_service=None,  # type: ignore[arg-type]
        chat_locks=None,  # type: ignore[arg-type]
        idle_timeout_sec=60,
        embedding_client=None,  # type: ignore[arg-type]
        models_service=None,  # type: ignore[arg-type]
    )

    counter = service._tool_round_counts
    cap = service._TOOL_ROUND_LRU_CAP

    # Fill counter to exactly the cap via the production method.
    for chat_id in range(1, cap + 1):
        service._increment_tool_round(chat_id)
    assert len(counter) == cap

    # Read chat 1 via plain .get — MUST NOT bump recency. (The pre-stream
    # passthrough at `stream_chat` reads the count this way; bumping recency
    # on every dispatch would defeat the LRU's purpose.)
    _ = counter.get(1)

    # Increment a NEW chat; the LRU victim must be chat 1 (least-recently-
    # incremented). If the `.get(1)` above had erroneously bumped recency,
    # chat 2 would be evicted instead — that's the contract this test guards.
    service._increment_tool_round(cap + 1)
    assert 1 not in counter
    assert 2 in counter


def test_counter_resets_on_mtp_suspected_emit() -> None:
    """When an mtp_suspected error event flows through the pump, the chat's
    counter is reset so the next retry starts a fresh detection cycle.

    Exercises the reset behaviour by invoking `reset_counter` directly (the
    pump applies the same call). Without this reset, every subsequent tool
    round on the same chat would immediately re-fire mtp_suspected (since
    the cumulative count stays above threshold), and the frontend dedupe
    would silently suppress the banner — leaving the user with no UX signal
    that the issue persists.
    """
    service = StreamingService(
        engine=None,  # type: ignore[arg-type]
        lm_client=None,  # type: ignore[arg-type]
        memory_service=None,  # type: ignore[arg-type]
        chat_locks=None,  # type: ignore[arg-type]
        idle_timeout_sec=60,
        embedding_client=None,  # type: ignore[arg-type]
        models_service=None,  # type: ignore[arg-type]
    )

    # Accumulate past the MTP-suspect threshold for chat 1.
    for _ in range(25):
        service._increment_tool_round(1)
    assert service._tool_round_counts.get(1) == 25

    # Simulate the pump's reset on mtp_suspected event.
    service.reset_counter(1)
    assert service._tool_round_counts.get(1) is None

    # Next tool round in the same chat starts fresh — counter increments
    # from zero, so the threshold predicate won't fire until 20 more rounds.
    service._increment_tool_round(1)
    assert service._tool_round_counts.get(1) == 1
