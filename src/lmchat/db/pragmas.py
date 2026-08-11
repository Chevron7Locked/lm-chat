# SPDX-License-Identifier: Apache-2.0
"""SQLite PRAGMA configuration for lm-chat.

Durability vs throughput trade-off for synchronous=NORMAL
----------------------------------------------------------
SQLite's ``synchronous`` PRAGMA controls how aggressively the OS
journal is flushed to disk before a transaction is considered
committed.

- ``synchronous=FULL`` (SQLite default) — every commit issues an
  fsync(2).  The database survives a power failure mid-write.
  Cost: ~1 fsync per write transaction, limiting throughput to
  ~100-200 writes/s on a typical spinning disk; SSDs are faster
  but fsync latency is still real.

- ``synchronous=NORMAL`` (our default) — SQLite syncs at
  "safe checkpoints" (WAL checkpoints and certain lock escalation
  points) but not after every individual commit.  The database
  survives a crash that only kills the SQLite process (OS and
  hardware are still running); it is NOT guaranteed to survive a
  power failure or OS panic mid-write.  Throughput is materially
  higher: writes are buffered in the WAL file and the OS page
  cache, so sequential-write batches can sustain several thousand
  transactions/second.

Rationale for choosing NORMAL:
- lm-chat runs on developer hardware and self-hosted servers where
  a sudden power failure is a recoverable event (restore from
  backup, replay from LM Studio's SSE stream).
- The streaming service writes draft messages at high frequency;
  FULL synchronous would introduce noticeable latency on every
  token save.
- WAL mode (journal_mode=WAL) already improves concurrent readers
  significantly; pairing it with NORMAL gives the best throughput
  profile for the 1-50 simultaneous user deployment target.

Admins who prefer FULL durability guarantees can override via
the environment variable::

    LM_CHAT_SQLITE_SYNCHRONOUS=FULL

All seven PRAGMAs support env-override via
``LM_CHAT_SQLITE_<PRAGMA_UPPER>=value``.  The override is applied
at engine creation time (see db/engine.py).
"""
from __future__ import annotations

import os
from typing import Any, Final

SQLITE_PRAGMAS: Final[dict[str, str]] = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "busy_timeout": "5000",   # 5 s — max wait before SQLITE_BUSY is raised
    "foreign_keys": "ON",
    "cache_size": "-65536",   # 64 MB (negative = kibibytes)
    "temp_store": "MEMORY",
    "mmap_size": "268435456", # 256 MB
}

_ENV_PREFIX: Final[str] = "LM_CHAT_SQLITE_"


def _resolve_pragmas() -> dict[str, str]:
    """Return PRAGMA dict with any env overrides applied.

    For each key in ``SQLITE_PRAGMAS``, checks
    ``LM_CHAT_SQLITE_<KEY_UPPER>`` in the environment.  If present,
    the env value replaces the default.
    """
    resolved: dict[str, str] = dict(SQLITE_PRAGMAS)
    for key in list(resolved):
        env_key = _ENV_PREFIX + key.upper()
        env_val = os.environ.get(env_key)
        if env_val is not None:
            resolved[key] = env_val
    return resolved


def apply_sqlite_pragmas(dbapi_conn: Any) -> None:  # noqa: ANN401
    """Apply all SQLite PRAGMAs to a raw DBAPI connection.

    Called from the SQLAlchemy ``connect`` event hook registered in
    ``db/engine.py``.  Each PRAGMA is executed as a separate
    statement; SQLite does not support parameterised PRAGMA values,
    so values are interpolated as literals after resolution through
    ``_resolve_pragmas()``.

    The function is intentionally synchronous: the ``connect`` event
    fires on the *sync* DBAPI layer beneath the async engine, so async
    execution is neither available nor needed here.

    Args:
        dbapi_conn: A raw ``sqlite3.Connection`` object as provided by
            the SQLAlchemy connect event.
    """
    pragmas = _resolve_pragmas()
    cursor = dbapi_conn.cursor()
    try:
        for pragma, value in pragmas.items():
            cursor.execute(f"PRAGMA {pragma} = {value}")
        dbapi_conn.commit()
    finally:
        cursor.close()
