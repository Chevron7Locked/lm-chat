# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.db.pragmas — PRAGMA dict and apply function.

Uses a real sqlite3 DBAPI connection
to confirm PRAGMAs are actually executed rather than just queuing up.
"""
from __future__ import annotations

import sqlite3

from lmchat.db.pragmas import _ENV_PREFIX, SQLITE_PRAGMAS, apply_sqlite_pragmas

# ---------------------------------------------------------------------------
# Default pragma set
# ---------------------------------------------------------------------------


def test_all_seven_pragmas_present():
    """SQLITE_PRAGMAS must contain exactly the 7 PRAGMAs."""
    expected_keys = {
        "journal_mode",
        "synchronous",
        "busy_timeout",
        "foreign_keys",
        "cache_size",
        "temp_store",
        "mmap_size",
    }
    assert set(SQLITE_PRAGMAS.keys()) == expected_keys


def test_apply_sqlite_pragmas_applies_all_defaults(tmp_path):
    """apply_sqlite_pragmas must apply all 7 default PRAGMAs.

    WAL mode requires a file-based DB (in-memory SQLite silently falls back
    to 'memory' journal mode).  We use a temp file so journal_mode=WAL can
    actually be verified.
    """
    db_file = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_file)
    apply_sqlite_pragmas(conn)

    cursor = conn.cursor()
    # foreign_keys should be ON (1)
    cursor.execute("PRAGMA foreign_keys")
    assert cursor.fetchone()[0] == 1

    # journal_mode=WAL requires a file-based DB
    cursor.execute("PRAGMA journal_mode")
    assert cursor.fetchone()[0].upper() == "WAL"

    # synchronous=NORMAL → SQLite returns 1
    cursor.execute("PRAGMA synchronous")
    assert cursor.fetchone()[0] == 1

    conn.close()


def test_apply_sqlite_pragmas_executes_without_error():
    """apply_sqlite_pragmas must complete without raising on a fresh connection."""
    conn = sqlite3.connect(":memory:")
    try:
        apply_sqlite_pragmas(conn)  # must not raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Env override
# ---------------------------------------------------------------------------


def test_env_override_journal_mode(monkeypatch, tmp_path):
    """LM_CHAT_SQLITE_JOURNAL_MODE env var must override the default value.

    Uses a temp file because journal_mode pragmas only take full effect on
    file-based databases (in-memory stays in 'memory' mode regardless).
    """
    monkeypatch.setenv(f"{_ENV_PREFIX}JOURNAL_MODE", "DELETE")

    db_file = str(tmp_path / "test_jm.db")
    conn = sqlite3.connect(db_file)
    apply_sqlite_pragmas(conn)

    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0].upper()
    conn.close()

    assert mode == "DELETE"


def test_env_override_synchronous(monkeypatch):
    """LM_CHAT_SQLITE_SYNCHRONOUS=FULL must override the default NORMAL."""
    monkeypatch.setenv(f"{_ENV_PREFIX}SYNCHRONOUS", "FULL")

    conn = sqlite3.connect(":memory:")
    apply_sqlite_pragmas(conn)

    cursor = conn.cursor()
    cursor.execute("PRAGMA synchronous")
    # FULL → SQLite returns 2
    value = cursor.fetchone()[0]
    conn.close()

    assert value == 2


def test_env_override_foreign_keys_off(monkeypatch):
    """LM_CHAT_SQLITE_FOREIGN_KEYS=OFF must turn FK enforcement off."""
    monkeypatch.setenv(f"{_ENV_PREFIX}FOREIGN_KEYS", "OFF")

    conn = sqlite3.connect(":memory:")
    apply_sqlite_pragmas(conn)

    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys")
    value = cursor.fetchone()[0]
    conn.close()

    assert value == 0
