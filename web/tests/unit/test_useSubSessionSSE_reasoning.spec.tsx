/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for useSubSessionSSE — reasoning_content wire-up (A7).
 *
 * The backend emits sub.reasoning.start / sub.reasoning.delta /
 * sub.reasoning.end when LM Studio streams a thinking block during a
 * sub-session.  These tests assert that the hook accumulates the deltas
 * into state.reasoning_content instead of discarding them.
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

function sseFrameEmpty(eventType: string): string {
  return `event: ${eventType}\ndata: {}\n\n`;
}

const baseParams = {
  chatId: 42,
  modelId: "reasoning-model",
  systemPrompt: "you are a reasoner",
  messages: [{ role: "user" as const, content: "think hard" }],
};

describe("useSubSessionSSE — reasoning_content (A7)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("initial state has reasoning_content as null", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");
    const { result } = renderHook(() => useSubSessionSSE());
    expect(result.current.state.reasoning_content).toBeNull();
  });

  it("accumulates sub.reasoning.* deltas into reasoning_content alongside content", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    const body =
      sseFrameEmpty("sub.reasoning.start") +
      sseFrame("sub.reasoning.delta", { delta: "Let me " }) +
      sseFrame("sub.reasoning.delta", { delta: "think..." }) +
      sseFrameEmpty("sub.reasoning.end") +
      sseFrame("sub.delta", { delta: "The answer is 42." }) +
      sseFrame("sub.complete", { final_content: "The answer is 42." });

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
      expect(result.current.state.status).toBe("complete");
    });

    // Content stream assembled correctly.
    expect(result.current.state.content).toBe("The answer is 42.");
    expect(onComplete).toHaveBeenCalledWith("The answer is 42.");

    // Reasoning content is non-null and contains the concatenated deltas.
    expect(result.current.state.reasoning_content).not.toBeNull();
    expect(result.current.state.reasoning_content).toBe("Let me think...");
  });

  it("reasoning_content is non-null after sub.reasoning.start even before deltas arrive", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    // Stream with start + no deltas + complete.
    const body =
      sseFrameEmpty("sub.reasoning.start") +
      sseFrameEmpty("sub.reasoning.end") +
      sseFrame("sub.complete", { final_content: "done" });

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
      expect(result.current.state.status).toBe("complete");
    });

    // After start, reasoning_content transitions from null to an empty string.
    expect(result.current.state.reasoning_content).toBe("");
  });

  it("reset() clears reasoning_content back to null", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    const body =
      sseFrameEmpty("sub.reasoning.start") +
      sseFrame("sub.reasoning.delta", { delta: "thinking..." }) +
      sseFrame("sub.complete", { final_content: "done" });

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
      expect(result.current.state.status).toBe("complete");
    });

    expect(result.current.state.reasoning_content).toBe("thinking...");

    act(() => {
      result.current.reset();
    });

    expect(result.current.state.reasoning_content).toBeNull();
    expect(result.current.state.status).toBe("idle");
  });

  it("reasoning_content resets to null when a new stream() call starts", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    // First stream: has reasoning.
    const body1 =
      sseFrameEmpty("sub.reasoning.start") +
      sseFrame("sub.reasoning.delta", { delta: "old thinking" }) +
      sseFrame("sub.complete", { final_content: "first" });

    // Second stream: no reasoning.
    const body2 = sseFrame("sub.complete", { final_content: "second" });

    const fetchSpy = vi.fn()
      .mockResolvedValueOnce(
        new Response(buildSseStream(body1), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(buildSseStream(body2), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      );
    global.fetch = fetchSpy as typeof global.fetch;

    const { result } = renderHook(() => useSubSessionSSE());

    // First stream.
    await act(async () => {
      result.current.stream(baseParams);
    });
    await waitFor(() => expect(result.current.state.status).toBe("complete"));
    expect(result.current.state.reasoning_content).toBe("old thinking");

    // Second stream — reasoning_content must reset to null on start.
    await act(async () => {
      result.current.stream(baseParams);
    });
    await waitFor(() => expect(result.current.state.status).toBe("complete"));
    expect(result.current.state.reasoning_content).toBeNull();
  });
});
