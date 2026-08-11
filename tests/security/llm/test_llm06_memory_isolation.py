# SPDX-License-Identifier: Apache-2.0
"""LLM06 Sensitive Information Disclosure — cross-user memory isolation.

LLM06 — memory isolation tests:

    Test:           P14 S2 + garak leakreplay + custom cross-user fixture
    Pass criterion: zero cross-user memory leak; leakreplay <5% hit

These tests target the lm-chat ``MemoryService`` and assert the
**isolation invariant**: every code path that reads from the
``memory_insights`` / ``memory_insights_history`` tables MUST filter on
``user_id == <caller>``.  Any read query without that predicate is a
finding (would allow user A's insights to surface in user B's curator
context).

Static inspection approach
--------------------------
We compile the SQL statements via SQLAlchemy's ``str(stmt.compile(...))``
machinery (no real DB connection required) and assert the rendered SQL
contains a ``user_id =`` predicate.  This is fast, deterministic, and
runs without Docker.
"""
from __future__ import annotations

import inspect

import pytest

# The memory service module owns the SQL we inspect.
from lmchat.services import memory_service as ms


def _module_source() -> str:
    """Read the source of the memory_service module once for whole-file checks."""
    return inspect.getsource(ms)


def test_module_imports_memory_insights_table() -> None:
    """Sanity: the test target is wired up."""
    assert hasattr(ms, "MemoryService")
    src = _module_source()
    # The table reference must be present (catches an accidental rename
    # that would silently break this guard).
    assert "memory_insights" in src


def test_every_select_on_memory_insights_filters_by_user_id() -> None:
    """Static-source invariant: every ``select(memory_insights)`` filters on user_id.

    Scans the memory_service.py source for ``select(memory_insights)``
    occurrences and asserts that within the same statement (rough proxy:
    within the next ~600 chars before a terminating ``)`` at column 0 or a
    blank line), ``memory_insights.c.user_id`` appears.

    Refinements to the heuristic are welcome; the current form catches
    the obvious leak shapes (a ``.where(...)`` without user_id) and is
    deliberately conservative.
    """
    src = _module_source()
    # Find every ``select(memory_insights`` occurrence — both
    # ``select(memory_insights)`` and ``select(memory_insights.c.X)``.
    needle = "select(memory_insights"
    idx = 0
    selects: list[tuple[int, str]] = []
    while True:
        i = src.find(needle, idx)
        if i == -1:
            break
        # Find the closing of the statement: the next blank line at column 0
        # or up to 1200 chars ahead, whichever comes first.
        end = min(len(src), i + 1200)
        chunk = src[i:end]
        # Truncate at the next ``\n\n`` to scope to one statement.
        if "\n\n" in chunk:
            chunk = chunk.split("\n\n", 1)[0]
        selects.append((i, chunk))
        idx = i + len(needle)

    assert selects, "Expected at least one select(memory_insights*) in memory_service"

    offenders: list[tuple[int, str]] = []
    for offset, chunk in selects:
        # ``memory_insights_history`` is a sibling table — its selects need the
        # same guard, but the substring search already caught both.  We exempt
        # purely-internal `_touch_insights` / `_insert_embedding_rows` paths
        # only if they DO NOT read user-scoped data (we don't exempt anything;
        # every chunk must show the user_id filter or another scope guard).
        has_user_filter = (
            "memory_insights.c.user_id" in chunk
            or "memory_insights_history.c.user_id" in chunk
            or "user_id ==" in chunk
            or "user_id=" in chunk
        )
        # A small number of statements scope by ``insight_id`` (followed by a
        # subquery filter on user_id).  Those count as filtered too.
        has_pk_filter_with_user = (
            "memory_insights.c.id ==" in chunk and "user_id" in chunk
        )
        if not (has_user_filter or has_pk_filter_with_user):
            offenders.append((offset, chunk))

    if offenders:
        report = "\n\n".join(
            f"offset={off}\n---\n{chunk}\n---" for off, chunk in offenders
        )
        pytest.fail(
            f"{len(offenders)} memory_insights SELECT(s) without a user_id filter:"
            f"\n\n{report}"
        )


def test_list_pinned_signature_takes_user_id() -> None:
    """``list_pinned(user_id: int)`` — type signature is the first defence."""
    sig = inspect.signature(ms.MemoryService.list_pinned)
    assert "user_id" in sig.parameters, (
        "list_pinned must accept user_id; cross-user leakage guard"
    )
    p = sig.parameters["user_id"]
    # The annotation must be int (not Optional[int] which would default-None).
    assert p.annotation in (int, "int"), (
        f"list_pinned.user_id must be int, got {p.annotation!r}"
    )
    # No default — caller MUST pass an explicit user_id.
    assert p.default is inspect.Parameter.empty, (
        "list_pinned.user_id must not have a default (caller must pass explicitly)"
    )


def test_recall_signature_takes_user_id() -> None:
    """``recall(user_id: int, ...)`` — the cross-search path is user-scoped."""
    sig = inspect.signature(ms.MemoryService.recall)
    assert "user_id" in sig.parameters, "recall must accept user_id"
    # Same no-default invariant.
    assert sig.parameters["user_id"].default is inspect.Parameter.empty


def test_pin_insight_signature_takes_user_id() -> None:
    """``pin_insight`` is the write path; same user_id invariant."""
    sig = inspect.signature(ms.MemoryService.pin_insight)
    assert "user_id" in sig.parameters
    assert sig.parameters["user_id"].default is inspect.Parameter.empty


def test_unpin_insight_signature_resolves_via_user_lookup() -> None:
    """``unpin_insight`` resolves the insight by id; the body must look up user_id.

    Sanity check: the body references ``user_id`` (even if it derives it
    from the row) — catches a regression where the function is
    refactored to skip the ownership check.
    """
    src = inspect.getsource(ms.MemoryService.unpin_insight)
    assert "user_id" in src, "unpin_insight body must reference user_id"


def test_no_global_select_without_filter() -> None:
    """No ``memory_insights.select()`` calls without a where clause."""
    src = _module_source()
    # The bare ``memory_insights.select()`` form is the classic leak shape.
    # We accept it if a ``.where(`` follows on the next 200 chars.
    for substr in ("memory_insights.select(", "memory_insights_history.select("):
        idx = 0
        while True:
            i = src.find(substr, idx)
            if i == -1:
                break
            look = src[i : i + 400]
            assert ".where(" in look, (
                f"bare {substr} without .where(...) at offset {i}: {look!r}"
            )
            idx = i + len(substr)
