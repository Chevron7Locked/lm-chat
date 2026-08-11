/* SPDX-License-Identifier: Apache-2.0 */
/**
 * A-autotitle-verify — useGenerateTitle hook unit tests.
 *
 * AC23 — mutation success patches chatKeys.list() cache without a refetch.
 * AC24 — mutation rejection propagates to the caller (hook does NOT swallow).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mock api module ──────────────────────────────────────────────────────────

const mockRequest = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args),
    postForm: vi.fn(),
  },
  ApiClient: vi.fn(),
}));

// ─── Mock authStore (prevents enabled-guard from blocking) ───────────────────

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      user: { id: 1, username: "test", is_admin: false },
      isInitializing: false,
      isLoading: false,
      error: null,
    };
    if (typeof selector === "function") return selector(state);
    return state;
  },
}));

// ─── Wrapper factory ──────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks();
});

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return {
    qc,
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children),
  };
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("useGenerateTitle — AC23: mutation success patches chatKeys.list() cache", () => {
  it("updates matching chat row title in the list cache without a refetch", async () => {
    const { useGenerateTitle, chatKeys } = await import("@/hooks/useChats");
    const { qc, wrapper } = makeWrapper();

    // Pre-seed the list cache with two chat rows.
    qc.setQueryData(chatKeys.list(), [
      { id: 7, title: "New Chat", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null },
      { id: 8, title: "Other",    folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null },
    ]);

    // Mock api.request to resolve with the new title.
    mockRequest.mockResolvedValue({ title: "Cool" });

    const { result } = renderHook(() => useGenerateTitle(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(7);
    });

    // After onSuccess runs, the cache should be patched.
    await waitFor(() => {
      const cached = qc.getQueryData<Array<{ id: number; title: string }>>(chatKeys.list());
      expect(cached).toBeDefined();
      const chat7 = cached?.find((c) => c.id === 7);
      const chat8 = cached?.find((c) => c.id === 8);
      expect(chat7?.title).toBe("Cool");
      // Chat 8 must be untouched.
      expect(chat8?.title).toBe("Other");
    });
  });
});

describe("useGenerateTitle — AC24: mutation rejection propagates to caller", () => {
  it("mutateAsync rejects when api.request rejects with a 502-shaped error", async () => {
    const { useGenerateTitle } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();

    // Construct a 502-shaped ApiError (matches ApiError interface from api.ts).
    const apiError = Object.assign(new Error("upstream returned status 500"), {
      status: 502,
      detail: "upstream returned status 500",
    });
    mockRequest.mockRejectedValue(apiError);

    const { result } = renderHook(() => useGenerateTitle(), { wrapper });

    // mutateAsync must reject — the hook does NOT swallow it.
    await expect(
      act(async () => {
        await result.current.mutateAsync(7);
      })
    ).rejects.toThrow();
  });
});
