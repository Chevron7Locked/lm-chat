/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for streamStore — the chat-keyed SSE state machine that
 * replaces useSSE()'s single shared instance (Stage 2 of the FE stream-
 * scoping refactor).
 *
 * This file characterizes the two properties the refactor exists to
 * guarantee, that a single-instance hook structurally could not:
 *
 *   1. start(chatId, payload) writes ONLY to streams[chatId] — a sibling
 *      chat's slot is untouched. This is the store-characterization test
 *      the whole refactor rests on: it goes red the instant start()/stop()
 *      write to a shared slot instead of a chat-keyed one.
 *   2. start(B) does not abort an in-flight start(A) — separate
 *      AbortControllers per chat close the write-side of the cross-chat
 *      data-loss bug (generation running in chat A, user opens chat B,
 *      sends anything → A's fetch used to be aborted mid-generation).
 *
 * Wire-format / event-handling parity with the pre-refactor useSSE.ts hook
 * (message.delta accumulation, tool_call upsert-by-id, chat.end stats,
 * OOB followups/memory.saved/mode_adopt frames, etc.) is NOT re-derived
 * here — it's the same handleEvent ported verbatim, and is exercised
 * end-to-end via useSSE(chatId) once Stage 3 wires it up (see
 * test_useSSE.spec.ts / test_useSSE_main_path_tool_calls.spec.ts).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useStreamStore } from "@/stores/streamStore";

// ─── Helpers ────────────────────────────────────────────────────────────────

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

/** A Response whose body never closes on its own — used to hold a stream
 *  "in flight" while a second chat's start() runs concurrently, and to
 *  push frames into it on demand. Mirrors test_useSSE.spec.ts's
 *  stop()-test idiom (manual controller, closed at the end of the test so
 *  nothing leaks between tests). */
function pendingResponse(): {
  response: Response;
  enqueue: (text: string) => void;
  close: () => void;
} {
  let ctrl: ReadableStreamDefaultController<Uint8Array> | null = null;
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  return {
    response: new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
    enqueue: (text: string) => ctrl?.enqueue(encoder.encode(text)),
    close: () => ctrl?.close(),
  };
}

class StubChannel {
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  postMessage(): void {
    /* no-op */
  }
  close(): void {
    /* no-op */
  }
}

// ─── Tests ────────────────────────────────────────────────────────────────

describe("streamStore", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // @ts-expect-error -- stub BroadcastChannel globally
    global.BroadcastChannel = StubChannel;
    localStorage.clear();
    // The store is a module-level singleton — reset its observable state
    // between tests. (abortControllers/runGuards are internal and keyed
    // per-chatId; leftover entries from a prior test are harmless no-ops.)
    useStreamStore.setState({ streams: {} });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // @ts-expect-error -- restore
    delete global.BroadcastChannel;
  });

  it("start(chatId, payload) writes only that chat's slot — a sibling chat's slot stays untouched", async () => {
    const frames =
      frame("chat.start", { msg_id: 1, response_id: "rid-1" }) +
      frame("message.delta", { msg_id: 1, delta: "hi" }) +
      frame("chat.end", { msg_id: 1 });
    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    await useStreamStore.getState().start(1, { input: [] });

    expect(useStreamStore.getState().streams[1]?.status).toBe("complete");
    expect(useStreamStore.getState().streams[1]?.contentDeltas).toEqual(["hi"]);
    // The sibling chat was never touched — no slot was ever allocated for it.
    expect(useStreamStore.getState().streams[2]).toBeUndefined();
  });

  it("stop(chatId) only transitions that chat's slot", async () => {
    const pendingA = pendingResponse();
    global.fetch = vi.fn().mockResolvedValue(pendingA.response);

    const startPromise = useStreamStore.getState().start(1, { input: [] });
    await Promise.resolve();
    await Promise.resolve();

    useStreamStore.getState().stop(1);
    pendingA.close();
    await startPromise.catch(() => {
      /* swallow AbortError-adjacent rejection, if any */
    });

    expect(useStreamStore.getState().streams[1]?.status).toBe("idle");
    expect(useStreamStore.getState().streams[2]).toBeUndefined();
  });

  it("start(B) does not abort an in-flight start(A) — separate AbortControllers per chat", async () => {
    const pendingA = pendingResponse();
    const pendingB = pendingResponse();
    const fetchCalls: { chatId: number; signal: AbortSignal }[] = [];
    global.fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const rawBody = init?.body;
      const body = JSON.parse(typeof rawBody === "string" ? rawBody : "{}") as {
        chat_id: number;
      };
      if (init?.signal != null) {
        fetchCalls.push({ chatId: body.chat_id, signal: init.signal });
      }
      return Promise.resolve(body.chat_id === 1 ? pendingA.response : pendingB.response);
    });

    const startA = useStreamStore.getState().start(1, { input: [] });
    await Promise.resolve();
    await Promise.resolve();

    // A's stream is genuinely underway (chat.start landed) before B starts.
    pendingA.enqueue(frame("chat.start", { msg_id: 10, response_id: "rid-A" }));
    await Promise.resolve();
    await Promise.resolve();

    const startB = useStreamStore.getState().start(2, { input: [] });
    await Promise.resolve();
    await Promise.resolve();

    const callA = fetchCalls.find((c) => c.chatId === 1);
    // A's request must still be alive — starting B must not have aborted it.
    expect(callA?.signal.aborted).toBe(false);
    expect(useStreamStore.getState().streams[1]?.status).toBe("streaming");
    expect(useStreamStore.getState().streams[1]?.messageId).toBe(10);
    expect(useStreamStore.getState().streams[2]?.status).toBe("streaming");

    // A frame delivered to A AFTER B started must still land only in A's
    // slot — proving A is still genuinely live, not just un-aborted.
    pendingA.enqueue(frame("message.delta", { msg_id: 10, delta: "still alive" }));
    pendingA.enqueue(frame("chat.end", { msg_id: 10 }));
    pendingA.close();
    pendingB.enqueue(frame("chat.start", { msg_id: 20, response_id: "rid-B" }));
    pendingB.enqueue(frame("chat.end", { msg_id: 20 }));
    pendingB.close();

    await startA;
    await startB;

    expect(useStreamStore.getState().streams[1]?.status).toBe("complete");
    expect(useStreamStore.getState().streams[1]?.contentDeltas).toEqual(["still alive"]);
    expect(useStreamStore.getState().streams[2]?.status).toBe("complete");
    expect(useStreamStore.getState().streams[2]?.contentDeltas).toEqual([]);
  });

  it("receiveCrossTabMessage folds a peer-tab stream_started into that chat's slot only", () => {
    useStreamStore.getState().receiveCrossTabMessage({
      type: "stream_started",
      chat_id: 7,
      msg_id: 99,
    });

    expect(useStreamStore.getState().streams[7]).toMatchObject({
      status: "streaming",
      chatId: 7,
      messageId: 99,
    });
    expect(useStreamStore.getState().streams[8]).toBeUndefined();
  });
});
