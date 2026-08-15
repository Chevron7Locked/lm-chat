/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for useModelList.ts — the derived /api/models UI view.
 *
 * fe-13: `refresh` was renamed to `revalidate`. GET /api/models reads the
 * backend's in-memory model cache — invalidating the FE TanStack query
 * alone re-fetches the SAME stale payload; it does NOT force LM Studio to
 * re-probe. The old `refresh` name implied a live re-probe it never did.
 * These tests cover the rename and the (non-)behavior it documents.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ModelListResponse } from "@/hooks/useModels";

// ─── Mock useModels — isolate useModelList from the real /api/models fetch ──

interface MockUseModelsResult {
  data: ModelListResponse | undefined;
  isError: boolean;
  isFetching: boolean;
  error: unknown;
  dataUpdatedAt: number;
  errorUpdatedAt: number;
}

const mockUseModels = vi.fn<(...args: unknown[]) => MockUseModelsResult>();

vi.mock("@/hooks/useModels", () => ({
  useModels: (...args: unknown[]) => mockUseModels(...args),
  modelKeys: { all: ["models"], list: () => ["models", "list"] },
}));

// ─── Mock lmStudioStore — simple selector-hook, fixed state per test ────────

vi.mock("@/stores/lmStudioStore", () => ({
  useLmStudioStore: (selector: (s: unknown) => unknown) => {
    const state = {
      status: "connected",
      lastError: null,
      beginProbe: vi.fn(),
      resolveProbe: vi.fn(),
    };
    return selector(state);
  },
}));

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

describe("useModelList", () => {
  it("exposes revalidate (not refresh) on the returned object", async () => {
    mockUseModels.mockReturnValue({
      data: { models: [], total: 0 },
      isError: false,
      isFetching: false,
      error: null,
      dataUpdatedAt: 1,
      errorUpdatedAt: 0,
    });
    const { useModelList } = await import("@/hooks/useModelList");
    const { wrapper } = makeWrapper();

    const { result } = renderHook(() => useModelList(), { wrapper });

    expect(typeof result.current.revalidate).toBe("function");
    expect(
      (result.current as unknown as { refresh?: unknown }).refresh,
    ).toBeUndefined();
  });

  it("fe-13: revalidate() invalidates modelKeys.list() — re-reads the FE cache, does NOT trigger a BE re-probe", async () => {
    mockUseModels.mockReturnValue({
      data: { models: [], total: 0 },
      isError: false,
      isFetching: false,
      error: null,
      dataUpdatedAt: 1,
      errorUpdatedAt: 0,
    });
    const { useModelList } = await import("@/hooks/useModelList");
    const { modelKeys } = await import("@/hooks/useModels");
    const { wrapper, qc } = makeWrapper();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(() => useModelList(), { wrapper });

    await act(async () => {
      await result.current.revalidate();
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: modelKeys.list() });
    // revalidate never touches the admin-only BE re-probe endpoint — that
    // is useRefreshModels's job (POST /api/admin/models/refresh).
    expect(mockUseModels).toHaveBeenCalled();
  });
});
