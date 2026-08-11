# SPDX-License-Identifier: Apache-2.0
"""In-process registry of chat_ids with an active stream.

The stream reaper's
``_finalize_stuck_drafts`` must skip draft rows whose chat_id has an
actively-running stream.  ``STREAMS_ACTIVE`` (metrics.py) is a Prometheus
Gauge — a single integer count with no chat_id membership query.  This
module provides the set-based companion registry that the reaper can
consult.

Design notes
------------
- ``ACTIVE_STREAM_CHAT_IDS`` is a plain ``set`` protected only by the
  async event loop's cooperative multitasking guarantee (FastAPI / uvicorn
  run on a single asyncio event loop; there is no thread-level concurrency
  in the hot path).  No ``asyncio.Lock`` is needed for membership checks.
- ``streaming_service.py`` calls ``mark_active`` / ``mark_inactive`` around
  the streaming pump; ``_stream_reaper.py`` calls ``is_active`` inside
  ``_finalize_stuck_drafts`` to skip live rows.
- The registry is intentionally process-local (single-replica
  constraint).  It is NOT persisted; a restart resets it to empty, which
  is safe: after a restart there are no active streams, so the reaper's
  next tick correctly sees all draft rows as abandonable.
"""
from __future__ import annotations

ACTIVE_STREAM_CHAT_IDS: set[int] = set()


def mark_active(chat_id: int) -> None:
    """Record that *chat_id* has an in-progress stream."""
    ACTIVE_STREAM_CHAT_IDS.add(chat_id)


def mark_inactive(chat_id: int) -> None:
    """Remove *chat_id* from the active-stream registry.

    Idempotent: no-op if the chat_id is not present.
    """
    ACTIVE_STREAM_CHAT_IDS.discard(chat_id)


def is_active(chat_id: int) -> bool:
    """Return True if *chat_id* has an in-progress stream."""
    return chat_id in ACTIVE_STREAM_CHAT_IDS
