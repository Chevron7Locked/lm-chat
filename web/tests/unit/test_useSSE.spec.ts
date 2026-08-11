/**
 * Unit tests for useSSE state machine.
 *
 * Strategy: mock fetch to return a ReadableStream of SSE frames; call start();
 * verify state transitions after each significant event type.
 *
 * jsdom does not implement ReadableStream.pipeThrough natively in all versions;
 * we construct a synthetic ReadableStream that yields a Buffer and verify state
 * transitions by awaiting the start() promise.
 *
 * stop() test notes: jsdom's fetch mock does not propagate AbortController
 * signals into ReadableStream reads. To test the stop() path we close the
 * underlying stream controller after calling stop(), which causes reader.read()
 * to resolve with { done: true } and the hook exits the read loop. The hook
 * then sees status === "idle" (set by stop() before the loop exits).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSSE } from "@/hooks/useSSE";

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Build a minimal Response carrying the given SSE text as a ReadableStream. */
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

/** Format a single SSE frame. */
function frame(type: string, data: Record<string, unknown>): string {
  return `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
}

// ─── BroadcastChannel stub ────────────────────────────────────────────────────

class StubChannel {
  static instances: StubChannel[] = [];
  messages: unknown[] = [];
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  postMessage(msg: unknown) { this.messages.push(msg); }
  close() { /* no-op */ }
  constructor() { StubChannel.instances.push(this); }
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("useSSE", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    StubChannel.instances = [];
    // Override the global BroadcastChannel with our stub for this test.
    // @ts-expect-error -- stub BroadcastChannel globally
    global.BroadcastChannel = StubChannel;
    // Clear localStorage before each test to avoid cross-test pollution.
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // Restore real BroadcastChannel after each test.
    // @ts-expect-error -- restore
    delete global.BroadcastChannel;
  });

  it("initial state is idle", () => {
    const { result } = renderHook(() => useSSE());
    expect(result.current.state.status).toBe("idle");
    expect(result.current.state.contentDeltas).toHaveLength(0);
    expect(result.current.state.reasoningDeltas).toHaveLength(0);
    expect(result.current.state.toolCalls).toHaveLength(0);
    expect(result.current.state.error).toBeNull();
  });

  it("transitions idle → streaming on start()", async () => {
    const frames =
      frame("chat.start", { msg_id: 1, response_id: "rid-1" }) +
      frame("message.start", { msg_id: 1 }) +
      frame("message.delta", { msg_id: 1, delta: "Hello" }) +
      frame("chat.end", { msg_id: 1 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());
    expect(result.current.state.status).toBe("idle");

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.status).toBe("complete");
  });

  it("reads PAST chat.end to capture the out-of-band followups frame", async () => {
    // Regression (2026-06-24): the read loop used to `reader.cancel(); return`
    // on chat.end, which dropped the `followups` frame the BE emits AFTER it
    // (chips are generated after the answer so they never delay it). The chips
    // never rendered in the browser even though the BE sent them. The loop must
    // keep reading past chat.end until the followups frame arrives (or close).
    const frames =
      frame("chat.start", { msg_id: 7, response_id: "rid-7" }) +
      frame("message.start", { msg_id: 7 }) +
      frame("message.delta", { msg_id: 7, delta: "Vectors are lists of numbers." }) +
      frame("chat.end", { msg_id: 7 }) +
      frame("followups", {
        msg_id: 7,
        followups: ["How do I make my own?", "Why are similar items close?"],
      });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    // chat.end still marks the answer complete...
    expect(result.current.state.status).toBe("complete");
    // ...AND the trailing followups frame was read and captured.
    expect(result.current.state.followups).toEqual([
      "How do I make my own?",
      "Why are similar items close?",
    ]);
  });

  it("reads PAST both chat.end and followups to capture the memory.saved frame", async () => {
    // memory.saved is the true LAST OOB frame on the wire — it can follow
    // followups (see streaming_service.py's chat.end epilogue). The read
    // loop must not stop on followups anymore, or this later frame would
    // never be read (the reader would already be cancelled).
    const frames =
      frame("chat.start", { msg_id: 9, response_id: "rid-9" }) +
      frame("message.start", { msg_id: 9 }) +
      frame("message.delta", { msg_id: 9, delta: "Noted." }) +
      frame("chat.end", { msg_id: 9 }) +
      frame("followups", { msg_id: 9, followups: ["Anything else?"] }) +
      frame("memory.saved", { msg_id: 9, count: 2 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.followups).toEqual(["Anything else?"]);
    expect(result.current.state.memorySaved).toEqual({ count: 2, msgId: 9 });
  });

  it("captures memory.saved when followups never arrive (followups disabled)", async () => {
    const frames =
      frame("chat.start", { msg_id: 11, response_id: "rid-11" }) +
      frame("message.start", { msg_id: 11 }) +
      frame("message.delta", { msg_id: 11, delta: "Got it." }) +
      frame("chat.end", { msg_id: 11 }) +
      frame("memory.saved", { msg_id: 11, count: 1 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.memorySaved).toEqual({ count: 1, msgId: 11 });
  });

  it("leaves memorySaved undefined when no memory.saved frame arrives", async () => {
    const frames =
      frame("chat.start", { msg_id: 12, response_id: "rid-12" }) +
      frame("message.start", { msg_id: 12 }) +
      frame("message.delta", { msg_id: 12, delta: "Sure." }) +
      frame("chat.end", { msg_id: 12 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.memorySaved).toBeUndefined();
  });

  it("ignores a memory.saved frame with count 0 (BE never sends one, but be defensive)", async () => {
    const frames =
      frame("chat.start", { msg_id: 13, response_id: "rid-13" }) +
      frame("message.start", { msg_id: 13 }) +
      frame("message.delta", { msg_id: 13, delta: "OK." }) +
      frame("chat.end", { msg_id: 13 }) +
      frame("memory.saved", { msg_id: 13, count: 0 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.memorySaved).toBeUndefined();
  });

  it("accumulates message.delta into contentDeltas", async () => {
    const frames =
      frame("chat.start", { msg_id: 2, response_id: "rid-2" }) +
      frame("message.delta", { msg_id: 2, delta: "Hello" }) +
      frame("message.delta", { msg_id: 2, delta: " world" }) +
      frame("chat.end", { msg_id: 2 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.contentDeltas).toEqual(["Hello", " world"]);
    expect(result.current.state.contentDeltas.join("")).toBe("Hello world");
  });

  it("accumulates reasoning.delta into reasoningDeltas", async () => {
    const frames =
      frame("chat.start", { msg_id: 3, response_id: "rid-3" }) +
      frame("reasoning.delta", { msg_id: 3, delta: "thinking..." }) +
      frame("message.delta", { msg_id: 3, delta: "answer" }) +
      frame("chat.end", { msg_id: 3 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.reasoningDeltas).toEqual(["thinking..."]);
  });

  it("sets messageId and responseId from chat.start", async () => {
    const frames =
      frame("chat.start", { msg_id: 99, response_id: "resp-abc" }) +
      frame("chat.end", { msg_id: 99 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.messageId).toBe(99);
    expect(result.current.state.responseId).toBe("resp-abc");
  });

  it("transitions to error on error frame", async () => {
    const frames =
      frame("chat.start", { msg_id: 5, response_id: "rid-5" }) +
      frame("error", { msg_id: 5, code: "upstream_stall", message: "LM Studio stalled" });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.status).toBe("error");
    expect(result.current.state.error).toMatchObject({
      code: "upstream_stall",
      message: "LM Studio stalled",
    });
  });

  it("transitions to error on non-ok HTTP response", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: { code: "stream_in_progress", chat_id: 42 } }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      })
    );

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.status).toBe("error");
    expect(result.current.state.error?.code).toBe("http_409");
  });

  it("accumulates tool_call events (nested wire shape per _format_sse_frame)", async () => {
    // Finding #1 (audit ba1d324): the main-chat wire NESTS the payload under
    // `tool_call` (CanonicalToolCall.model_dump()); `arguments` is a dict and
    // each tool_call.arguments event re-sends the COMPLETE dict (no deltas).
    const frames =
      frame("chat.start", { msg_id: 6, response_id: "rid-6" }) +
      frame("tool_call.start", {
        msg_id: 6,
        tool_call: { id: "tc-1", name: "", arguments: {}, call_id: null, result: null },
      }) +
      frame("tool_call.name", {
        msg_id: 6,
        tool_call: { id: "tc-1", name: "search", arguments: {}, call_id: null, result: null },
      }) +
      frame("tool_call.arguments", {
        msg_id: 6,
        tool_call: { id: "tc-1", name: "search", arguments: { q: "test" }, call_id: null, result: null },
      }) +
      frame("tool_call.success", {
        msg_id: 6,
        tool_call: { id: "tc-1", name: "search", arguments: { q: "test" }, call_id: null, result: "results here" },
      }) +
      frame("chat.end", { msg_id: 6 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.toolCalls).toHaveLength(1);
    const tc = result.current.state.toolCalls[0];
    expect(tc?.name).toBe("search");
    expect(tc?.arguments).toBe('{"q":"test"}');
    expect(tc?.status).toBe("success");
    expect(tc?.result).toBe("results here");
  });

  it("stores msg_id in localStorage on chat.start (checked before chat.end clears it)", async () => {
    // Use a stream that sends chat.start but NOT chat.end so msg_id persists.
    // Per PLAN §P7b: msg_id is stored on chat.start, cleared on chat.end/error.
    // The stream ends without a terminal event — the hook treats this as "complete"
    // (reader exhausted) but the msg_id is NOT cleared (only chat.end clears it).
    const frames =
      frame("chat.start", { msg_id: 77, response_id: "rid-77" }) +
      frame("message.delta", { msg_id: 77, delta: "hello" });
    // Intentionally no chat.end — stream closes without a terminal frame.

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(10, { input: [] });
    });

    // msg_id should be stored because no chat.end was emitted to clear it.
    expect(localStorage.getItem("lmchat:sse:10:msg_id")).toBe("77");
  });

  it("clears msg_id from localStorage on chat.end", async () => {
    localStorage.setItem("lmchat:sse:10:msg_id", "77");

    const frames =
      frame("chat.start", { msg_id: 77, response_id: "rid-77" }) +
      frame("chat.end", { msg_id: 77 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(10, { input: [] });
    });

    expect(localStorage.getItem("lmchat:sse:10:msg_id")).toBeNull();
  });

  it("stop() transitions streaming to idle", async () => {
    // Use a stream that never closes naturally. We close its controller manually
    // after stop() to unblock the reader.read() call.
    // jsdom's mocked fetch does not propagate AbortController signals to
    // ReadableStream reads, so we must close the stream to resolve the read.
    let readController: ReadableStreamDefaultController<Uint8Array> | null = null;
    const stream = new ReadableStream<Uint8Array>({
      start(ctrl) { readController = ctrl; },
    });

    global.fetch = vi.fn().mockResolvedValue(
      new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } })
    );

    const { result } = renderHook(() => useSSE());

    // Start the stream — don't await; it hangs until we close the stream.
    const startPromise = result.current.start(42, { input: [] });

    // Give a tick for fetch to resolve and the read loop to start.
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    // Call stop() — this sets status to idle via setState immediately.
    act(() => {
      result.current.stop();
    });

    // Close the underlying stream so reader.read() resolves.
    // This lets the start() async function exit, which resolves startPromise.
    act(() => {
      readController?.close();
    });

    // Await the start promise (exits cleanly when stream closes).
    await act(async () => {
      await startPromise.catch(() => { /* swallow any AbortError */ });
    });

    // After stop(), status should be idle.
    expect(result.current.state.status).toBe("idle");
  }, 10_000);

  it("transitions to error when response.body is null", async () => {
    // Construct a Response with a null body by reading a text response to exhaustion.
    // We simulate this by creating a custom Response-like object.
    const nullBodyResponse = new Response(null, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    global.fetch = vi.fn().mockResolvedValue(nullBodyResponse);

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    // A null body response should set status to error.
    expect(result.current.state.status).toBe("error");
    expect(result.current.state.error?.code).toBe("no_body");
  });

  it("skips malformed JSON data lines without crashing", async () => {
    // A stream with invalid JSON in the data field followed by a valid chat.end.
    const encoder = new TextEncoder();
    const badFrame = "event: message.delta\ndata: {NOT VALID JSON}\n\n";
    const goodFrame = frame("chat.end", { msg_id: 1 });
    const allFrames =
      frame("chat.start", { msg_id: 1, response_id: "rid-1" }) +
      badFrame +
      goodFrame;

    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(allFrames));
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue(
      new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } })
    );

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    // Stream completes (chat.end was parsed after skipping bad frame).
    expect(result.current.state.status).toBe("complete");
  });

  // Cluster 1 Task 4 (audit 2026-06-10): EOF-without-terminal handling.
  // Pre-Cluster-1, BOTH cases below mapped to status:"complete" — which
  // hid the silent-stream-death symptom (thinking indicator vanishes,
  // no error). Post-Cluster-1, the no-delta case becomes a visible
  // `stream_truncated` error; the with-delta case stays complete but
  // raises `truncated_without_terminal` for the Cluster 3b Continue chip.

  it("EOF after chat.start with deltas → complete + truncated_without_terminal", async () => {
    // Stream with chat.start + delta but no chat.end; reader closes naturally.
    // Deltas were received, so this is a legitimate mid-token truncation.
    const frames =
      frame("chat.start", { msg_id: 8, response_id: "rid-8" }) +
      frame("message.delta", { msg_id: 8, delta: "partial" });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.contentDeltas).toContain("partial");
    expect(result.current.state.truncated_without_terminal).toBe(true);
    // Continue-chip closeout: EOF-without-terminal is the second source
    // of the unified Continue affordance.
    expect(result.current.state.showContinue).toBe(true);
  });

  // Continue-chip closeout (audit 2026-06-10): stop_reason capture from
  // chat.end + the unified showContinue affordance (one chip, two sources).

  it("chat.end with stop_reason='length' → stop_reason captured + showContinue", async () => {
    const frames =
      frame("chat.start", { msg_id: 21, response_id: "rid-21" }) +
      frame("message.delta", { msg_id: 21, delta: "truncated reply" }) +
      frame("chat.end", { msg_id: 21, stop_reason: "length" });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.stop_reason).toBe("length");
    expect(result.current.state.showContinue).toBe(true);
  });

  it("chat.end with stop_reason='stop' → no Continue affordance", async () => {
    const frames =
      frame("chat.start", { msg_id: 22, response_id: "rid-22" }) +
      frame("message.delta", { msg_id: 22, delta: "complete reply" }) +
      frame("chat.end", { msg_id: 22, stop_reason: "stop" });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.stop_reason).toBe("stop");
    expect(result.current.state.showContinue).toBe(false);
  });

  // Finding #2 (audit ba1d324): chat.end carries real LM Studio token stats.

  it("chat.end with real stats → outputTokens/tokensPerSecond from the BE, not chunk counts", async () => {
    const frames =
      frame("chat.start", { msg_id: 30, response_id: "rid-30" }) +
      frame("message.delta", { msg_id: 30, delta: "one chunk only" }) +
      frame("chat.end", {
        msg_id: 30,
        total_output_tokens: 512,
        tokens_per_second: 41.7,
      });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.status).toBe("complete");
    // Real BE values win over the local deltaCount-derived approximation
    // (1 delta arrived; the BE says 512 tokens at 41.7 tok/s).
    expect(result.current.state.stats.outputTokens).toBe(512);
    expect(result.current.state.stats.tokensPerSecond).toBe(41.7);
  });

  it("chat.end WITHOUT stats → falls back to local chunk-count approximation", async () => {
    const frames =
      frame("chat.start", { msg_id: 31, response_id: "rid-31" }) +
      frame("message.delta", { msg_id: 31, delta: "a" }) +
      frame("message.delta", { msg_id: 31, delta: "b" }) +
      frame("chat.end", { msg_id: 31 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.stats.outputTokens).toBe(2);
    expect(result.current.state.stats.tokensPerSecond).not.toBeNull();
  });

  it("chat.end WITHOUT stop_reason (older backend) → null, no chip", async () => {
    const frames =
      frame("chat.start", { msg_id: 23, response_id: "rid-23" }) +
      frame("message.delta", { msg_id: 23, delta: "reply" }) +
      frame("chat.end", { msg_id: 23 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.stop_reason).toBeNull();
    expect(result.current.state.showContinue).toBe(false);
  });

  it("EOF after chat.start with NO deltas → error stream_truncated", async () => {
    // Stream with chat.start ONLY, then EOF. This is the exact
    // silent-stream-death pattern: thinking indicator appears (chat.start
    // arrived), then disappears (EOF), with zero content. Pre-Cluster-1 this
    // mapped to status:"complete" — the bug. Post-Cluster-1 the user sees
    // an actionable error frame.
    const frames = frame("chat.start", { msg_id: 9, response_id: "rid-9" });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.status).toBe("error");
    expect(result.current.state.error?.code).toBe("stream_truncated");
    expect(result.current.state.error?.message).toMatch(/context window|connection/);
    expect(result.current.state.truncated_without_terminal).toBe(false);
  });

  it("transitions to error on fetch() rejection (network error)", async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error("network failure"));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.status).toBe("error");
    expect(result.current.state.error?.code).toBe("fetch_error");
    expect(result.current.state.error?.message).toBe("network failure");
  });

  it("completes with responseId=null when chat.start omits response_id (LM Studio native pattern)", async () => {
    // Regression guard for the message-list desync bug (2026-05-28):
    // LM Studio native streams send chat.start WITHOUT response_id.
    // The refetch effect in Chat.tsx must NOT gate on responseId !== null —
    // only on status === "complete".  This test documents that the hook
    // faithfully reflects the missing field (responseId stays null) so the
    // caller can implement the unconditional refetch guard correctly.
    const frames =
      frame("chat.start", { msg_id: 21 }) +        // no response_id field
      frame("message.delta", { msg_id: 21, delta: "OK" }) +
      frame("chat.end", { msg_id: 21 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(5, { input: [{ type: "text" as const, content: "say OK" }] });
    });

    // Stream completes normally even without response_id.
    expect(result.current.state.status).toBe("complete");
    // responseId must be null — the caller must not use this as a refetch gate.
    expect(result.current.state.responseId).toBeNull();
    // Content arrives as expected.
    expect(result.current.state.contentDeltas).toEqual(["OK"]);
    // messageId is set from msg_id as usual.
    expect(result.current.state.messageId).toBe(21);
  });

  it("broadcasts stream_started on BroadcastChannel when chat.start fires", async () => {
    const frames =
      frame("chat.start", { msg_id: 55, response_id: "rid-55" }) +
      frame("chat.end", { msg_id: 55 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(20, { input: [] });
    });

    // The StubChannel instance used by the hook should have received the message.
    const ch = StubChannel.instances[0];
    expect(ch).toBeDefined();
    const startMsg = ch?.messages.find(
      (m) => (m as { type: string }).type === "stream_started"
    );
    expect(startMsg).toBeDefined();
    expect((startMsg as { chat_id: number }).chat_id).toBe(20);
  });

  it("reads error code from nested error field (fixes prod bug)", async () => {
    // The backend writes error frames as { data: { error: { code, message, ... } } }
    // The frontend was reading from top-level (code, message) which didn't exist,
    // causing every error to show code: "unknown". This test verifies the fix.
    const frames =
      frame("chat.start", { msg_id: 100, response_id: "rid-100" }) +
      frame("error", {
        msg_id: 100,
        error: {
          code: "context_window_exceeded",
          message: "Conversation too long",
        },
      });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    // The error code should be read from the nested error field, not "unknown"
    expect(result.current.state.status).toBe("error");
    expect(result.current.state.error?.code).toBe("context_window_exceeded");
    expect(result.current.state.error?.message).toBe("Conversation too long");
  });

  it("preserves mtp_suspected error with cumulative_tool_rounds and hint", async () => {
    const frames =
      frame("chat.start", { msg_id: 101, response_id: "rid-101" }) +
      frame("error", {
        msg_id: 101,
        error: {
          code: "mtp_suspected",
          message: "Long tool chain",
          cumulative_tool_rounds: 20,
          hint: "Disable MTP in LM Studio",
        },
      });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.status).toBe("error");
    expect(result.current.state.error?.code).toBe("mtp_suspected");
    expect(result.current.state.error?.message).toBe("Long tool chain");
    expect(result.current.state.error?.cumulative_tool_rounds).toBe(20);
    expect(result.current.state.error?.hint).toBe("Disable MTP in LM Studio");
  });

  // ─── T0-1 (chat-flow remediation Phase 0) — non-terminal warning frames ───

  it("appends a warning frame to state.warnings without terminating the stream", async () => {
    // The BE budget gate yields the warning BEFORE chat.start (the gate
    // runs before the upstream stream opens) — the handler must not
    // assume any prior lifecycle event. If `warning` were treated as
    // terminal, the reader would cancel and the deltas below would
    // never accumulate.
    const frames =
      frame("warning", {
        msg_id: 7,
        warning: {
          code: "integrations_trimmed_for_context",
          message: "This model's context only fits 2 of 5 tools.",
        },
      }) +
      frame("chat.start", { msg_id: 7, response_id: "rid-7" }) +
      frame("message.delta", { msg_id: 7, delta: "Hello" }) +
      frame("chat.end", { msg_id: 7 });

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });

    expect(result.current.state.warnings).toEqual([
      {
        code: "integrations_trimmed_for_context",
        message: "This model's context only fits 2 of 5 tools.",
      },
    ]);
    // Non-terminal: the stream proceeded to a normal completion and the
    // post-warning content arrived.
    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.contentDeltas).toEqual(["Hello"]);
    expect(result.current.state.error).toBeNull();
  });

  it("resets warnings on a new start()", async () => {
    const firstFrames =
      frame("warning", {
        msg_id: 8,
        warning: { code: "integrations_trimmed_for_context", message: "Trimmed." },
      }) +
      frame("chat.start", { msg_id: 8 }) +
      frame("chat.end", { msg_id: 8 });
    const secondFrames =
      frame("chat.start", { msg_id: 9 }) +
      frame("message.delta", { msg_id: 9, delta: "ok" }) +
      frame("chat.end", { msg_id: 9 });

    global.fetch = vi
      .fn()
      .mockResolvedValueOnce(sseResponse(firstFrames))
      .mockResolvedValueOnce(sseResponse(secondFrames));

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "hi" }] });
    });
    expect(result.current.state.warnings).toHaveLength(1);

    await act(async () => {
      await result.current.start(42, { input: [{ type: "text" as const, content: "again" }] });
    });
    expect(result.current.state.warnings).toHaveLength(0);
    expect(result.current.state.status).toBe("complete");
  });

  // The end-to-end "banner shows once, second event suppressed" behavior is
  // tested at the component level in `test_Chat.spec.tsx` ("Chat (mtp_suspected
  // dedupe)") since the dedupe state lives on the Chat component, not the hook.
});
