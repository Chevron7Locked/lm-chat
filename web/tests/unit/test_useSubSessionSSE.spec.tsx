/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for the useSubSessionSSE hook.
 *
 * The hook owns the SSE transport that PR-E now bridges through the
 * canonical pipeline. Tests assert the hook's state-machine contract:
 *
 *   stream()   — POSTs to /api/chats/{id}/sub-session/stream, drives state
 *                through "streaming" → "complete" as sub.delta / sub.complete
 *                events arrive.
 *   finalize() — POSTs to /api/chats/{id}/sub-session/finalize using the same
 *                machinery, fires onComplete with the assembled final content.
 *   sub.error  — surfaces as state.status === "error" with the error message.
 *   reset()    — returns to idle and aborts any in-flight request.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

function buildSseStream(text: string): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

function sseFrame(eventType: string, data: object): string {
  return `event: ${eventType}\ndata: ${JSON.stringify(data)}\n\n`;
}

const baseParams = {
  chatId: 7,
  modelId: "test-model",
  systemPrompt: "you are a tester",
  messages: [{ role: "user" as const, content: "hello" }],
};

describe("useSubSessionSSE", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("initial state is idle", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");
    const { result } = renderHook(() => useSubSessionSSE());
    expect(result.current.state.status).toBe("idle");
    expect(result.current.state.content).toBe("");
    expect(result.current.state.error).toBeNull();
  });

  it("stream() POSTs to /sub-session/stream and assembles sub.delta into content", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    const body =
      sseFrame("sub.delta", { delta: "Hel" }) +
      sseFrame("sub.delta", { delta: "lo" }) +
      sseFrame("sub.complete", { final_content: "Hello" });

    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(buildSseStream(body), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );
    global.fetch = fetchSpy as typeof global.fetch;

    const onComplete = vi.fn();
    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream({ ...baseParams, onComplete });
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("complete");
    });

    // POST hit the stream endpoint.
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/chats/7/sub-session/stream");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);

    expect(result.current.state.content).toBe("Hello");
    expect(onComplete).toHaveBeenCalledWith("Hello");
  });

  it("finalize() POSTs to /sub-session/finalize with the same form payload", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    const body = sseFrame("sub.complete", { final_content: "summary" });

    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(buildSseStream(body), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );
    global.fetch = fetchSpy as typeof global.fetch;

    const onComplete = vi.fn();
    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.finalize({ ...baseParams, onComplete });
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("complete");
    });

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/chats/7/sub-session/finalize");
    expect(init.method).toBe("POST");
    expect(onComplete).toHaveBeenCalledWith("summary");
  });

  it("sub.error transitions state to error and surfaces the error object", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    // BE emits canonical {code, message} shape.
    const body =
      sseFrame("sub.delta", { delta: "partial" }) +
      sseFrame("sub.error", { code: "context_window_exceeded", message: "The context window was exceeded." });

    global.fetch = vi.fn().mockResolvedValue(
      new Response(buildSseStream(body), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    ) as typeof global.fetch;

    const onComplete = vi.fn();
    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream({ ...baseParams, onComplete });
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("error");
    });
    // Correction 3: error is now a typed object, not a string.
    expect(result.current.state.error?.code).toBe("context_window_exceeded");
    expect(result.current.state.error?.message).toBe("The context window was exceeded.");
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("HTTP 500 transitions state to error without crashing", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "boom" }), {
        status: 500,
        headers: { "content-type": "application/json" },
      }),
    ) as typeof global.fetch;

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(baseParams);
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("error");
    });
    // Correction 3: error is now a typed object; message contains the HTTP status.
    expect(result.current.state.error?.message).toContain("500");
  });

  it("reset() aborts an in-flight stream and returns state to idle", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    // Never-resolving fetch — simulates an in-flight stream.
    global.fetch = vi.fn().mockReturnValue(
      new Promise(() => { /* never resolves */ }),
    ) as typeof global.fetch;

    const { result } = renderHook(() => useSubSessionSSE());

    act(() => {
      result.current.stream(baseParams);
    });
    expect(result.current.state.status).toBe("streaming");

    act(() => {
      result.current.reset();
    });
    expect(result.current.state.status).toBe("idle");
    expect(result.current.state.content).toBe("");
    expect(result.current.state.error).toBeNull();
  });

  it("sub.error with neither code nor message uses friendly fallback string", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    // BE emits a sub.error with an empty payload — no code, no message.
    const body = sseFrame("sub.error", {});

    global.fetch = vi.fn().mockResolvedValue(
      new Response(buildSseStream(body), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    ) as typeof global.fetch;

    const { result } = renderHook(() => useSubSessionSSE());

    await act(async () => {
      result.current.stream(baseParams);
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("error");
    });
    expect(result.current.state.error?.message).toBe(
      "Research session ended unexpectedly — try again.",
    );
  });
});
