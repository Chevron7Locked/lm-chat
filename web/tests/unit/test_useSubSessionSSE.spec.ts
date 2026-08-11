/**
 * Unit tests for useSubSessionSSE.
 *
 * Key test: Fix #1 — the SSE parser bug where `eventType` was declared INSIDE
 * the chunked-read loop, causing it to reset to "" before the `data:` line was
 * processed whenever an SSE message arrived across two or more read() chunks.
 *
 * Strategy: mock fetch to return a ReadableStream with controlled chunking;
 * verify that events split across chunk boundaries are correctly parsed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSubSessionSSE } from "@/hooks/useSubSessionSSE";
import type { SubSessionStreamParams } from "@/hooks/useSubSessionSSE";

// ─── Helpers ─────────────────────────────────────────────────────────────────

const encoder = new TextEncoder();

/**
 * Build a Response whose body is a ReadableStream yielding the supplied chunks
 * in order, with one read() call per chunk.
 */
function chunkedSseResponse(chunks: string[]): Response {
  let idx = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (idx < chunks.length) {
        controller.enqueue(encoder.encode(chunks[idx++]!));
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

/** Minimal params for useSubSessionSSE.stream(). */
const BASE_PARAMS: SubSessionStreamParams = {
  chatId: 1,
  modelId: "test-model",
  systemPrompt: "You are helpful.",
  messages: [],
};

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("useSubSessionSSE — SSE parser (fix #1)", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("parses a sub.delta event that arrives in a single chunk", async () => {
    const sseText = "event: sub.delta\ndata: {\"delta\":\"hello\"}\n\nevent: sub.complete\ndata: {\"final_content\":\"hello\"}\n\n";
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse([sseText]));

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(BASE_PARAMS);
      // Let the microtask queue drain so the async IIFE can process all chunks.
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.content).toBe("hello");
  });

  it("fix #1: parses a sub.delta event split across two read() chunks (event: in chunk 1, data: in chunk 2)", async () => {
    // Chunk 1 ends inside the SSE message — after the event: line, before data:.
    const chunk1 = "event: sub.delta\n";
    const chunk2 = "data: {\"delta\":\"world\"}\n\nevent: sub.complete\ndata: {\"final_content\":\"world\"}\n\n";

    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse([chunk1, chunk2]));

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(result.current.state.status).toBe("complete");
    // The delta should have been accumulated — proves eventType survived the
    // chunk boundary between event: and data:.
    expect(result.current.state.content).toBe("world");
  });

  it("fix #1: handles multiple sub.delta events where each event line and data line are in separate chunks", async () => {
    // Three deltas, each event: split from its data: by a chunk boundary.
    const chunks = [
      "event: sub.delta\n",
      "data: {\"delta\":\"a\"}\n\n",
      "event: sub.delta\n",
      "data: {\"delta\":\"b\"}\n\n",
      "event: sub.delta\n",
      "data: {\"delta\":\"c\"}\n\nevent: sub.complete\ndata: {\"final_content\":\"abc\"}\n\n",
    ];

    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse(chunks));

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.content).toBe("abc");
  });

  it("fix #3: EOF without sub.complete emits status=error with stream_truncated code (does NOT fire onComplete)", async () => {
    // Stream just has some deltas and then EOF — no sub.complete or sub.error.
    const sseText = "event: sub.delta\ndata: {\"delta\":\"partial\"}\n\n";
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse([sseText]));

    const onComplete = vi.fn();
    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream({ ...BASE_PARAMS, onComplete });
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(result.current.state.status).toBe("error");
    expect(result.current.state.truncated).toBe(true);
    expect(result.current.state.content).toBe("partial"); // accumulated content preserved
    expect(result.current.state.error?.code).toBe("stream_truncated");
    expect(onComplete).not.toHaveBeenCalled(); // fix #3: onComplete must NOT fire
  });

  it("fix #3: EOF without any events at all emits status=error (empty accumulated)", async () => {
    // Server closes the stream immediately without any events.
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse([]));

    const onComplete = vi.fn();
    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream({ ...BASE_PARAMS, onComplete });
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(result.current.state.status).toBe("error");
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("fix #5: sub.complete with truncated=true exposes truncation metadata on state", async () => {
    const completePayload = JSON.stringify({
      final_content: "some partial content",
      truncated: true,
      truncation_reason: "token_limit",
      truncation_hint: "The response was cut at the token limit.",
    });
    const sseText = `event: sub.complete\ndata: ${completePayload}\n\n`;
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse([sseText]));

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.content).toBe("some partial content");
    expect(result.current.state.truncated).toBe(true);
    expect(result.current.state.truncation_reason).toBe("token_limit");
    expect(result.current.state.truncation_hint).toBe("The response was cut at the token limit.");
  });

  it("fix #9: unknown sub.* event is logged and skipped (does not crash)", async () => {
    const consoleSpy = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const sseText = "event: sub.future_event\ndata: {\"new_field\":\"value\"}\n\nevent: sub.complete\ndata: {\"final_content\":\"ok\"}\n\n";
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse([sseText]));

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(result.current.state.status).toBe("complete");
    expect(consoleSpy).toHaveBeenCalledWith(
      "[sub-session] unrecognised event:",
      "sub.future_event",
      expect.anything(),
    );
    consoleSpy.mockRestore();
  });

  it("fix #9: malformed data JSON logs console.error and continues (does not crash)", async () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    // Bad JSON on the delta line, but then a valid sub.complete follows.
    const sseText = "event: sub.delta\ndata: {invalid json}\n\nevent: sub.complete\ndata: {\"final_content\":\"recovered\"}\n\n";
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse([sseText]));

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(result.current.state.status).toBe("complete");
    expect(consoleSpy).toHaveBeenCalledWith(
      "[sub-session] SSE parse error for event",
      "sub.delta",
      ":",
      expect.anything(),
    );
    consoleSpy.mockRestore();
  });

  it("fix #18: stale setState callbacks from an aborted stream are ignored", async () => {
    // First stream starts but is aborted before it produces results.
    // Second stream runs normally.
    let resolveFirst!: (r: Response) => void;
    const firstFetch = new Promise<Response>((res) => { resolveFirst = res; });

    const sseText2 = "event: sub.complete\ndata: {\"final_content\":\"second\"}\n\n";

    let callCount = 0;
    vi.mocked(fetch).mockImplementation(() => {
      callCount++;
      if (callCount === 1) return firstFetch;
      return Promise.resolve(chunkedSseResponse([sseText2]));
    });

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      // Start first stream.
      result.current.stream(BASE_PARAMS);
      // Abort immediately and start second stream.
      result.current.abort();
      result.current.stream({ ...BASE_PARAMS, modelId: "second-model" });
      // Now resolve the first fetch with content — its setState should be a no-op.
      resolveFirst(
        chunkedSseResponse(["event: sub.complete\ndata: {\"final_content\":\"first\"}\n\n"])
      );
      await new Promise((r) => setTimeout(r, 100));
    });

    // The second stream's result should win, not the first.
    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.content).toBe("second");
  });

  it("empty model_id is rejected with an error state immediately", async () => {
    const { result } = renderHook(() => useSubSessionSSE());

    act(() => {
      result.current.stream({ ...BASE_PARAMS, modelId: "" });
    });

    expect(result.current.state.status).toBe("error");
    expect(result.current.state.error?.code).toBe("no_model_selected");
    expect(fetch).not.toHaveBeenCalled();
  });
});
