# SPDX-License-Identifier: Apache-2.0
"""Contract tests for chat_service.ChatService.compact() (hybrid compaction).

Tests for message compaction: summarize-and-archive, not delete.

Tests cover:
- Oldest-first archive ordering.
- Invariant preservation (system prompt, latest user message, tool-call pairs).
- CompactTooLowError when target_tokens is below invariant minimum.
- Scope guard: a 2nd compact never re-selects the 1st pass's archived rows.
- Archive-not-delete: rows + message_embeddings survive; compaction_id is set.
- Summary-fail ABORTS: nothing is archived when the LLM summary call fails.
- Recall via list_compactions() / get_compaction_messages().
- Audit log row written.
- Tokenizer fallback on KeyError from encoding_for_model.
- No handle_message_deleted notification fires for archived rows (they were
  never deleted).

All tests stub ``ChatService._run_llm_distill`` directly (same pattern this
repo already uses for LM Studio boundary calls in
``test_AC10_generate_title_*`` in ``tests/routes/test_chats.py`` — monkeypatch
the service method rather than faking the httpx transport) so nothing here
depends on a live LM Studio endpoint.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import audit_log, compactions, message_embeddings, messages, metadata
from lmchat.services.chat_service import (
    _COMPACTION_MAX_RUNS_PER_CALL,
    ChatService,
    CompactionSummaryError,
    CompactResult,
    CompactTooLowError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with FK pragmas applied."""
    db_path = tmp_path / "test_compaction.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, user_id: int = 1) -> None:
    """Insert a minimal user row."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"user{user_id}", "ph": "scrypt$dummy"},
        )


async def _insert_message(
    engine: AsyncEngine,
    chat_id: int,
    role: str = "user",
    content: str = "hello",
    response_id: str | None = None,
) -> int:
    """Insert a message row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content, response_id)"
                " VALUES (:cid, :role, :content, :rid)"
            ),
            {"cid": chat_id, "role": role, "content": content, "rid": response_id},
        )
        return result.lastrowid  # type: ignore[return-value]


def _make_service(
    engine: AsyncEngine,
    memory_mock: Any | None = None,
    models_mock: Any | None = None,
    chat_locks: dict[int, asyncio.Lock] | None = None,
) -> ChatService:
    """Build a ChatService with configurable mocks."""
    if memory_mock is None:
        memory_mock = MagicMock()
        memory_mock.handle_message_deleted = AsyncMock(return_value=None)
    if models_mock is None:
        models_mock = MagicMock()
        models_mock.resolve_to_loaded_or_fallback = AsyncMock(
            return_value=MagicMock(wire_id=None)
        )
        models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
        # None of this file's synthetic messages/chats carry a model_id, so
        # the summary-model resolution (chat_service.py's
        # _compact_under_lock) falls through to tier 3 (list_loaded) for
        # every test in this module. Default to a single loaded chat model
        # so existing tests — which only care about archive/invariant
        # behavior, not which model summarized — keep passing unchanged.
        models_mock.list_loaded = AsyncMock(
            return_value=[
                MagicMock(key="qwen-test-7b", type="llm"),
            ]
        )
    return ChatService(
        engine=engine,
        memory_service=memory_mock,
        models_service=models_mock,
        chat_locks=chat_locks if chat_locks is not None else {},
    )


async def _compact(
    svc: ChatService,
    chat_id: int,
    *,
    user_id: int = 1,
    target_tokens: int,
    summary: str = "stub summary of the archived turns",
) -> CompactResult:
    """Call ``svc.compact()`` with the LLM-summary call stubbed.

    Every test that isn't specifically exercising the summarization
    boundary itself routes through this helper so it never depends on a
    live LM Studio endpoint. Mirrors the ``monkeypatch.setattr(chat_svc,
    "generate_title", ...)`` pattern already used for AC10 in
    ``tests/routes/test_chats.py`` — stub the service method, not the
    transport.
    """
    svc._run_llm_distill = AsyncMock(return_value=summary)  # type: ignore[method-assign]
    return await svc.compact(
        chat_id,
        user_id=user_id,
        target_tokens=target_tokens,
        http_client=AsyncMock(),
        base_url="http://lm-studio.test",
    )


# ---------------------------------------------------------------------------
# Compaction tests
# ---------------------------------------------------------------------------


async def test_compaction_drops_oldest_messages_first(engine: AsyncEngine) -> None:
    """compact() archives the oldest messages first to reach the target."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    # Insert messages with known content.
    # "old message one" ~ 3 tokens each; we want to drop old ones first.
    mid1 = await _insert_message(engine, chat.id, role="assistant", content="old message one")
    await _insert_message(engine, chat.id, role="assistant", content="old message two")
    await _insert_message(engine, chat.id, role="assistant", content="old message three")
    mid4 = await _insert_message(engine, chat.id, role="user", content="latest user")

    # Token counts: each "old message *" = 3 tokens, "latest user" = 2.
    # Total = 11. target_tokens=8 forces dropping at least one old message.
    # Invariant minimum = "latest user" (2 tokens) + 10% margin = 2.2, well below 8.
    result = await _compact(svc, chat.id, target_tokens=8)

    assert isinstance(result, CompactResult)
    # mid1 should be archived (oldest).
    assert mid1 in result.removed_message_ids

    # Verify mid4 (latest user) is NOT archived.
    assert mid4 not in result.removed_message_ids

    # Check DB state: ALL rows survive (archive, not delete) — mid1 stays
    # present with compaction_id set, mid4 stays present and active.
    async with engine.connect() as conn:
        surviving = (
            await conn.execute(
                select(messages.c.id, messages.c.compaction_id).where(
                    messages.c.chat_id == chat.id
                )
            )
        ).fetchall()
    surviving_ids = {r.id for r in surviving}
    assert mid1 in surviving_ids
    assert mid4 in surviving_ids
    by_id = {r.id: r.compaction_id for r in surviving}
    assert by_id[mid1] == result.compaction_id
    assert by_id[mid4] is None


async def test_compaction_preserves_system_prompt(engine: AsyncEngine) -> None:
    """compact() never archives the first system-role message."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    sys_id = await _insert_message(engine, chat.id, role="system", content="You are helpful.")
    await _insert_message(engine, chat.id, role="assistant", content="old assistant reply one")
    await _insert_message(engine, chat.id, role="assistant", content="old assistant reply two")
    user_id_msg = await _insert_message(engine, chat.id, role="user", content="latest user msg")

    result = await _compact(svc, chat.id, target_tokens=30)

    assert sys_id not in result.removed_message_ids
    assert user_id_msg not in result.removed_message_ids


async def test_compaction_preserves_latest_user_message(engine: AsyncEngine) -> None:
    """compact() never archives the most recent user-role message."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    await _insert_message(engine, chat.id, role="assistant", content="old a")
    await _insert_message(engine, chat.id, role="assistant", content="old b")
    latest_user = await _insert_message(engine, chat.id, role="user", content="keep this forever")

    result = await _compact(svc, chat.id, target_tokens=10)

    assert latest_user not in result.removed_message_ids

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(messages).where(messages.c.id == latest_user)
            )
        ).fetchone()
    assert row is not None
    assert row.compaction_id is None


async def test_compaction_preserves_tool_call_pairs_atomically(engine: AsyncEngine) -> None:
    """compact() keeps assistant+tool pairs together (never orphans a tool message).

    Also exercises the non-contiguous archive set: the protected pair stays
    active (compaction_id NULL) even when older/newer unrelated messages
    around it get archived, so membership is a set, not an id range.
    """
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    # Old unrelated assistant messages to provide archive candidates.
    await _insert_message(engine, chat.id, role="assistant", content="old msg alpha")
    await _insert_message(engine, chat.id, role="assistant", content="old msg beta")

    # Tool-call pair with shared response_id.
    tool_call_rid = "call_abc123"
    asst_id = await _insert_message(
        engine, chat.id, role="assistant",
        content="I will use tool X",
        response_id=tool_call_rid,
    )
    tool_id = await _insert_message(
        engine, chat.id, role="tool",
        content='{"result": "done"}',
        response_id=tool_call_rid,
    )

    # Latest user message.
    latest = await _insert_message(engine, chat.id, role="user", content="done?")

    result = await _compact(svc, chat.id, target_tokens=30)

    # Neither member of the pair should be in the archive list without the other.
    both_dropped = (
        asst_id in result.removed_message_ids and tool_id in result.removed_message_ids
    )
    both_kept = (
        asst_id not in result.removed_message_ids and tool_id not in result.removed_message_ids
    )
    assert both_dropped or both_kept, (
        "Tool-call pair was split: one member archived without the other"
    )

    # Latest user message never archived.
    assert latest not in result.removed_message_ids

    # If the pair was protected (both kept), it must still be active.
    if both_kept:
        async with engine.connect() as conn:
            pair_rows = (
                await conn.execute(
                    select(messages.c.compaction_id).where(
                        messages.c.id.in_([asst_id, tool_id])
                    )
                )
            ).fetchall()
        assert all(r.compaction_id is None for r in pair_rows)

async def test_compaction_excludes_draft_and_aborted_messages(engine: AsyncEngine) -> None:
    """compact() must never fold non-FINAL messages into the summary/token count.

    A draft (still streaming) or aborted-by-client row is not settled
    conversation content.

    RED-ON-REVERT: the old query selected every active row (chat_id +
    compaction_id IS NULL) regardless of ``state``, so a leftover
    draft/aborted row from an interrupted turn got counted toward
    ``original_token_count`` and was eligible for archiving even though it
    never finished. Reverting the ``state == FINAL`` filter makes the
    ``original_token_count`` assertion below fail (the ~1000-token
    draft/aborted content gets counted).
    """
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    # Same token math as test_compaction_drops_oldest_messages_first: three
    # ~3-token FINAL messages + a ~2-token latest user message (total ~11).
    mid1 = await _insert_message(engine, chat.id, role="assistant", content="old message one")
    await _insert_message(engine, chat.id, role="assistant", content="old message two")
    await _insert_message(engine, chat.id, role="assistant", content="old message three")
    latest = await _insert_message(engine, chat.id, role="user", content="latest user")

    # Leftover DRAFT / ABORTED_BY_CLIENT rows with a LOT of content — if
    # counted, they would dominate token accounting and be archived well
    # before the FINAL rows above are even considered.
    async with engine.begin() as conn:
        draft_id = (
            await conn.execute(
                insert(messages).values(
                    chat_id=chat.id,
                    role="assistant",
                    content="x " * 500,
                    state="draft",
                )
            )
        ).inserted_primary_key[0]  # type: ignore[index]
        aborted_id = (
            await conn.execute(
                insert(messages).values(
                    chat_id=chat.id,
                    role="assistant",
                    content="y " * 500,
                    state="aborted_by_client",
                )
            )
        ).inserted_primary_key[0]  # type: ignore[index]

    result = await _compact(svc, chat.id, target_tokens=8)

    # Same outcome as the FINAL-only baseline test — the draft/aborted rows
    # must not shift which FINAL messages get archived.
    assert mid1 in result.removed_message_ids
    assert latest not in result.removed_message_ids

    # The draft/aborted rows were never candidates — not archived, not
    # touched at all.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.id, messages.c.state, messages.c.compaction_id).where(
                    messages.c.id.in_([draft_id, aborted_id])
                )
            )
        ).fetchall()
    by_id = {r.id: r for r in rows}
    assert by_id[draft_id].state == "draft"
    assert by_id[draft_id].compaction_id is None
    assert by_id[aborted_id].state == "aborted_by_client"
    assert by_id[aborted_id].compaction_id is None

    # original_token_count reflects ONLY the FINAL messages (~11 tokens) —
    # proves the ~1000-token draft/aborted content was excluded from
    # accounting, not just from the archive selection.
    assert result.original_token_count < 50, (
        f"draft/aborted content leaked into token accounting: "
        f"{result.original_token_count}"
    )


async def test_compaction_splits_non_contiguous_archive_into_ordered_spans(
    engine: AsyncEngine,
) -> None:
    """Archived content on BOTH SIDES of a retained tool-call pair must land
    in two separately-anchored compaction rows, not one row anchored at the
    overall min(id).

    RED-ON-REVERT: the old code wrote exactly ONE ``compactions`` row per
    ``compact()`` call, with ``anchor_msg_id=drop_ids[0]`` — the archive
    set's overall min id — and one ``_run_llm_distill`` call over the whole
    (non-contiguous) archive set. When the archive set straddles a retained
    pair (content both before AND after it), that single anchor places ALL
    archived content — including the later span, which chronologically
    comes AFTER the pair — before the pair, inverting order relative to
    what's retained. Reverting the fix makes ``list_compactions()`` return
    ONE row instead of two, and ``_run_llm_distill`` gets called once (with
    all four archived rows) instead of twice.
    """
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    alpha = await _insert_message(engine, chat.id, role="assistant", content="old alpha reply")

    tool_call_rid = "call_span_test"
    asst_id = await _insert_message(
        engine, chat.id, role="assistant",
        content="I will check something",
        response_id=tool_call_rid,
    )
    tool_id = await _insert_message(
        engine, chat.id, role="tool",
        content='{"result": "ok"}',
        response_id=tool_call_rid,
    )

    beta1 = await _insert_message(engine, chat.id, role="assistant", content="old beta reply one")
    beta2 = await _insert_message(engine, chat.id, role="assistant", content="old beta reply two")

    latest = await _insert_message(engine, chat.id, role="user", content="latest question")

    # Distinct per-run summaries (keyed off the archive_rows actually
    # passed) so each row's provenance is independently verifiable —
    # bypasses the shared ``_compact()`` helper's single fixed-text stub.
    calls: list[list[int]] = []

    async def _distill(*, archive_rows: list[Any], **_kwargs: Any) -> str:
        ids = [r.id for r in archive_rows]
        calls.append(ids)
        return f"summary of {ids}"

    svc._run_llm_distill = AsyncMock(side_effect=_distill)  # type: ignore[method-assign]

    # Low enough to force the walk past the protected pair and archive both
    # alpha AND beta1+beta2, but still above the invariant minimum (pair +
    # latest user = 12 tokens, ~13 with the 10% margin) so compact() doesn't
    # reject it outright.
    result = await svc.compact(
        chat.id,
        user_id=1,
        target_tokens=14,
        http_client=AsyncMock(),
        base_url="http://lm-studio.test",
    )

    # Both spans were actually archived, the pair + latest user were not —
    # the scenario this test needs.
    assert alpha in result.removed_message_ids
    assert beta1 in result.removed_message_ids
    assert beta2 in result.removed_message_ids
    assert asst_id not in result.removed_message_ids
    assert tool_id not in result.removed_message_ids
    assert latest not in result.removed_message_ids

    # TWO separate per-run summarize calls, not one call over the whole set.
    assert len(calls) == 2, f"expected 2 per-run summarize calls, got {calls}"
    assert calls[0] == [alpha]
    assert calls[1] == [beta1, beta2]

    # TWO compactions rows, correctly ordered/anchored: span 1 (alpha)
    # before span 2 (beta1, beta2) — anchor_msg_id ascending.
    spans = await svc.list_compactions(chat.id, user_id=1)
    assert len(spans) == 2, f"expected 2 compaction spans, got {len(spans)}"
    assert spans[0].anchor_msg_id == alpha
    assert spans[1].anchor_msg_id == beta1
    assert spans[0].archived_count == 1
    assert spans[1].archived_count == 2

    # Message -> compaction_id mapping matches the correct span.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.id, messages.c.compaction_id).where(
                    messages.c.id.in_([alpha, beta1, beta2])
                )
            )
        ).fetchall()
    by_id = {r.id: r.compaction_id for r in rows}
    assert by_id[alpha] == spans[0].id
    assert by_id[beta1] == spans[1].id
    assert by_id[beta2] == spans[1].id


async def test_compaction_bounds_run_count_and_leaves_excess_scattered_runs_live(
    engine: AsyncEngine,
) -> None:
    """Many SCATTERED retained tool-call pairs must not turn one compact()
    call into an unbounded number of summarizer LLM calls.

    Ten alternating (droppable reply, protected tool-call pair) units force
    the archive-candidate walk to produce TEN singleton runs (each reply is
    isolated by the two protected messages on either side of it — see the
    run-splitting comment in ``_compact_under_lock``). Without a cap, that
    is 10 separate ``_run_llm_distill`` calls for one ``/compact`` request,
    each requesting its own reasoning-headroom budget, and 10 pointless
    ``compactions`` rows for singleton spans that can't meaningfully shrink.

    RED-ON-REVERT: before the run cap, this test's archive set produces 10
    runs, 10 ``_run_llm_distill`` calls, and 10 ``compactions`` rows — all
    of the scattered replies get archived. With the cap
    (``_COMPACTION_MAX_RUNS_PER_CALL``), only the first N (oldest-anchored)
    runs are summarized + archived; the rest are left as live messages for
    a later ``/compact`` call to pick up.
    """
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    n_units = 10
    reply_ids: list[int] = []
    for i in range(n_units):
        reply_ids.append(
            await _insert_message(
                engine, chat.id, role="assistant", content=f"old reply {i}"
            )
        )
        rid = f"call_{i}"
        await _insert_message(
            engine, chat.id, role="assistant",
            content=f"checking thing {i}", response_id=rid,
        )
        await _insert_message(
            engine, chat.id, role="tool",
            content='{"result": "ok"}', response_id=rid,
        )

    await _insert_message(engine, chat.id, role="user", content="latest question")

    calls: list[list[int]] = []

    async def _distill(*, archive_rows: list[Any], **_kwargs: Any) -> str:
        calls.append([r.id for r in archive_rows])
        return f"summary of {[r.id for r in archive_rows]}"

    svc._run_llm_distill = AsyncMock(side_effect=_distill)  # type: ignore[method-assign]

    # Each "old reply {i}" / "checking thing {i}" is 4 cl100k_base tokens,
    # the tool response is 6, and "latest question" is 2 — 10 * (4+4+6) + 2
    # = 142 tokens total, of which 10 * 4 = 40 is droppable (the replies)
    # and 102 is invariant-protected (the 10 tool-call pairs + latest_user).
    # target_tokens=113 sits in the narrow window that (a) clears the
    # invariant floor (102 * 1.10 = 112.2) and (b) keeps
    # target_with_margin (113 * 0.9 = 101.7 -> 101) below the protected
    # floor (102), so the walk never breaks early and archives every one
    # of the 10 replies before hitting a protected message it can't drop.
    result = await svc.compact(
        chat.id,
        user_id=1,
        target_tokens=113,
        http_client=AsyncMock(),
        base_url="http://lm-studio.test",
    )

    # Bounded: at most _COMPACTION_MAX_RUNS_PER_CALL LLM calls / spans, not
    # one per scattered run.
    assert len(calls) == _COMPACTION_MAX_RUNS_PER_CALL, (
        f"expected {_COMPACTION_MAX_RUNS_PER_CALL} bounded LLM calls, got {len(calls)}"
    )
    assert all(len(c) == 1 for c in calls), "every kept run is a singleton"

    spans = await svc.list_compactions(chat.id, user_id=1)
    assert len(spans) == _COMPACTION_MAX_RUNS_PER_CALL
    assert len(result.compaction_ids) == _COMPACTION_MAX_RUNS_PER_CALL

    # Oldest-anchored runs win: the first _COMPACTION_MAX_RUNS_PER_CALL
    # replies were archived, the rest were left live.
    kept = reply_ids[:_COMPACTION_MAX_RUNS_PER_CALL]
    left_live = reply_ids[_COMPACTION_MAX_RUNS_PER_CALL:]
    assert set(result.removed_message_ids) == set(kept)
    assert len(left_live) == n_units - _COMPACTION_MAX_RUNS_PER_CALL

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.id, messages.c.compaction_id).where(
                    messages.c.id.in_(left_live)
                )
            )
        ).fetchall()
    assert all(r.compaction_id is None for r in rows), (
        "excess scattered runs must stay live, not be pointlessly summarized"
    )


async def test_compact_result_reports_all_span_ids_for_multi_run_call(
    engine: AsyncEngine,
) -> None:
    """CompactResult.compaction_ids carries every row a multi-run compact()
    call wrote, not just the single most-recent id the pre-existing
    ``compaction_id`` field reports.

    Reuses the same non-contiguous-archive fixture as
    ``test_compaction_splits_non_contiguous_archive_into_ordered_spans``
    (already proven to produce exactly 2 compaction rows at
    target_tokens=14) so this test's own numbers don't need re-deriving;
    its focus is the *result contract*, not the archive-splitting logic.

    RED-ON-REVERT: before this fix, ``CompactResult`` had no
    ``compaction_ids`` field — a multi-span call already wrote N rows to
    ``compactions``, but the wire contract only ever exposed the most
    recent one (``compaction_id``), silently dropping the rest. This pins
    the multi-row contract: ``compaction_ids`` must list every row this
    call wrote, oldest-anchor first, with the last entry equal to the
    unchanged (backward-compat) singular ``compaction_id``.
    """
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    alpha = await _insert_message(engine, chat.id, role="assistant", content="old alpha reply")

    tool_call_rid = "call_contract_test"
    await _insert_message(
        engine, chat.id, role="assistant",
        content="I will check something",
        response_id=tool_call_rid,
    )
    await _insert_message(
        engine, chat.id, role="tool",
        content='{"result": "ok"}',
        response_id=tool_call_rid,
    )

    beta1 = await _insert_message(engine, chat.id, role="assistant", content="old beta reply one")
    beta2 = await _insert_message(engine, chat.id, role="assistant", content="old beta reply two")

    await _insert_message(engine, chat.id, role="user", content="latest question")

    svc._run_llm_distill = AsyncMock(return_value="stub summary")  # type: ignore[method-assign]

    result = await svc.compact(
        chat.id,
        user_id=1,
        target_tokens=14,
        http_client=AsyncMock(),
        base_url="http://lm-studio.test",
    )

    assert alpha in result.removed_message_ids
    assert beta1 in result.removed_message_ids
    assert beta2 in result.removed_message_ids

    spans = await svc.list_compactions(chat.id, user_id=1)
    assert len(spans) == 2, f"expected 2 compaction spans, got {len(spans)}"

    assert result.compaction_ids == [spans[0].id, spans[1].id]
    assert result.compaction_id == result.compaction_ids[-1]


async def test_compaction_below_invariant_token_count_raises_CompactTooLowError(
    engine: AsyncEngine,
) -> None:
    """compact() raises CompactTooLowError when target_tokens is below invariant minimum."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(
        engine, chat.id, role="system", content="You are a very helpful assistant."
    )
    await _insert_message(
        engine, chat.id, role="user",
        content="This is a long user question that needs tokens.",
    )

    # target_tokens = 1 is certainly below any real invariant minimum.
    with pytest.raises(CompactTooLowError):
        await _compact(svc, chat.id, target_tokens=1)


async def test_compaction_does_not_call_handle_message_deleted(
    engine: AsyncEngine,
) -> None:
    """compact() archives — it never calls handle_message_deleted.

    Regression guard distinguishing archive-semantics from the old
    delete-semantics: archived rows are NOT gone, so the delete-
    notification contract must not fire for them.
    """
    await _insert_user(engine)

    memory_mock = MagicMock()
    memory_mock.handle_message_deleted = AsyncMock(return_value=None)
    models_mock = MagicMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=MagicMock(wire_id=None)
    )
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
    models_mock.list_loaded = AsyncMock(
        return_value=[MagicMock(key="qwen-test-7b", type="llm")]
    )

    svc = _make_service(engine, memory_mock=memory_mock, models_mock=models_mock)

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(engine, chat.id, role="assistant", content="old message to drop alpha")
    await _insert_message(engine, chat.id, role="assistant", content="old message to drop beta")
    await _insert_message(engine, chat.id, role="user", content="latest user msg")

    # total ~13 tokens; target=6 forces archiving both old messages while
    # keeping the invariant-protected latest user message.
    result = await _compact(svc, chat.id, target_tokens=6)

    assert result.removed_message_ids, "expected something to be archived"
    memory_mock.handle_message_deleted.assert_not_called()


async def test_compaction_audit_log_row_emitted(engine: AsyncEngine) -> None:
    """compact() writes a chat.compacted row to audit_log."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(engine, chat.id, role="assistant", content="old one")
    await _insert_message(engine, chat.id, role="assistant", content="old two")
    await _insert_message(engine, chat.id, role="user", content="latest user")

    # total = 6 tokens; target=4 forces dropping old messages while keeping latest user
    await _compact(svc, chat.id, target_tokens=4)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(audit_log).where(audit_log.c.event == "chat.compacted")
            )
        ).fetchall()

    assert len(rows) >= 1


async def test_compaction_tokenizer_fallback_on_keyerror(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compact() falls back to cl100k_base and logs WARNING when encoding_for_model raises.

    Spies on ``chat_service.log.warning`` directly via monkeypatch.setattr
    rather than going through caplog/structlog/stdlib plumbing. This is
    order-independent because the spy intercepts at the BoundLogger level,
    bypassing the entire stdlib-routing chain that can be poisoned by other
    tests' configure_logging calls. Same pattern as
    tests/services/test_single_session_warning.py.
    """
    from lmchat.services import chat_service as chat_service_mod

    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(engine, chat.id, role="assistant", content="old msg")
    await _insert_message(engine, chat.id, role="user", content="latest")

    captured_events: list[str] = []
    original_warning = chat_service_mod.log.warning

    def _spy_warning(event: str, **kwargs: object) -> None:
        captured_events.append(event)
        original_warning(event, **kwargs)

    monkeypatch.setattr(chat_service_mod.log, "warning", _spy_warning)

    # Patch tiktoken.encoding_for_model to raise KeyError.
    with patch(
        "lmchat.services.chat_service.tiktoken.encoding_for_model",
        side_effect=KeyError("unknown model"),
    ):
        result = await _compact(svc, chat.id, target_tokens=100)

    assert isinstance(result, CompactResult)
    assert "tokenizer.fallback" in captured_events, (
        f"Expected 'tokenizer.fallback' in captured warnings but got:\n"
        f"{captured_events}"
    )


# ---------------------------------------------------------------------------
# Hybrid compaction: scope guard + archive-not-delete + summary-fail abort
# ---------------------------------------------------------------------------


async def test_second_compact_excludes_first_pass_archived_rows(
    engine: AsyncEngine,
) -> None:
    """A 2nd /compact never re-selects or re-counts the 1st pass's archived rows."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    # First batch: 3 old assistant turns + 1 user turn.
    await _insert_message(engine, chat.id, role="assistant", content="alpha reply one two three")
    await _insert_message(engine, chat.id, role="assistant", content="beta reply one two three")
    await _insert_message(engine, chat.id, role="assistant", content="gamma reply one two three")
    await _insert_message(engine, chat.id, role="user", content="first question here now")

    result1 = await _compact(svc, chat.id, target_tokens=8)
    assert result1.removed_message_ids, "first pass should have archived something"
    assert result1.compaction_id is not None
    first_archived = set(result1.removed_message_ids)

    # Archived rows carry the new compaction_id.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.id, messages.c.compaction_id).where(
                    messages.c.id.in_(first_archived)
                )
            )
        ).fetchall()
    assert all(r.compaction_id == result1.compaction_id for r in rows)

    # Second batch: more turns after the first compaction.
    await _insert_message(engine, chat.id, role="assistant", content="delta reply one two three")
    await _insert_message(engine, chat.id, role="assistant", content="epsilon reply one two")
    await _insert_message(engine, chat.id, role="user", content="second question here now")

    result2 = await _compact(svc, chat.id, target_tokens=8)
    assert result2.removed_message_ids, "second pass should have archived something too"
    second_archived = set(result2.removed_message_ids)

    # Second pass must not re-touch first pass's archived rows.
    assert not (second_archived & first_archived), (
        "2nd compact re-selected the 1st pass's archived rows"
    )
    assert result2.compaction_id != result1.compaction_id

    # original_token_count for pass 2 must exclude the 1st-pass archived
    # rows' tokens — it's computed only over the still-active SELECT.
    async with engine.connect() as conn:
        active_ids = {
            r[0]
            for r in (
                await conn.execute(
                    select(messages.c.id).where(
                        messages.c.chat_id == chat.id,
                        messages.c.compaction_id.is_(None),
                    )
                )
            ).fetchall()
        }
    assert not (active_ids & first_archived)

    # 1st-pass archived rows retain their ORIGINAL compaction_id — not
    # reassigned to the 2nd compaction.
    async with engine.connect() as conn:
        rows_after = (
            await conn.execute(
                select(messages.c.id, messages.c.compaction_id).where(
                    messages.c.id.in_(first_archived)
                )
            )
        ).fetchall()
    assert all(r.compaction_id == result1.compaction_id for r in rows_after)


async def test_compaction_archives_not_deletes_and_retains_embeddings(
    engine: AsyncEngine,
) -> None:
    """compact() archives (rows retained, compaction_id set) — never deletes.

    Also asserts message_embeddings survive: the archive path must not
    cascade-drop them (they stay retained for semantic recall).
    """
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    old_id = await _insert_message(
        engine, chat.id, role="assistant", content="old msg one two three"
    )
    await _insert_message(engine, chat.id, role="assistant", content="old msg two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here now")

    # Attach an embedding to the oldest message BEFORE compacting.
    async with engine.begin() as conn:
        await conn.execute(
            insert(message_embeddings).values(
                message_id=old_id,
                embedding_model_id="test-embed-model",
                embedding=b"\x00" * 12,
                text_hash="a" * 64,
            )
        )

    result = await _compact(svc, chat.id, target_tokens=6)
    assert old_id in result.removed_message_ids
    assert result.compaction_id is not None
    assert result.summary == "stub summary of the archived turns"
    assert result.archived_count == len(result.removed_message_ids)
    assert result.summary_token_count > 0

    # Row still present, with compaction_id set (archived, not deleted).
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.id == old_id))
        ).fetchone()
    assert row is not None
    assert row.compaction_id == result.compaction_id

    # Embedding survives — NOT cascade-dropped.
    async with engine.connect() as conn:
        emb_row = (
            await conn.execute(
                select(message_embeddings).where(
                    message_embeddings.c.message_id == old_id
                )
            )
        ).fetchone()
    assert emb_row is not None, "embedding must survive archiving (not cascade-deleted)"

    # The compactions row itself carries the expected fields.
    async with engine.connect() as conn:
        crow = (
            await conn.execute(
                select(compactions).where(compactions.c.id == result.compaction_id)
            )
        ).fetchone()
    assert crow is not None
    assert crow.chat_id == chat.id
    assert crow.summary == result.summary
    assert crow.anchor_msg_id == min(result.removed_message_ids)


async def test_compact_summary_failure_aborts_and_archives_nothing(
    engine: AsyncEngine,
) -> None:
    """Summary-fail policy = ABORT: nothing is archived when the LLM call fails."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    await _insert_message(engine, chat.id, role="assistant", content="old msg one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old msg two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here now")

    svc._run_llm_distill = AsyncMock(  # type: ignore[method-assign]
        side_effect=CompactionSummaryError("upstream failed")
    )

    with pytest.raises(CompactionSummaryError):
        await svc.compact(
            chat.id,
            user_id=1,
            target_tokens=6,
            http_client=AsyncMock(),
            base_url="http://lm-studio.test",
        )

    # Nothing archived — every message still active.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.compaction_id).where(messages.c.chat_id == chat.id)
            )
        ).fetchall()
    assert all(r.compaction_id is None for r in rows)

    # No compactions row was written.
    async with engine.connect() as conn:
        comp_rows = (
            await conn.execute(
                select(compactions).where(compactions.c.chat_id == chat.id)
            )
        ).fetchall()
    assert comp_rows == []


# ---------------------------------------------------------------------------
# Summary-model resolution: decoupled from the tokenizer `hint`.
#
# Regression guard for the bug where a chat/message set with NO model_id
# anywhere left `hint` at its tokenizer-encoding default ("cl100k_base"),
# which was then passed straight through as the LM Studio `model` field on
# the summarization call -> a 400 "Invalid model identifier" from LM Studio,
# surfaced to the caller as a 502. The summary model must resolve
# independently (latest message model_id -> chat.model_id -> first
# non-embedding loaded model -> CompactionSummaryError), never falling back
# to the tokenizer encoding name.
# ---------------------------------------------------------------------------


async def test_compaction_summary_model_resolves_from_loaded_models_when_no_message_model_id(
    engine: AsyncEngine,
) -> None:
    """No message/chat model_id anywhere -> summary model comes from list_loaded(),
    not the tokenizer hint/encoding name.
    """
    await _insert_user(engine)

    models_mock = MagicMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=MagicMock(wire_id=None)
    )
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
    models_mock.list_loaded = AsyncMock(
        return_value=[
            MagicMock(key="text-embed-3-small", type="embedding"),
            MagicMock(key="qwen3-32b-instruct", type="llm"),
        ]
    )
    svc = _make_service(engine, models_mock=models_mock)

    # No model_id passed at chat-creation -> chats.model_id stays NULL.
    chat = await svc.create(user_id=1, title="Chat")
    # _insert_message never sets model_id -> every message.model_id is NULL,
    # so tiers 1 (message scan) and 2 (chat.model_id) both miss and tier 3
    # (list_loaded) must resolve the summary model.
    await _insert_message(engine, chat.id, role="assistant", content="old msg one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old msg two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here now")

    svc._run_llm_distill = AsyncMock(return_value="a running summary")  # type: ignore[method-assign]

    result = await svc.compact(
        chat.id,
        user_id=1,
        target_tokens=6,
        http_client=AsyncMock(),
        base_url="http://lm-studio.test",
    )

    assert result.removed_message_ids, "expected something archived"
    svc._run_llm_distill.assert_awaited_once()
    await_args = svc._run_llm_distill.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    # The embedding model must be skipped; the loaded LLM wins.
    assert call_kwargs["model_id"] == "qwen3-32b-instruct"
    assert call_kwargs["model_id"] != "cl100k_base"

    # The resolved (non-tokenizer) model id is what gets persisted, too.
    async with engine.connect() as conn:
        crow = (
            await conn.execute(
                select(compactions).where(compactions.c.id == result.compaction_id)
            )
        ).fetchone()
    assert crow is not None
    assert crow.summary_model_id == "qwen3-32b-instruct"


async def test_compaction_raises_CompactionSummaryError_when_only_embedding_models_loaded(
    engine: AsyncEngine,
) -> None:
    """No message/chat model_id + list_loaded() has only embedding models ->
    compact() raises CompactionSummaryError (not an opaque LM Studio 400/502)
    and archives nothing.
    """
    await _insert_user(engine)

    models_mock = MagicMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=MagicMock(wire_id=None)
    )
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
    models_mock.list_loaded = AsyncMock(
        return_value=[MagicMock(key="text-embed-3-small", type="embedding")]
    )
    svc = _make_service(engine, models_mock=models_mock)

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(engine, chat.id, role="assistant", content="old msg one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old msg two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here now")

    svc._run_llm_distill = AsyncMock(return_value="should never be reached")  # type: ignore[method-assign]

    with pytest.raises(CompactionSummaryError):
        await svc.compact(
            chat.id,
            user_id=1,
            target_tokens=6,
            http_client=AsyncMock(),
            base_url="http://lm-studio.test",
        )

    svc._run_llm_distill.assert_not_awaited()

    # Nothing archived — every message still active.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.compaction_id).where(messages.c.chat_id == chat.id)
            )
        ).fetchall()
    assert all(r.compaction_id is None for r in rows)

    # No compactions row was written.
    async with engine.connect() as conn:
        comp_rows = (
            await conn.execute(
                select(compactions).where(compactions.c.chat_id == chat.id)
            )
        ).fetchall()
    assert comp_rows == []


async def test_compaction_raises_CompactionSummaryError_when_no_models_loaded(
    engine: AsyncEngine,
) -> None:
    """No message/chat model_id + list_loaded() returns an empty list ->
    CompactionSummaryError, not an opaque LM Studio 400/502.
    """
    await _insert_user(engine)

    models_mock = MagicMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=MagicMock(wire_id=None)
    )
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
    models_mock.list_loaded = AsyncMock(return_value=[])
    svc = _make_service(engine, models_mock=models_mock)

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(engine, chat.id, role="assistant", content="old msg one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old msg two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here now")

    svc._run_llm_distill = AsyncMock(return_value="should never be reached")  # type: ignore[method-assign]

    with pytest.raises(CompactionSummaryError):
        await svc.compact(
            chat.id,
            user_id=1,
            target_tokens=6,
            http_client=AsyncMock(),
            base_url="http://lm-studio.test",
        )

    svc._run_llm_distill.assert_not_awaited()

    async with engine.connect() as conn:
        comp_rows = (
            await conn.execute(
                select(compactions).where(compactions.c.chat_id == chat.id)
            )
        ).fetchall()
    assert comp_rows == []


async def test_list_compactions_and_get_compaction_messages(
    engine: AsyncEngine,
) -> None:
    """list_compactions() + get_compaction_messages() recall the archived span."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    await _insert_message(engine, chat.id, role="assistant", content="old msg one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old msg two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here now")

    result = await _compact(svc, chat.id, target_tokens=6)
    assert result.compaction_id is not None

    spans = await svc.list_compactions(chat.id, user_id=1)
    assert len(spans) == 1
    assert spans[0].id == result.compaction_id
    assert spans[0].summary == result.summary
    assert spans[0].chat_id == chat.id
    # archived_count is derived from live membership
    # (COUNT messages WHERE compaction_id = this span's id), not a stored
    # number — must equal exactly what compact() archived.
    assert spans[0].archived_count == len(result.removed_message_ids)

    archived_msgs = await svc.get_compaction_messages(
        chat.id, result.compaction_id, user_id=1
    )
    assert {m.id for m in archived_msgs} == set(result.removed_message_ids)
    # id-ordered.
    ids = [m.id for m in archived_msgs]
    assert ids == sorted(ids)


async def test_list_compactions_archived_count_reflects_live_membership_not_original_count(
    engine: AsyncEngine,
) -> None:
    """archived_count is derived per-call, not a stored/frozen number.

    Membership is the set of ``messages`` rows whose ``compaction_id``
    equals the span's id — possibly non-contiguous, and it can shrink after
    the fact (e.g. a fork remap moves a row to a new compaction, or a
    protected message is repaired back to active). This directly
    constructs a non-contiguous span (m1, m3 archived; m2 in the middle
    left active) to prove the count is a live COUNT(*), not
    ``len(removed_message_ids)`` frozen at archive time.
    """
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    m1 = await _insert_message(engine, chat.id, role="assistant", content="one")
    m2 = await _insert_message(engine, chat.id, role="assistant", content="two")
    m3 = await _insert_message(engine, chat.id, role="assistant", content="three")
    await _insert_message(engine, chat.id, role="user", content="latest")

    # Build a compaction span covering m1..m3 directly (bypassing compact()
    # so the archived set is fully test-controlled).
    async with engine.begin() as conn:
        pk = (
            await conn.execute(
                insert(compactions).values(
                    chat_id=chat.id,
                    summary="stub summary",
                    summary_model_id=None,
                    anchor_msg_id=m1,
                    original_token_count=30,
                    summary_token_count=5,
                )
            )
        ).inserted_primary_key
        assert pk is not None
        compaction_id = int(pk[0])
        await conn.execute(
            update(messages)
            .where(messages.c.id.in_([m1, m2, m3]))
            .values(compaction_id=compaction_id)
        )

    spans = await svc.list_compactions(chat.id, user_id=1)
    assert len(spans) == 1
    assert spans[0].archived_count == 3

    # m2 (the middle of the id range) leaves the span's membership — the
    # archived set is now non-contiguous (m1, m3) by id.
    async with engine.begin() as conn:
        await conn.execute(
            update(messages).where(messages.c.id == m2).values(compaction_id=None)
        )

    spans_after = await svc.list_compactions(chat.id, user_id=1)
    assert len(spans_after) == 1
    assert spans_after[0].archived_count == 2


# ---------------------------------------------------------------------------
# _run_llm_distill — direct unit tests
# ---------------------------------------------------------------------------
# Every test above stubs _run_llm_distill itself, so it never exercises its
# real HTTP-calling body. These tests call it directly with a fake httpx
# client to cover that boundary in isolation.


async def test_run_llm_distill_happy_path_posts_and_parses_response(
    engine: AsyncEngine,
) -> None:
    """_run_llm_distill POSTs to /v1/chat/completions and returns stripped content."""
    await _insert_user(engine)
    svc = _make_service(engine)

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "  A tidy running summary.  "}}]
    }
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=fake_response)

    row = MagicMock(role="assistant", content="Some old turn content.")
    summary = await svc._run_llm_distill(
        http_client=http_client,
        base_url="http://lm-studio.test/",
        model_id="qwen-test",
        archive_rows=[row],
        max_tokens=64,
    )

    assert summary == "A tidy running summary."
    http_client.post.assert_awaited_once()
    call = http_client.post.await_args
    # base_url trailing slash is stripped before appending the path.
    assert call.args[0] == "http://lm-studio.test/v1/chat/completions"
    body = call.kwargs["json"]
    assert body["model"] == "qwen-test"
    assert body["stream"] is False
    assert any(
        "ASSISTANT: Some old turn content." in m["content"] for m in body["messages"]
    )


async def test_run_llm_distill_non_200_raises_CompactionSummaryError(
    engine: AsyncEngine,
) -> None:
    """A non-200 upstream response raises CompactionSummaryError."""
    await _insert_user(engine)
    svc = _make_service(engine)

    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.text = "internal server error"
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=fake_response)

    row = MagicMock(role="user", content="hi there")
    with pytest.raises(CompactionSummaryError):
        await svc._run_llm_distill(
            http_client=http_client,
            base_url="http://lm-studio.test",
            model_id="m",
            archive_rows=[row],
            max_tokens=64,
        )


async def test_run_llm_distill_empty_content_raises_CompactionSummaryError(
    engine: AsyncEngine,
) -> None:
    """A 200 response with empty/whitespace content raises CompactionSummaryError."""
    await _insert_user(engine)
    svc = _make_service(engine)

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"choices": [{"message": {"content": "   "}}]}
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=fake_response)

    row = MagicMock(role="user", content="hi there")
    with pytest.raises(CompactionSummaryError):
        await svc._run_llm_distill(
            http_client=http_client,
            base_url="http://lm-studio.test",
            model_id="m",
            archive_rows=[row],
            max_tokens=64,
        )


async def test_run_llm_distill_network_error_raises_CompactionSummaryError(
    engine: AsyncEngine,
) -> None:
    """A transport-level failure (timeout/connect error) raises CompactionSummaryError."""
    await _insert_user(engine)
    svc = _make_service(engine)

    http_client = AsyncMock()
    http_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    row = MagicMock(role="user", content="hi there")
    with pytest.raises(CompactionSummaryError):
        await svc._run_llm_distill(
            http_client=http_client,
            base_url="http://lm-studio.test",
            model_id="m",
            archive_rows=[row],
            max_tokens=64,
        )


async def test_run_llm_distill_no_summarizable_content_raises_CompactionSummaryError(
    engine: AsyncEngine,
) -> None:
    """An archive set with only blank content raises before ever calling out."""
    await _insert_user(engine)
    svc = _make_service(engine)

    http_client = AsyncMock()
    row = MagicMock(role="user", content="")
    with pytest.raises(CompactionSummaryError):
        await svc._run_llm_distill(
            http_client=http_client,
            base_url="http://lm-studio.test",
            model_id="m",
            archive_rows=[row],
            max_tokens=64,
        )
    http_client.post.assert_not_awaited()
