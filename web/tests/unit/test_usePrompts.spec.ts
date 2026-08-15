/**
 * Unit tests for usePrompts / mutation hooks.
 *
 * P8c Item 13. Covers:
 * - usePrompts: fetches list[Prompt] from /api/prompts.
 * - useCreatePrompt: posts form-encoded body, error path.
 * - useUpdatePrompt: PATCH form-encoded body, error path.
 * - useDeletePrompt: DELETE, error path.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mocks ────────────────────────────────────────────────────────────────────

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: { request: (...args: unknown[]) => mockRequest(...args) },
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

const fixedPrompt = {
  id: 1,
  user_id: 1,
  name: "greeting",
  content: "Hello!",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

// ─── usePrompts ───────────────────────────────────────────────────────────────

describe("usePrompts", () => {
  it("fetches /api/prompts and returns list[Prompt]", async () => {
    const { usePrompts } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([fixedPrompt]);

    const { result } = renderHook(() => usePrompts(), { wrapper });
    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });

    expect(mockRequest).toHaveBeenCalledWith("/api/prompts");
    expect(result.current.data).toHaveLength(1);
    expect(result.current.data?.[0]?.name).toBe("greeting");
  });

  it("isError is true on rejection", async () => {
    const { usePrompts } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("fail"), { status: 401 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => usePrompts(), { wrapper });
    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(401);
  });
});

// ─── useCreatePrompt ──────────────────────────────────────────────────────────

describe("useCreatePrompt", () => {
  it("posts form-encoded body to /api/prompts", async () => {
    const { useCreatePrompt } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue(fixedPrompt);

    const { result } = renderHook(() => useCreatePrompt(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ name: "greeting", content: "Hello!" });
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/prompts",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/x-www-form-urlencoded",
        }),
      })
    );

    const callArgs = mockRequest.mock.calls[0];
    const bodyStr = (callArgs?.[1] as { body?: string } | undefined)?.body ?? "";
    const params = new URLSearchParams(bodyStr);
    expect(params.get("name")).toBe("greeting");
    expect(params.get("content")).toBe("Hello!");
  });

  it("error path: isError on 409 conflict", async () => {
    const { useCreatePrompt } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("conflict"), { status: 409 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useCreatePrompt(), { wrapper });

    act(() => {
      result.current.mutate({ name: "dup", content: "x" });
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(409);
  });
});

// ─── useUpdatePrompt ──────────────────────────────────────────────────────────

describe("useUpdatePrompt", () => {
  it("omits name field when not provided", async () => {
    const { useUpdatePrompt } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue(fixedPrompt);

    const { result } = renderHook(() => useUpdatePrompt(1), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ content: "new content only" });
    });

    const callArgs = mockRequest.mock.calls[0];
    const bodyStr = (callArgs?.[1] as { body?: string } | undefined)?.body ?? "";
    const params = new URLSearchParams(bodyStr);
    expect(params.has("name")).toBe(false);
    expect(params.get("content")).toBe("new content only");
  });

  it("sends PATCH /api/prompts/{id} with form body", async () => {
    const { useUpdatePrompt } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue(fixedPrompt);

    const { result } = renderHook(() => useUpdatePrompt(1), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ name: "updated" });
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/prompts/1",
      expect.objectContaining({ method: "PATCH" })
    );
  });

  it("error path: isError on 404", async () => {
    const { useUpdatePrompt } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("not found"), { status: 404 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useUpdatePrompt(999), { wrapper });

    act(() => {
      result.current.mutate({ name: "x" });
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(404);
  });
});

// ─── useDeletePrompt ──────────────────────────────────────────────────────────

describe("useDeletePrompt", () => {
  it("sends DELETE /api/prompts/{id}", async () => {
    const { useDeletePrompt } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ status: "ok" });

    const { result } = renderHook(() => useDeletePrompt(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(1);
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/prompts/1",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("error path: isError on 404", async () => {
    const { useDeletePrompt } = await import("@/hooks/usePrompts");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("not found"), { status: 404 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useDeletePrompt(), { wrapper });

    act(() => {
      result.current.mutate(999);
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(404);
  });
});
