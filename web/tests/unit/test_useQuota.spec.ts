/**
 * Unit tests for useQuotaMe, useAdminQuotaList, useUpdateQuota.
 *
 * Strategy: wrap in QueryClientProvider; mock api.request / api.postForm;
 * verify query key shapes, enabled flags, and mutation side-effects
 * (including 429 error path for Invariant 2).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mock api module ─────────────────────────────────────────────────────────

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();
const mockPostForm = vi.fn<(...args: unknown[]) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args),
    postForm: (...args: unknown[]) => mockPostForm(...args),
  },
  ApiClient: vi.fn(),
}));

// ─── Mock authStore ──────────────────────────────────────────────────────────

const mockAuthState = {
  user: { id: 1, username: "alice", is_admin: false } as {
    id: number;
    username: string;
    is_admin: boolean;
  } | null,
  isInitializing: false,
};

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      ...mockAuthState,
      isLoading: false,
      error: null,
    };
    if (typeof selector === "function") return selector(state);
    return state;
  },
}));

// ─── Mock toastStore ─────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
}));

// ─── Wrapper factory ─────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks();
  mockAuthState.user = { id: 1, username: "alice", is_admin: false };
  mockAuthState.isInitializing = false;
});

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

// ─── quotaKeys ───────────────────────────────────────────────────────────────

describe("quotaKeys", () => {
  it("me() key includes 'quotas' and 'me'", async () => {
    const { quotaKeys } = await import("@/hooks/useQuota");
    expect(quotaKeys.me()).toContain("quotas");
    expect(quotaKeys.me()).toContain("me");
  });

  it("adminList() key includes 'admin'", async () => {
    const { quotaKeys } = await import("@/hooks/useQuota");
    expect(quotaKeys.adminList()).toContain("admin");
  });
});

// ─── useQuotaMe ──────────────────────────────────────────────────────────────

describe("useQuotaMe", () => {
  it("fetches /api/quotas/me when user is authenticated", async () => {
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValueOnce({
      tokens_per_day: 100_000,
      requests_per_day: 1_000,
      tokens_consumed_today: 0,
      requests_consumed_today: 0,
      resets_at: "2026-05-21T00:00:00+00:00",
    });

    const { useQuotaMe } = await import("@/hooks/useQuota");
    const { result } = renderHook(() => useQuotaMe(), { wrapper });
    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });

    expect(mockRequest).toHaveBeenCalledWith("/api/quotas/me");
    expect(result.current.data?.tokens_per_day).toBe(100_000);
  });

  it("is disabled when user is null", async () => {
    mockAuthState.user = null;
    const { wrapper } = makeWrapper();

    const { useQuotaMe } = await import("@/hooks/useQuota");
    const { result } = renderHook(() => useQuotaMe(), { wrapper });

    // Should not fetch.
    expect(result.current.isLoading).toBe(false);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("is disabled when isInitializing is true", async () => {
    mockAuthState.isInitializing = true;
    const { wrapper } = makeWrapper();

    const { useQuotaMe } = await import("@/hooks/useQuota");
    const { result } = renderHook(() => useQuotaMe(), { wrapper });

    expect(result.current.isLoading).toBe(false);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("surfaces ApiError on 429 from the query", async () => {
    const err = new Error("Daily quota exceeded") as Error & { status: number; detail?: string };
    err.status = 429;
    err.detail = "request quota exceeded for the day";
    mockRequest.mockRejectedValueOnce(err);

    const { wrapper } = makeWrapper();
    const { useQuotaMe } = await import("@/hooks/useQuota");
    const { result } = renderHook(() => useQuotaMe(), { wrapper });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect(result.current.error?.status).toBe(429);
  });
});

// ─── useAdminQuotaList ───────────────────────────────────────────────────────

describe("useAdminQuotaList", () => {
  it("is disabled for non-admin users", async () => {
    // user.is_admin = false (default)
    const { wrapper } = makeWrapper();
    const { useAdminQuotaList } = await import("@/hooks/useQuota");
    const { result } = renderHook(() => useAdminQuotaList(), { wrapper });

    expect(result.current.isLoading).toBe(false);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("fetches /api/admin/quotas for admin users", async () => {
    mockAuthState.user = { id: 1, username: "admin", is_admin: true };
    mockRequest.mockResolvedValueOnce([
      { user_id: 2, tokens_per_day: 5000, requests_per_day: 100 },
    ]);

    const { wrapper } = makeWrapper();
    const { useAdminQuotaList } = await import("@/hooks/useQuota");
    const { result } = renderHook(() => useAdminQuotaList(), { wrapper });
    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });

    expect(mockRequest).toHaveBeenCalledWith("/api/admin/quotas");
    expect(Array.isArray(result.current.data)).toBe(true);
  });
});

// ─── useUpdateQuota ──────────────────────────────────────────────────────────

describe("useUpdateQuota", () => {
  it("calls postForm with correct params on success", async () => {
    mockPostForm.mockResolvedValueOnce({
      user_id: 7,
      tokens_per_day: 50_000,
      requests_per_day: 500,
    });

    const { wrapper, qc } = makeWrapper();
    const { useUpdateQuota, quotaKeys } = await import("@/hooks/useQuota");
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useUpdateQuota(), { wrapper });

    act(() => {
      result.current.mutate({ userId: 7, tokensPerDay: 50_000, requestsPerDay: 500 });
    });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(mockPostForm).toHaveBeenCalledWith("/api/admin/quotas/7", {
      tokens_per_day: "50000",
      requests_per_day: "500",
    });
    // useUpdateQuota's onSuccess invalidates the admin list so the quota
    // table refetches instead of showing the pre-update value — this is
    // the assertion the destructured-but-unused `qc` was a fossil of
    // (see d6e07c4's commit message).
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: quotaKeys.adminList() });
  });

  it("pushes error toast on mutation failure (Invariant 2)", async () => {
    const err = new Error("Server error") as Error & { status: number; detail?: string };
    err.status = 500;
    err.detail = "internal server error";
    mockPostForm.mockRejectedValueOnce(err);

    const { wrapper } = makeWrapper();
    const { useUpdateQuota } = await import("@/hooks/useQuota");
    const { result } = renderHook(() => useUpdateQuota(), { wrapper });

    act(() => {
      result.current.mutate({ userId: 7, tokensPerDay: 1000, requestsPerDay: 10 });
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "error" }),
    );
  });

  it("pushes error toast on 422 unprocessable entity (Invariant 2)", async () => {
    const err = new Error("Unprocessable") as Error & { status: number; detail?: string };
    err.status = 422;
    err.detail = "value error";
    mockPostForm.mockRejectedValueOnce(err);

    const { wrapper } = makeWrapper();
    const { useUpdateQuota } = await import("@/hooks/useQuota");
    const { result } = renderHook(() => useUpdateQuota(), { wrapper });

    act(() => {
      result.current.mutate({ userId: 3, tokensPerDay: -1, requestsPerDay: 0 });
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "error" }),
    );
  });
});
