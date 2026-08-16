/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Characterization tests — tool_call.start fold divergence (fe-components-state-9).
 *
 * Three independent SSE readers (useSSE, useABStream, useSubSessionSSE) evolved
 * separately. This file pins what EACH does when a `tool_call.start` /
 * `sub.tool_call.start` frame arrives twice for the SAME call id — the exact
 * scenario a reconnect, a retried decoder emission, or a BE resend could
 * trigger.
 *
 * ORIGINAL findings (pre-fix — this is the RED/GREEN anchor the
 * fold-reconciliation step was verified against):
 *   - useSSE.ts's `tool_call.start` case unconditionally APPENDED a new
 *     ToolCall — a repeated start for the same id produced a DUPLICATE card.
 *   - useSubSessionSSE.ts's `sub.tool_call.start` branch upserted by id (found
 *     the existing index and replaced it in place) — a repeated start did
 *     NOT duplicate.
 *   - The backend's own persistence fold — `_accumulate_tool_call` in
 *     src/lmchat/services/streaming_service.py — upserts by id too: on
 *     `tool_call.start`, it only appends `if entry is None`; a second start
 *     for an id already in the list is a no-op. Its docstring states it
 *     "Mirrors the FE's live accumulator in useSSE.ts" — meaning useSSE's
 *     blind-append was a REGRESSION from its own documented mirror, not
 *     intentional behavior. This is what settled the fold-reconciliation
 *     call: upsert-by-id is provably the correct semantics for BOTH hooks,
 *     not a guess.
 *
 * FIX (fold reconciliation): both hooks now share one `foldToolCallStart`
 * (web/src/lib/sseStream.ts) which upserts by id. The "useSSE duplicates"
 * test below was UPDATED (not deleted) to assert the new unified behavior —
 * see the inline comment on that test for the exact rationale. Mutation
 * check: temporarily reverting `foldToolCallStart` to a blind append
 * reproduces a 2-card failure on BOTH tests in this file (useSSE AND
 * useSubSessionSSE now share the one fold, so breaking it breaks both) —
 * confirmed, then reverted.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSSE } from "@/hooks/useSSE";
import { useSubSessionSSE } from "@/hooks/useSubSessionSSE";
import type { SubSessionStreamParams } from "@/hooks/useSubSessionSSE";

// ─── useSSE helpers (main-chat wire: nested `tool_call` payload) ────────────

function sseResponse(frames: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(frames));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function frame(type: string, data: Record<string, unknown>): string {
  return `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
}

class StubChannel {
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  postMessage() {
    /* no-op */
  }
  close() {
    /* no-op */
  }
}

// ─── useSubSessionSSE helpers (sub-session wire: flat fields) ───────────────

const encoder = new TextEncoder();

function chunkedSseResponse(chunks: string[]): Response {
  let idx = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const chunk = chunks[idx++];
      if (chunk !== undefined) {
        controller.enqueue(encoder.encode(chunk));
      } else {
        controller.close();
      }
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

const BASE_PARAMS: SubSessionStreamParams = {
  chatId: 1,
  modelId: "test-model",
  systemPrompt: "You are helpful.",
  messages: [],
};

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("tool_call.start fold — CURRENT (pre-fix) divergence between hooks", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // @ts-expect-error -- stub BroadcastChannel globally
    global.BroadcastChannel = StubChannel;
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // @ts-expect-error -- restore
    delete global.BroadcastChannel;
  });

  it("useSSE: a repeated tool_call.start for the SAME id UPSERTS in place (fixed — was a pinned duplicate bug)", async () => {
    // FIX (fold reconciliation, fe-components-state-9): this test used to
    // pin useSSE's tool_call.start blindly APPENDING a new card on a
    // repeated start for the same id (2 duplicate cards). useSSE's own
    // doc comment claims its fold "mirrors the BE persistence accumulator"
    // (streaming_service._accumulate_tool_call), and that BE accumulator
    // upserts by id (a repeated start for an id already in its list is a
    // no-op) — so the append was a real divergence from useSSE's own
    // documented contract, not intended behavior. Both useSSE and
    // useSubSessionSSE now share `foldToolCallStart` (lib/sseStream.ts),
    // which upserts by id — matching the BE fold and useSubSessionSSE's
    // pre-existing (correct) behavior below. Updated expectations assert
    // exactly ONE card, matching the "useSubSessionSSE" test in this file.
    const frames =
      frame("chat.start", { msg_id: 1, response_id: "rid-1" }) +
      frame("tool_call.start", {
        msg_id: 1,
        tool_call: { id: "dup-id", name: "", arguments: {}, call_id: null, result: null },
      }) +
      // Repeated start for the SAME id — e.g. a reconnect / decoder resend.
      frame("tool_call.start", {
        msg_id: 1,
        tool_call: { id: "dup-id", name: "", arguments: {}, call_id: null, result: null },
      }) +
      frame("tool_call.name", {
        msg_id: 1,
        tool_call: { id: "dup-id", name: "search", arguments: {}, call_id: null, result: null },
      }) +
      frame("chat.end", { msg_id: 1 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE(42));
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    // Correct (upsert-by-id) behavior: exactly ONE card for the one logical
    // tool call, matching useSubSessionSSE's and the backend's fold.
    expect(result.current.state.toolCalls).toHaveLength(1);
    expect(result.current.state.toolCalls[0]?.id).toBe("dup-id");
    expect(result.current.state.toolCalls[0]?.name).toBe("search");
  });

  it("useSubSessionSSE: a repeated sub.tool_call.start for the SAME id UPSERTS in place (no duplicate)", async () => {
    const sseText =
      `event: sub.tool_call.start\ndata: ${JSON.stringify({ id: "dup-id", name: "", arguments: "" })}\n\n` +
      // Repeated start for the SAME id.
      `event: sub.tool_call.start\ndata: ${JSON.stringify({ id: "dup-id", name: "", arguments: "" })}\n\n` +
      `event: sub.tool_call.name\ndata: ${JSON.stringify({ id: "dup-id", name: "search" })}\n\n` +
      `event: sub.complete\ndata: ${JSON.stringify({ final_content: "done" })}\n\n`;

    global.fetch = vi.fn().mockResolvedValue(chunkedSseResponse([sseText]));
    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Correct (upsert-by-id) behavior: exactly ONE card for the one logical
    // tool call, matching the backend's own `_accumulate_tool_call` fold.
    expect(result.current.state.toolCalls).toHaveLength(1);
    expect(result.current.state.toolCalls[0]?.id).toBe("dup-id");
    expect(result.current.state.toolCalls[0]?.name).toBe("search");
  });
});
