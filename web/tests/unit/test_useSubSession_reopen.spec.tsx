/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSubSession — P4 history + reopen + continue.
 *
 * Complements test_useSubSession_restore.spec.tsx (P3 — auto-restore of a
 * genuinely LIVE session only). This file covers the P4 additions:
 *
 *   openSubSessionHistory() — fetches `GET /sub-sessions` on demand and
 *     shows the browse view.
 *   reopenSubSession(id)    — fetches `GET /sub-sessions/{id}` and loads its
 *     FULL transcript into the panel for ANY status (final or aborted, NOT
 *     just a still-live one — that's the whole point of a manual reopen
 *     vs. P3's live-only auto-restore), then closes the browse view.
 *   continue                — once `subSession.subSessionId` is set (by a
 *     restore OR a reopen), the next `maybeRouteSubmit` forwards it as the
 *     `sub_session_id` continuation param so the backend APPENDS the turn
 *     onto the SAME durable row instead of starting a new one.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { createElement } from "react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// ─── Mock api module (usePresetModels + the P3/P4 fetch helpers all route
// through api.request) ────────────────────────────────────────────────────

const mockRequest = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args),
    postForm: vi.fn(),
  },
  ApiClient: vi.fn(),
}));

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

const baseSubSessionRow = {
  id: 9,
  chat_id: 42,
  preset_id: "coder",
  title: "Fix the flaky test",
  status: "final",
  model_id: "qwen3.6",
  created_at: "2026-08-10T10:00:00Z",
  updated_at: "2026-08-10T10:05:00Z",
};

function makeDetail(status: string) {
  return {
    ...baseSubSessionRow,
    status,
    messages: [
      {
        id: 1,
        sub_session_id: 9,
        role: "user",
        content: "fix the flaky test",
        reasoning_content: null,
        state: "final",
        tool_calls: null,
        response_id: null,
        stop_reason: null,
        model_id: null,
        created_at: "2026-08-10T10:00:00Z",
      },
      {
        id: 2,
        sub_session_id: 9,
        role: "assistant",
        content: "Found it — a race in the disconnect watcher.",
        reasoning_content: null,
        state: "final",
        tool_calls: null,
        response_id: null,
        stop_reason: null,
        model_id: "qwen3.6",
        created_at: "2026-08-10T10:05:00Z",
      },
    ],
  };
}

function mockRoutes(routes: Record<string, unknown>) {
  mockRequest.mockImplementation((path: string) => {
    if (path === "/api/settings/preset-models") return Promise.resolve({});
    for (const [prefix, value] of Object.entries(routes)) {
      if (path === prefix) return Promise.resolve(value);
    }
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

const noopArgs = {
  currentChat: undefined,
  selectedModel: "qwen3.6",
  savedDefaultModel: undefined,
  resolveTurnModel: () => "qwen3.6",
  push: vi.fn(),
  refetchMessages: vi.fn(async () => undefined),
};

function buildSseStream(text: string): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

describe("useSubSession — P4 history + reopen + continue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("openSubSessionHistory fetches the chat's sub-sessions and shows the browse view", async () => {
    mockRoutes({ "/api/chats/42/sub-sessions": [baseSubSessionRow] });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    expect(result.current.isSubSessionHistoryOpen).toBe(false);

    act(() => {
      result.current.openSubSessionHistory();
    });

    expect(result.current.isSubSessionHistoryOpen).toBe(true);
    await waitFor(() => {
      expect(result.current.subSessionHistory).not.toBeNull();
    });
    expect(result.current.subSessionHistory).toHaveLength(1);
    expect(result.current.subSessionHistory?.[0]?.id).toBe(9);
    expect(result.current.subSessionHistoryLoading).toBe(false);
  });

  it("closeSubSessionHistory hides the browse view", async () => {
    mockRoutes({ "/api/chats/42/sub-sessions": [baseSubSessionRow] });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    act(() => {
      result.current.openSubSessionHistory();
    });
    expect(result.current.isSubSessionHistoryOpen).toBe(true);

    act(() => {
      result.current.closeSubSessionHistory();
    });
    expect(result.current.isSubSessionHistoryOpen).toBe(false);
  });

  it("reopenSubSession loads a FINISHED session's full transcript into the panel", async () => {
    mockRoutes({
      "/api/chats/42/sub-sessions": [baseSubSessionRow],
      "/api/chats/42/sub-sessions/9": makeDetail("final"),
    });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    // P3's restore-on-load runs on mount too — the newest row is
    // status=final (not live), so it must NOT have auto-restored anything.
    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith("/api/chats/42/sub-sessions/9");
    });
    expect(result.current.subSession).toBeNull();
    mockRequest.mockClear();
    mockRoutes({
      "/api/chats/42/sub-sessions": [baseSubSessionRow],
      "/api/chats/42/sub-sessions/9": makeDetail("final"),
    });

    act(() => {
      result.current.openSubSessionHistory();
    });
    act(() => {
      result.current.reopenSubSession(9);
    });

    await waitFor(() => {
      expect(result.current.subSession).not.toBeNull();
    });
    expect(result.current.subSession?.subSessionId).toBe(9);
    expect(result.current.subSession?.presetId).toBe("coder");
    expect(result.current.subSession?.presetLabel).toBe("Coder");
    expect(result.current.subSession?.messages).toHaveLength(2);
    expect(result.current.subSession?.messages[0]).toMatchObject({
      role: "user",
      content: "fix the flaky test",
    });
    expect(result.current.subSession?.messages[1]).toMatchObject({
      role: "assistant",
      content: "Found it — a race in the disconnect watcher.",
      id: 2,
    });
    // Reopening closes the browse view — the panel now shows the
    // transcript, not the list it was picked from.
    expect(result.current.isSubSessionHistoryOpen).toBe(false);
  });

  it("reopenSubSession works for an ABORTED session too, not just a finished one", async () => {
    mockRoutes({
      "/api/chats/42/sub-sessions": [{ ...baseSubSessionRow, status: "aborted" }],
      "/api/chats/42/sub-sessions/9": makeDetail("aborted"),
    });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    act(() => {
      result.current.reopenSubSession(9);
    });

    await waitFor(() => {
      expect(result.current.subSession).not.toBeNull();
    });
    expect(result.current.subSession?.subSessionId).toBe(9);
  });

  it("continuing a reopened session sends its sub_session_id as the continuation param", async () => {
    mockRoutes({
      "/api/chats/42/sub-sessions": [baseSubSessionRow],
      "/api/chats/42/sub-sessions/9": makeDetail("final"),
    });

    const sseBody =
      "event: sub.complete\ndata: " +
      JSON.stringify({ final_content: "it was a coalesce race" }) +
      "\n\n";
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(buildSseStream(sseBody), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );
    global.fetch = fetchSpy as typeof global.fetch;

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    act(() => {
      result.current.reopenSubSession(9);
    });
    await waitFor(() => {
      expect(result.current.subSession?.subSessionId).toBe(9);
    });

    act(() => {
      result.current.maybeRouteSubmit(42, { input: [] }, "why did it happen?");
    });

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalled();
    });
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/chats/42/sub-session/stream");
    const form = init.body as FormData;
    expect(form.get("sub_session_id")).toBe("9");

    await waitFor(() => {
      expect(result.current.subSession?.messages).toHaveLength(4);
    });
    expect(result.current.subSession?.messages[2]).toMatchObject({
      role: "user",
      content: "why did it happen?",
    });
    expect(result.current.subSession?.messages[3]).toMatchObject({
      role: "assistant",
      content: "it was a coalesce race",
    });
  });

  it("a FRESH (never restored/reopened) session's turns omit sub_session_id", async () => {
    mockRoutes({ "/api/chats/42/sub-sessions": [] });

    const sseBody =
      "event: sub.complete\ndata: " +
      JSON.stringify({ final_content: "hi there" }) +
      "\n\n";
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(buildSseStream(sseBody), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );
    global.fetch = fetchSpy as typeof global.fetch;

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    let started: ReturnType<typeof result.current.startSubSession> = null;
    act(() => {
      started = result.current.startSubSession("coder");
    });
    expect(started).not.toBeNull();
    expect(started?.subSessionId).toBeNull();

    act(() => {
      result.current.maybeRouteSubmit(42, { input: [] }, "hello", started);
    });

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalled();
    });
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    const form = init.body as FormData;
    expect(form.get("sub_session_id")).toBeNull();
  });
});
