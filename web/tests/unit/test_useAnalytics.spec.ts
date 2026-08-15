/**
 * Unit tests for useMyAnalytics / useSystemAnalytics hooks.
 *
 * P8c Item 12. Covers query hook + error path (Invariant 2).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mocks ────────────────────────────────────────────────────────────────────

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: { request: (...args: unknown[]) => mockRequest(...args) },
  ApiClient: vi.fn(),
}));

// Admin user — useSystemAnalytics is gated on is_admin, so these hook tests
// need an admin. (There was a second, shadowed vi.mock for this same module
// with is_admin:false; vi.mock is last-wins, so it was dead — removed.)
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      user: { id: 1, username: "alice", is_admin: true },
      isInitializing: false,
      isLoading: false,
      error: null,
    };
    if (typeof selector === "function") return selector(state);
    return state;
  },
}));

// ─── Wrapper ──────────────────────────────────────────────────────────────────

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

// ─── useMyAnalytics ───────────────────────────────────────────────────────────

describe("useMyAnalytics", () => {
  it("fetches /api/analytics/me on mount", async () => {
    const { useMyAnalytics } = await import("@/hooks/useAnalytics");
    const { wrapper } = makeWrapper();
    const payload = {
      total_messages: 10,
      total_chats: 3,
      messages_last_7_days: 5,
      top_models: [{ model_id: "llama-3", count: 8 }],
    };
    mockRequest.mockResolvedValue(payload);

    const { result } = renderHook(() => useMyAnalytics(), { wrapper });
    await waitFor(() => { expect(result.current.isSuccess).toBe(true); }, {
      timeout: 2500,
    });

    expect(mockRequest).toHaveBeenCalledWith("/api/analytics/me");
    expect(result.current.data?.total_messages).toBe(10);
  });

  it("isError is true on rejection", async () => {
    const { useMyAnalytics } = await import("@/hooks/useAnalytics");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("fail"), { status: 401 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useMyAnalytics(), { wrapper });
    await waitFor(() => { expect(result.current.isError).toBe(true); }, {
      timeout: 2500,
    });
    expect((result.current.error as { status?: number } | null)?.status).toBe(401);
  });
});

// ─── useSystemAnalytics ───────────────────────────────────────────────────────

describe("useSystemAnalytics", () => {
  it("fetches /api/analytics/system when user is_admin is true", async () => {
    const { useSystemAnalytics } = await import("@/hooks/useAnalytics");
    const { wrapper } = makeWrapper();
    const payload = {
      total_users: 10,
      total_chats: 50,
      total_messages: 200,
      messages_last_7_days: 30,
      top_models: [],
    };
    mockRequest.mockResolvedValue(payload);

    const { result } = renderHook(() => useSystemAnalytics(), { wrapper });
    await waitFor(() => { expect(result.current.isSuccess).toBe(true); }, {
      timeout: 2500,
    });

    expect(mockRequest).toHaveBeenCalledWith("/api/analytics/system");
    expect(result.current.data?.total_users).toBe(10);
  });

  it("isError is true on 403 rejection", async () => {
    const { useSystemAnalytics } = await import("@/hooks/useAnalytics");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("forbidden"), { status: 403 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useSystemAnalytics(), { wrapper });
    await waitFor(() => { expect(result.current.isError).toBe(true); }, {
      timeout: 2500,
    });
  });
});
