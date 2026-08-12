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
- Durable sub-sessions (migration 0045) mark the SAME ``chat_id`` active
  while a ``/research``-style sub-session streams alongside the main chat
  — a main-chat stream and a sub-session stream may run concurrently on
  one chat_id (D4). ``mark_active``/``mark_inactive`` are therefore
  REFCOUNTED per chat_id: the first ``mark_active`` call for a chat_id
  adds it to ``ACTIVE_STREAM_CHAT_IDS``, and a chat_id is only removed
  once EVERY matching ``mark_inactive`` call has landed — the first
  stream to finish must not evict the chat_id out from under the other's
  still-live draft. ``ACTIVE_STREAM_CHAT_IDS`` itself stays a real
  ``set[int]`` (not a dict) so existing ``in`` / ``not in`` membership
  checks (production and test) are unaffected for the common
  single-stream-per-chat case.
- The registry is intentionally process-local (single-replica
  constraint).  It is NOT persisted; a restart resets it to empty, which
  is safe: after a restart there are no active streams, so the reaper's
  next tick correctly sees all draft rows as abandonable.
"""
from __future__ import annotations

ACTIVE_STREAM_CHAT_IDS: set[int] = set()

# Per-chat_id count of outstanding mark_active() calls not yet matched by a
# mark_inactive(). Not exported — ACTIVE_STREAM_CHAT_IDS is the public
# membership surface; this dict only exists to make mark_inactive()
# idempotent-per-caller under concurrent streams on the same chat_id.
_ACTIVE_STREAM_REFCOUNTS: dict[int, int] = {}


def mark_active(chat_id: int) -> None:
    """Record that *chat_id* has an in-progress stream.

    Safe to call more than once for the same chat_id (e.g. a main-chat
    stream and a sub-session stream running concurrently) — each call
    increments the refcount and must be matched by its own
    ``mark_inactive`` call.
    """
    _ACTIVE_STREAM_REFCOUNTS[chat_id] = _ACTIVE_STREAM_REFCOUNTS.get(chat_id, 0) + 1
    ACTIVE_STREAM_CHAT_IDS.add(chat_id)


def mark_inactive(chat_id: int) -> None:
    """Remove one outstanding stream for *chat_id*.

    Idempotent: a call with no matching ``mark_active`` is a no-op.
    ``chat_id`` is only removed from ``ACTIVE_STREAM_CHAT_IDS`` once its
    refcount reaches zero, so a second concurrent stream on the same
    chat_id (main + sub-session) keeps the chat_id registered until BOTH
    streams have torn down.
    """
    count = _ACTIVE_STREAM_REFCOUNTS.get(chat_id, 0)
    if count <= 1:
        _ACTIVE_STREAM_REFCOUNTS.pop(chat_id, None)
        ACTIVE_STREAM_CHAT_IDS.discard(chat_id)
    else:
        _ACTIVE_STREAM_REFCOUNTS[chat_id] = count - 1


def is_active(chat_id: int) -> bool:
    """Return True if *chat_id* has an in-progress stream."""
    return chat_id in ACTIVE_STREAM_CHAT_IDS
