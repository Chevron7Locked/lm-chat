# SPDX-License-Identifier: Apache-2.0
"""``_folder_archival._archive_folders_sync_op`` dialect dispatch.

Postgres support in
migration 0023a was previously stripped to avoid partial-state. This
test verifies the dialect dispatch sends the right SQL on SQLite
(``json_set``) vs Postgres (``jsonb_set``), without needing a live
Postgres test container.

The SQLite branch already round-trips through the real ``json_set``
in ``tests/db/test_migration_0023a.py::test_0023a_archives_folders_into_meta``;
this test focuses on the Postgres SQL shape + the unsupported-dialect
hard-fail.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lmchat.services._folder_archival import (
    _archive_folders_sync_op,
)


def _row(rid: int, folders: list[str] | None) -> SimpleNamespace:
    return SimpleNamespace(id=rid, folders=folders)


def _make_bind(dialect_name: str, rows: list[SimpleNamespace]):
    """Stub bind that records every ``execute`` call. The first call
    is the SELECT; subsequent calls are the per-row UPDATEs."""
    bind = MagicMock()
    bind.dialect.name = dialect_name

    select_result = MagicMock()
    select_result.fetchall.return_value = rows

    update_calls: list[tuple[str, dict]] = []

    def _execute(stmt, params=None):
        text = str(stmt)
        if text.startswith("SELECT"):
            return select_result
        update_calls.append((text, params or {}))
        return MagicMock()

    bind.execute = _execute
    bind._update_calls = update_calls  # type: ignore[attr-defined]
    return bind


def test_postgres_branch_emits_jsonb_set_with_to_jsonb() -> None:
    """Postgres branch uses ``jsonb_set`` + ``to_jsonb`` (not
    SQLite's ``json_set``) so the cast to jsonb survives a column
    that's declared json instead of jsonb."""
    rows = [_row(10, ["A", "B"]), _row(20, [])]
    bind = _make_bind("postgresql", rows)

    _archive_folders_sync_op(bind, batch_size=100)

    calls = bind._update_calls  # type: ignore[attr-defined]
    assert len(calls) == 2, f"expected one UPDATE per row, got {len(calls)}"

    text0, params0 = calls[0]
    assert "jsonb_set" in text0, text0
    assert "to_jsonb" in text0, text0
    assert "{folders}" in text0, text0
    assert "json_set" not in text0, (
        "postgres branch must NOT emit SQLite's json_set"
    )
    assert params0 == {"folders": '["A", "B"]', "id": 10}

    _, params1 = calls[1]
    assert params1 == {"folders": "[]", "id": 20}


def test_sqlite_branch_emits_json_set() -> None:
    """SQLite branch uses ``json_set`` (not Postgres's
    ``jsonb_set``). Already covered end-to-end by the migration
    integration test; this is a fast sanity check that the dispatch
    didn't accidentally swap branches."""
    rows = [_row(5, ["X"])]
    bind = _make_bind("sqlite", rows)

    _archive_folders_sync_op(bind, batch_size=100)

    calls = bind._update_calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    text, params = calls[0]
    assert "json_set" in text
    assert "jsonb_set" not in text
    assert params == {"folders": '["X"]', "id": 5}


def test_unsupported_dialect_raises_not_implemented() -> None:
    """Dialects other than sqlite/postgresql hard-fail at the dispatch
    so the admin knows the archival has no path for their
    deployment shape."""
    rows: list[SimpleNamespace] = []
    bind = _make_bind("mysql", rows)
    with pytest.raises(NotImplementedError, match="dialect='mysql'"):
        _archive_folders_sync_op(bind, batch_size=100)


def test_archival_handles_none_folders_as_empty_list() -> None:
    """A project row where ``folders`` was NULL pre-archival
    round-trips as ``meta.folders = []`` — no NoneType.encode errors."""
    rows = [_row(1, None)]
    bind = _make_bind("sqlite", rows)
    _archive_folders_sync_op(bind, batch_size=100)
    calls = bind._update_calls  # type: ignore[attr-defined]
    assert calls[0][1] == {"folders": "[]", "id": 1}


def test_archival_batches_respect_batch_size() -> None:
    """``batch_size`` controls per-iteration slice. The dispatch is
    one UPDATE per row regardless of batch size, but the batch size
    enforces the slicing pattern — verify the per-row count is right
    across a few sizes."""
    rows = [_row(i, ["x"]) for i in range(7)]
    bind = _make_bind("sqlite", rows)
    _archive_folders_sync_op(bind, batch_size=3)
    calls = bind._update_calls  # type: ignore[attr-defined]
    assert len(calls) == 7
    assert [c[1]["id"] for c in calls] == list(range(7))
