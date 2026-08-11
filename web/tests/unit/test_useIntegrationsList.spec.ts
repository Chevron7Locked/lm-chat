/**
 * Unit tests for useIntegrationsList and useUpdateIntegrationsList.
 *
 * Strategy: wrap in QueryClientProvider; mock api.request; verify
 * query key shapes, return data, and mutation invalidation.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mock api module ─────────────────────────────────────────────────────────

const mockRequest = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args),
  },
  ApiClient: vi.fn(),
}));

// ─── Import after mock ────────────────────────────────────────────────────────

import {
  useIntegrationsList,
  useUpdateIntegrationsList,
  INTEGRATIONS_LIST_KEY,
} from "@/hooks/useIntegrationsList";
import type { IntegrationEntry } from "@/hooks/useIntegrationsList";

// ─── Helpers ─────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks();
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

const STUB_ENTRIES: IntegrationEntry[] = [
  {
    id: 1,
    value: "mcp/searxng",
    sort_order: 0,
    created_at: "2026-05-21T00:00:00Z",
    updated_at: "2026-05-21T00:00:00Z",
  },
  {
    id: 2,
    value: "mcp/filesystem",
    sort_order: 1,
    created_at: "2026-05-21T00:00:00Z",
    updated_at: "2026-05-21T00:00:00Z",
  },
];

// ─── useIntegrationsList ──────────────────────────────────────────────────────

describe("useIntegrationsList", () => {
  it("fetches GET /api/integrations/available", async () => {
    mockRequest.mockResolvedValueOnce(STUB_ENTRIES);
    const { qc, wrapper } = makeWrapper();
    const { result } = renderHook(() => useIntegrationsList(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(mockRequest).toHaveBeenCalledWith("/api/integrations/available");
    expect(result.current.data).toEqual(STUB_ENTRIES);
    qc.clear();
  });

  it("returns empty array on empty list", async () => {
    mockRequest.mockResolvedValueOnce([]);
    const { qc, wrapper } = makeWrapper();
    const { result } = renderHook(() => useIntegrationsList(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
    qc.clear();
  });

  it("uses INTEGRATIONS_LIST_KEY as query key", async () => {
    mockRequest.mockResolvedValueOnce([]);
    const { qc, wrapper } = makeWrapper();
    renderHook(() => useIntegrationsList(), { wrapper });

    await waitFor(() =>
      expect(qc.getQueryData(INTEGRATIONS_LIST_KEY)).toBeDefined()
    );
    qc.clear();
  });

  it("exposes isLoading and error states", async () => {
    const err = new Error("401 Unauthorized") as Error & { status: number };
    err.status = 401;
    // Hook configures retry: 1 (2 total attempts) with default exponential
    // backoff (~1s before retry). Reject on all calls and bump the waitFor
    // timeout to cover the retry window.
    mockRequest.mockRejectedValue(err);
    const { qc, wrapper } = makeWrapper();
    const { result } = renderHook(() => useIntegrationsList(), { wrapper });

    await waitFor(
      () => expect(result.current.isError).toBe(true),
      { timeout: 3000 }
    );
    expect(result.current.error?.message).toMatch(/401/);
    qc.clear();
  });
});

// ─── useUpdateIntegrationsList ────────────────────────────────────────────────

describe("useUpdateIntegrationsList", () => {
  it("calls PUT /api/integrations/available with form-encoded entries", async () => {
    mockRequest.mockResolvedValueOnce(STUB_ENTRIES); // GET initial
    mockRequest.mockResolvedValueOnce(STUB_ENTRIES); // PUT response
    const { qc, wrapper } = makeWrapper();

    const { result } = renderHook(() => useUpdateIntegrationsList(), { wrapper });

    await act(async () => {
      result.current.mutate([
        { value: "mcp/searxng", sort_order: 0 },
        { value: "mcp/filesystem", sort_order: 1 },
      ]);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [path, init] = mockRequest.mock.calls[0] as [
      string,
      { method: string; headers: Record<string, string>; body: string },
    ];
    expect(path).toBe("/api/integrations/available");
    expect(init.method).toBe("PUT");
    expect(init.headers?.["Content-Type"]).toBe(
      "application/x-www-form-urlencoded"
    );
    // Body must be URL-encoded with entries= field.
    expect(init.body).toMatch(/^entries=/);
    qc.clear();
  });

  it("invalidates INTEGRATIONS_LIST_KEY on success", async () => {
    const newEntries: IntegrationEntry[] = [
      {
        id: 3,
        value: "mcp/new",
        sort_order: 0,
        created_at: "2026-05-21T00:00:00Z",
        updated_at: "2026-05-21T00:00:00Z",
      },
    ];
    mockRequest.mockResolvedValueOnce(newEntries); // mutation response
    const { qc, wrapper } = makeWrapper();

    // Seed cache with old data.
    qc.setQueryData(INTEGRATIONS_LIST_KEY, STUB_ENTRIES);

    const { result } = renderHook(() => useUpdateIntegrationsList(), { wrapper });

    await act(async () => {
      result.current.mutate([{ value: "mcp/new", sort_order: 0 }]);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // After success, the query key should be invalidated (marked stale).
    const queryState = qc.getQueryState(INTEGRATIONS_LIST_KEY);
    expect(queryState?.isInvalidated).toBe(true);
    qc.clear();
  });
});
