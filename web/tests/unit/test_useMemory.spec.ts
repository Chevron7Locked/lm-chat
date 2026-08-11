/**
 * Unit tests for useMemory hooks (pins CRUD + reindex).
 *
 * Strategy: wrap in QueryClientProvider; mock api.request; verify
 * correct endpoints are called and query cache is invalidated.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mock api module ─────────────────────────────────────────────────────────

const mockRequest = vi.fn();

vi.mock("@/lib/api", () => ({
  api: { request: (...args: unknown[]) => mockRequest(...args), postForm: vi.fn() },
  ApiClient: vi.fn(),
}));

// ─── Mock authStore — return isInitializing=false, user set ──────────────────

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

// ─── Wrapper factory ─────────────────────────────────────────────────────────

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

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("memoryKeys", () => {
  it("all is ['memory']", async () => {
    const { memoryKeys } = await import("@/hooks/useMemory");
    expect(memoryKeys.all).toEqual(["memory"]);
  });

  it("pins() contains 'pins'", async () => {
    const { memoryKeys } = await import("@/hooks/useMemory");
    expect(memoryKeys.pins()).toContain("pins");
  });
});

describe("useMemoryPins", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("fetches /api/memory/pins on mount", async () => {
    const { useMemoryPins } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    // Backend returns MemoryInsight[] — plain array, no envelope.
    mockRequest.mockResolvedValue([]);

    const { result } = renderHook(() => useMemoryPins(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockRequest).toHaveBeenCalledWith("/api/memory/pins");
  });

  it("propagates MemoryInsight[] as-is (no envelope unwrap)", async () => {
    const { useMemoryPins } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    const insight = { id: 1, text: "test insight", created_at: "2026-01-01T00:00:00Z" };
    // Wire shape: plain array — matches backend response_model=list[MemoryInsight].
    mockRequest.mockResolvedValue([insight]);

    const { result } = renderHook(() => useMemoryPins(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // data is MemoryInsight[] directly (no .pins wrapper).
    expect(Array.isArray(result.current.data)).toBe(true);
    expect(result.current.data?.[0]?.text).toBe("test insight");
  });

  it("isError on rejection", async () => {
    const { useMemoryPins } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    mockRequest.mockRejectedValue(new Error("fail"));

    const { result } = renderHook(() => useMemoryPins(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("usePinInsight", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("POSTs to /api/memory/pin with form-encoded body (backend uses Form(...))", async () => {
    const { usePinInsight } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    const created = { id: 5, text: "new pin", created_at: "2026-01-01T00:00:00Z" };
    mockRequest.mockResolvedValue(created);

    const { result } = renderHook(() => usePinInsight(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ text: "new pin" });
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/memory/pin",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/x-www-form-urlencoded",
        }),
      })
    );
  });

  it("encodes text in the form body", async () => {
    const { usePinInsight } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    const created = { id: 6, text: "hello world", created_at: "2026-01-01T00:00:00Z" };
    mockRequest.mockResolvedValue(created);

    const { result } = renderHook(() => usePinInsight(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ text: "hello world" });
    });

    const callArgs = mockRequest.mock.calls[0];
    const bodyStr = (callArgs?.[1] as { body?: string })?.body ?? "";
    const params = new URLSearchParams(bodyStr);
    expect(params.get("text")).toBe("hello world");
  });
});

describe("useUnpinInsight", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("sends DELETE /api/memory/pin/:id", async () => {
    const { useUnpinInsight } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ status: "ok" });

    const { result } = renderHook(() => useUnpinInsight(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(9);
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/memory/pin/9",
      expect.objectContaining({ method: "DELETE" })
    );
  });
});

describe("useMemoryReindex", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("POSTs to /api/memory/reindex with form-encoded body (backend uses Form(...))", async () => {
    const { useMemoryReindex } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ status: "ok" });

    const { result } = renderHook(() => useMemoryReindex(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ embedding_model_id: "nomic-embed" });
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/memory/reindex",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/x-www-form-urlencoded",
        }),
      })
    );
  });

  it("encodes embedding_model_id in the form body", async () => {
    const { useMemoryReindex } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ status: "ok" });

    const { result } = renderHook(() => useMemoryReindex(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ embedding_model_id: "nomic-embed-text-v1.5" });
    });

    const callArgs = mockRequest.mock.calls[0];
    const bodyStr = (callArgs?.[1] as { body?: string })?.body ?? "";
    const params = new URLSearchParams(bodyStr);
    expect(params.get("embedding_model_id")).toBe("nomic-embed-text-v1.5");
  });
});

// ─── Error-path tests for memory mutation hooks (P8a item 5) ─────────────────

describe("usePinInsight — error path", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("propagates isError=true and error.status on 500", async () => {
    const { usePinInsight } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("server error"), { status: 500, detail: "synthetic" });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => usePinInsight(), { wrapper });

    await act(async () => {
      await result.current.mutate({ text: "fail pin" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as { status?: number } | null)?.status).toBe(500);
  });
});

describe("useMemoryReindex — error path", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("propagates isError=true on 403 (non-admin)", async () => {
    const { useMemoryReindex } = await import("@/hooks/useMemory");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("forbidden"), { status: 403, detail: "admin privileges required" });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useMemoryReindex(), { wrapper });

    await act(async () => {
      await result.current.mutate({ embedding_model_id: "nomic-embed" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as { status?: number } | null)?.status).toBe(403);
  });
});
