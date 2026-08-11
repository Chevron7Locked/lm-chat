/**
 * Unit tests for useABStream hook.
 *
 * Covers:
 * - Two-pane state machine: idle → streaming → complete.
 * - Error in one pane does not break the other (Invariant: independent panes).
 * - paneA and paneB accumulate contentDeltas correctly.
 * - stop() transitions to idle.
 * - 401 / network error handling.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

// ─── Mock authStore ───────────────────────────────────────────────────────────

// Mock authStore: return authenticated user, initialization complete.
// NOTE: Tests for the "null user" gate use a separate approach (in-test spy)
// rather than vi.resetModules() to avoid clearing the top-level factory mock.
vi.mock("@/stores/authStore", () => {
  const mockUser = { id: 1, username: "test", is_admin: false };
  return {
    useAuthStore: vi.fn(() => ({ user: mockUser, isInitializing: false })),
  };
});

// ─── SSE mock helpers ─────────────────────────────────────────────────────────

/**
 * Build a minimal ReadableStream that emits all frames as one chunk, then closes.
 * Uses the start() callback (same pattern as useSSE tests) for jsdom compatibility.
 */
function buildSseStream(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  const allText = frames.join("");
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(allText));
      controller.close();
    },
  });
}

function sseFrame(eventType: string, data: object): string {
  return `event: ${eventType}\ndata: ${JSON.stringify(data)}\n\n`;
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("useABStream", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("initial state is idle with empty panes", async () => {
    const { useABStream } = await import("@/hooks/useABStream");
    const { result } = renderHook(() => useABStream());
    expect(result.current.state.status).toBe("idle");
    expect(result.current.state.paneA.status).toBe("idle");
    expect(result.current.state.paneB.status).toBe("idle");
  });

  it("accumulates content deltas for both panes", async () => {
    const { useABStream } = await import("@/hooks/useABStream");

    // All frames in one chunk so the ReadableStream yields them atomically.
    const allFrames =
      sseFrame("message.delta", { type: "message.delta", pane: "a", delta: "Hello " }) +
      sseFrame("message.delta", { type: "message.delta", pane: "b", delta: "World " }) +
      sseFrame("message.delta", { type: "message.delta", pane: "a", delta: "from A" }) +
      sseFrame("ab.end", { type: "ab.end", pane: "a" }) +
      sseFrame("ab.end", { type: "ab.end", pane: "b" });

    global.fetch = vi.fn().mockResolvedValue(
      new Response(buildSseStream([allFrames]), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      })
    );

    const { result } = renderHook(() => useABStream());

    await act(async () => {
      await result.current.start({
        chatId: 1,
        message: "test",
        modelA: "model-a",
        modelB: "model-b",
      });
    });

    expect(result.current.state.status).toBe("complete");
    const contentA = result.current.state.paneA.contentDeltas.join("");
    const contentB = result.current.state.paneB.contentDeltas.join("");
    expect(contentA).toBe("Hello from A");
    expect(contentB).toBe("World ");
  });

  it("error in pane A does not affect pane B", async () => {
    const { useABStream } = await import("@/hooks/useABStream");

    const allFrames =
      sseFrame("ab.error", { type: "ab.error", pane: "a", code: "model_load_failure", message: "Model not loaded" }) +
      sseFrame("message.delta", { type: "message.delta", pane: "b", delta: "B is fine" }) +
      sseFrame("ab.end", { type: "ab.end", pane: "b" });

    global.fetch = vi.fn().mockResolvedValue(
      new Response(buildSseStream([allFrames]), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      })
    );

    const { result } = renderHook(() => useABStream());

    await act(async () => {
      await result.current.start({
        chatId: 1,
        message: "test",
        modelA: "model-a",
        modelB: "model-b",
      });
    });

    expect(result.current.state.status).toBe("complete");
    // Pane A errored.
    expect(result.current.state.paneA.status).toBe("error");
    expect(result.current.state.paneA.error?.code).toBe("model_load_failure");
    // Pane B completed with content.
    expect(result.current.state.paneB.status).toBe("complete");
    const contentB = result.current.state.paneB.contentDeltas.join("");
    expect(contentB).toBe("B is fine");
  });

  it("HTTP 401 sets error status on both panes", async () => {
    const { useABStream } = await import("@/hooks/useABStream");

    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Not authenticated" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      })
    );

    const { result } = renderHook(() => useABStream());

    await act(async () => {
      await result.current.start({
        chatId: 1,
        message: "test",
        modelA: "model-a",
        modelB: "model-b",
      });
    });

    expect(result.current.state.status).toBe("error");
    expect(result.current.state.paneA.status).toBe("error");
    expect(result.current.state.paneB.status).toBe("error");
    expect(result.current.state.paneA.error?.code).toBe("http_401");
  });

  it("stop() resets status to idle", async () => {
    const { useABStream } = await import("@/hooks/useABStream");
    const { result } = renderHook(() => useABStream());

    // Mock a never-resolving fetch to simulate an in-flight stream.
    global.fetch = vi.fn().mockReturnValue(new Promise(() => { /* never resolves */ }));

    // Start without awaiting.
    act(() => {
      void result.current.start({
        chatId: 1,
        message: "test",
        modelA: "model-a",
        modelB: "model-b",
      });
    });

    act(() => {
      result.current.stop();
    });

    expect(result.current.state.status).toBe("idle");
  });

  it("is a no-op when user is null (gated on auth)", async () => {
    // Override the module-level mock for this test via vi.mocked().
    const { useAuthStore } = await import("@/stores/authStore");
    const mockedStore = vi.mocked(useAuthStore);
    mockedStore.mockReturnValueOnce({ user: null, isInitializing: false });

    const { useABStream } = await import("@/hooks/useABStream");
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy;

    const { result } = renderHook(() => useABStream());

    await act(async () => {
      await result.current.start({
        chatId: 1,
        message: "test",
        modelA: "a",
        modelB: "b",
      });
    });

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(result.current.state.status).toBe("idle");
  });
});
