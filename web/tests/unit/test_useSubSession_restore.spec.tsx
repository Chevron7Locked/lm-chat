/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSubSession — P3 restore-on-load.
 *
 * Durable sub-sessions (migration 0045 + P2's persist-through-the-draft-
 * state-machine) already survive a reload at the DB layer; this is what
 * makes that visible in the FE. On mount / chatId change, the hook fetches
 * `GET /api/chats/{id}/sub-sessions` and, only when the newest entry is
 * genuinely still LIVE (D9 — its newest transcript row's `state` is
 * `draft` or `pending_finalization`, not `sub_sessions.status` alone),
 * restores it from `GET /api/chats/{id}/sub-sessions/{sid}` into the
 * panel. A finished/aborted session must NOT auto-restore — that's P4's
 * history/reopen surface.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// ─── Mock api module (usePresetModels + the P3 fetch helpers both route
// through api.request) ────────────────────────────────────────────────────

const mockRequest = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: [path: string, init?: RequestInit]) => mockRequest(...args),
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
  id: 5,
  chat_id: 42,
  preset_id: "research",
  title: "What's the capital of France?",
  status: "active",
  model_id: "qwen3.6",
  created_at: "2026-08-12T10:00:00Z",
  updated_at: "2026-08-12T10:00:00Z",
};

function makeDetail(newestState: string) {
  return {
    ...baseSubSessionRow,
    messages: [
      {
        id: 1,
        sub_session_id: 5,
        role: "user",
        content: "What's the capital of France?",
        reasoning_content: null,
        state: "final",
        tool_calls: null,
        response_id: null,
        stop_reason: null,
        model_id: null,
        created_at: "2026-08-12T10:00:00Z",
      },
      {
        id: 2,
        sub_session_id: 5,
        role: "assistant",
        content: "Paris is the capital of France.",
        reasoning_content: null,
        state: newestState,
        tool_calls: null,
        response_id: null,
        stop_reason: null,
        model_id: "qwen3.6",
        created_at: "2026-08-12T10:00:05Z",
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
  selectedModel: undefined,
  savedDefaultModel: undefined,
  resolveTurnModel: () => "",
  push: vi.fn(),
  refetchMessages: vi.fn(() => Promise.resolve(undefined)),
};

describe("useSubSession — restore-on-load (P3)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("restores a genuinely live sub-session (newest row state=draft)", async () => {
    mockRoutes({
      "/api/chats/42/sub-sessions": [baseSubSessionRow],
      "/api/chats/42/sub-sessions/5": makeDetail("draft"),
    });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.subSession).not.toBeNull();
    });

    expect(result.current.subSession?.presetId).toBe("research");
    expect(result.current.subSession?.presetLabel).toBe("Research");
    expect(result.current.subSession?.subSessionId).toBe(5);
    expect(result.current.subSession?.messages).toHaveLength(2);
    expect(result.current.subSession?.messages[0]).toMatchObject({
      role: "user",
      content: "What's the capital of France?",
    });
    expect(result.current.subSession?.messages[1]).toMatchObject({
      role: "assistant",
      content: "Paris is the capital of France.",
      id: 2,
    });
    // A restored session hasn't been finalized yet — the panel should
    // offer "Summarize → main chat", not show a stale finalContent.
    expect(result.current.subSession?.finalContent).toBeNull();
    expect(result.current.subSession?.finalizing).toBe(false);
  });

  it("does NOT restore a finalizing sub-session (newest row state=pending_finalization)", async () => {
    // pending_finalization = the turn's answer completed + finalize step 1 ran;
    // it is DONE (awaiting the reaper's step-2 commit), not streaming — so it is
    // recovered via P4 reopen, not auto-restored as live (dogfood-found: a
    // reload landing during the finalize left the row here for a beat).
    mockRoutes({
      "/api/chats/42/sub-sessions": [baseSubSessionRow],
      "/api/chats/42/sub-sessions/5": makeDetail("pending_finalization"),
    });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith("/api/chats/42/sub-sessions/5");
    });
    expect(result.current.subSession).toBeNull();
  });

  it("does NOT restore a finished sub-session (newest row state=final)", async () => {
    mockRoutes({
      "/api/chats/42/sub-sessions": [{ ...baseSubSessionRow, status: "final" }],
      "/api/chats/42/sub-sessions/5": makeDetail("final"),
    });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    // Let the fetches resolve, then assert nothing was restored.
    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith("/api/chats/42/sub-sessions/5");
    });
    expect(result.current.subSession).toBeNull();
  });

  it("does NOT restore an aborted sub-session (newest row state=aborted_by_client)", async () => {
    mockRoutes({
      "/api/chats/42/sub-sessions": [{ ...baseSubSessionRow, status: "aborted" }],
      "/api/chats/42/sub-sessions/5": makeDetail("aborted_by_client"),
    });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith("/api/chats/42/sub-sessions/5");
    });
    expect(result.current.subSession).toBeNull();
  });

  it("does NOT restore when the chat has no sub-sessions", async () => {
    mockRoutes({ "/api/chats/42/sub-sessions": [] });

    const { useSubSession } = await import("@/hooks/useSubSession");
    const { result } = renderHook(() => useSubSession({ chatId: 42, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith("/api/chats/42/sub-sessions");
    });
    expect(result.current.subSession).toBeNull();
  });

  it("does not fetch sub-sessions when chatId is null", async () => {
    mockRoutes({});

    const { useSubSession } = await import("@/hooks/useSubSession");
    renderHook(() => useSubSession({ chatId: null, ...noopArgs }), {
      wrapper: makeWrapper(),
    });

    expect(mockRequest).not.toHaveBeenCalledWith(
      expect.stringContaining("/sub-sessions"),
    );
  });
});
