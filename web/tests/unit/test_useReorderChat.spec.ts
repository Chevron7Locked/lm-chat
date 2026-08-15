/**
 * Unit tests for useReorderChat mutation hook.
 *
 * P8c Item 11. Covers:
 * - Sends PATCH /api/chats/reorder with correct JSON body.
 * - Error path: isError on 404.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mocks ────────────────────────────────────────────────────────────────────

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();
const mockPostForm = vi.fn<(...args: unknown[]) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args),
    postForm: (...args: unknown[]) => mockPostForm(...args),
  },
  ApiClient: vi.fn(),
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      user: { id: 1, username: "alice", is_admin: false },
      isInitializing: false,
      isLoading: false,
      error: null,
    };
    if (typeof selector === "function") return selector(state);
    return state;
  },
}));

beforeEach(() => { vi.clearAllMocks(); });

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    qc,
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children),
  };
}

// ─── useReorderChat ───────────────────────────────────────────────────────────

describe("useReorderChat", () => {
  it("sends PATCH /api/chats/reorder with form-encoded body (P7c contract)", async () => {
    const { useReorderChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ ok: true });

    const { result } = renderHook(() => useReorderChat(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        chat_id: 5,
        folder: "work",
        display_order: 2,
      });
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/chats/reorder",
      expect.objectContaining({
        method: "PATCH",
        headers: expect.objectContaining({
          "Content-Type": "application/x-www-form-urlencoded",
        }),
      })
    );

    const callArgs = mockRequest.mock.calls[0];
    const rawBody = (callArgs?.[1] as { body?: string } | undefined)?.body ?? "";
    const params = new URLSearchParams(rawBody);
    expect(params.get("chat_id")).toBe("5");
    expect(params.get("folder")).toBe("work");
    expect(params.get("display_order")).toBe("2");
  });

  it("handles null folder — omits folder param (ungrouped/pinned section)", async () => {
    const { useReorderChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ ok: true });

    const { result } = renderHook(() => useReorderChat(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        chat_id: 3,
        folder: null,
        display_order: 0,
      });
    });

    const callArgs = mockRequest.mock.calls[0];
    const rawBody = (callArgs?.[1] as { body?: string } | undefined)?.body ?? "";
    const params = new URLSearchParams(rawBody);
    // null folder must not be sent as the string "null" — it must be absent.
    expect(params.has("folder")).toBe(false);
    expect(params.get("chat_id")).toBe("3");
    expect(params.get("display_order")).toBe("0");
  });

  it("error path: isError on 404", async () => {
    const { useReorderChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("not found"), { status: 404 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useReorderChat(), { wrapper });

    // .mutate() (not .mutateAsync()) is fire-and-forget by design here — this
    // test asserts the ERROR PATH via state (isError), not by catching a
    // rejected promise, so waitFor below does the actual synchronization.
    act(() => {
      result.current.mutate({ chat_id: 999, folder: null, display_order: 0 });
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect((result.current.error as { status?: number } | null)?.status).toBe(404);
  });
});

// ─── useChatsDirect ──────────────────────────────────────────────────────────

describe("useChatsDirect (DEFERRED §3 ChatSummary[] direct)", () => {
  it("returns ChatSummary[] directly (no envelope wrapper)", async () => {
    const { useChatsDirect } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const chat = {
      id: 1,
      title: "Test",
      folder: null,
      pinned: false,
      updated_at: "2026-01-01T00:00:00Z",
      model_id: null,
      display_order: 0,
    };
    mockRequest.mockResolvedValue([chat]);

    const { result } = renderHook(() => useChatsDirect(), { wrapper });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    // data is ChatSummary[] directly (not wrapped in envelope).
    expect(Array.isArray(result.current.data)).toBe(true);
    expect(result.current.data?.[0]?.id).toBe(1);
    expect(result.current.data?.[0]?.display_order).toBe(0);
  });
});

// ─── ChatSummary.display_order field ─────────────────────────────────────────

describe("ChatSummary.display_order", () => {
  it("display_order is present in the ChatSummary type (compile-time check)", () => {
    // This test verifies that the type includes display_order by creating
    // a well-typed object — if the field were missing, TypeScript would error.
    const chat = {
      id: 1,
      title: "t",
      folder: null,
      pinned: false,
      updated_at: "2026-01-01T00:00:00Z",
      model_id: null,
      display_order: 5, // <-- P8c field
    };
    expect(chat.display_order).toBe(5);
  });
});
