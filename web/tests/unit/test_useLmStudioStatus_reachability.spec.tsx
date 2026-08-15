/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for the reachability fix in useLmStudioStatus.
 *
 * Key scenario (the false-green bug):
 *   - useModels returns cached data with loaded models (stale 30-min catalog)
 *   - useLmStudioHealth returns reachable=false (LM Studio is actually DOWN)
 *   → useLmStudioStatus must return status:"error", NOT status:"ok"
 *
 * These tests mock both hooks at the module level so the
 * useLmStudioStatus hook's decision logic can be tested in isolation.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Module mocks — applied before any imports of the modules under test
// ---------------------------------------------------------------------------

// Mock useModels so we can control its output independently.
vi.mock("@/hooks/useModels", () => ({
  useModels: vi.fn(),
  modelKeys: { all: ["models"], list: () => ["models", "list"] },
}));

// Mock useLmStudioConfig so auth_failed can be controlled.
vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: vi.fn(),
  lmStudioConfigKeys: {
    all: ["lmstudio-config"],
    resolved: () => ["lmstudio-config", "resolved"],
  },
}));

// Mock useLmStudioHealth so reachability can be controlled.
vi.mock("@/hooks/useLmStudioHealth", () => ({
  useLmStudioHealth: vi.fn(),
  lmStudioHealthKeys: {
    all: ["lmstudio-health"],
    live: () => ["lmstudio-health", "live"],
  },
}));

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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

/** Build a minimal useModels return value with N loaded models. */
function makeModelsData(loadedCount: number) {
  const models = Array.from({ length: loadedCount }, (_, i) => ({
    id: `model-${String(i)}`,
    name: `Model ${String(i)}`,
    provider: "lmstudio",
    capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
    loaded: true,
    size_bytes: 0,
    params_string: "",
    quantization: null,
    loaded_instance_ids: [`instance-${String(i)}`],
    max_context_length: 8192,
    loaded_context_length: 8192,
  }));
  return {
    data: { models, total: loadedCount },
    isError: false,
    dataUpdatedAt: Date.now(),
    isFetching: false,
  };
}

/** Build a useLmStudioHealth return value. */
function makeHealthData(reachable: boolean, loadedCount = 0, authFailed = false) {
  return {
    data: {
      reachable,
      loaded_count: loadedCount,
      auth_failed: authFailed,
      last_probe_at: Date.now() / 1000,
    },
    isError: false,
    dataUpdatedAt: Date.now(),
    isFetching: false,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("useLmStudioStatus — reachability fix (false-green bug)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns 'error' when health.reachable=false even if useModels has cached loaded models", async () => {
    // This is the exact false-green scenario: catalog cache says models loaded,
    // but the live health probe says LM Studio is down.
    const { useModels } = await import("@/hooks/useModels");
    const { useLmStudioConfig } = await import("@/hooks/useLmStudioConfig");
    const { useLmStudioHealth } = await import("@/hooks/useLmStudioHealth");

    vi.mocked(useModels).mockReturnValue(makeModelsData(2) as ReturnType<typeof useModels>);
    vi.mocked(useLmStudioConfig).mockReturnValue({
      data: undefined,
      isError: false,
      dataUpdatedAt: 0,
      isFetching: false,
    } as ReturnType<typeof useLmStudioConfig>);
    vi.mocked(useLmStudioHealth).mockReturnValue(
      makeHealthData(false, 0, false) as ReturnType<typeof useLmStudioHealth>
    );

    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });

    // Must be error, NOT ok — even though useModels reports 2 models loaded.
    await waitFor(() => { expect(result.current.status).toBe("error"); });
    expect(result.current.tooltip.toLowerCase()).toContain("not reachable");
  });

  it("returns 'ok' when health.reachable=true and models are loaded", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { useLmStudioConfig } = await import("@/hooks/useLmStudioConfig");
    const { useLmStudioHealth } = await import("@/hooks/useLmStudioHealth");

    vi.mocked(useModels).mockReturnValue(makeModelsData(1) as ReturnType<typeof useModels>);
    vi.mocked(useLmStudioConfig).mockReturnValue({
      data: undefined,
      isError: false,
      dataUpdatedAt: 0,
      isFetching: false,
    } as ReturnType<typeof useLmStudioConfig>);
    vi.mocked(useLmStudioHealth).mockReturnValue(
      makeHealthData(true, 1, false) as ReturnType<typeof useLmStudioHealth>
    );

    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });

    await waitFor(() => { expect(result.current.status).toBe("ok"); });
    expect(result.current.tooltip.toLowerCase()).toContain("connected");
  });

  it("returns 'error' with auth tooltip when health.auth_failed=true", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { useLmStudioConfig } = await import("@/hooks/useLmStudioConfig");
    const { useLmStudioHealth } = await import("@/hooks/useLmStudioHealth");

    vi.mocked(useModels).mockReturnValue(makeModelsData(1) as ReturnType<typeof useModels>);
    vi.mocked(useLmStudioConfig).mockReturnValue({
      data: undefined,
      isError: false,
      dataUpdatedAt: 0,
      isFetching: false,
    } as ReturnType<typeof useLmStudioConfig>);
    // Reachable=true but auth_failed=true (401 from LM Studio)
    vi.mocked(useLmStudioHealth).mockReturnValue(
      makeHealthData(true, 0, true) as ReturnType<typeof useLmStudioHealth>
    );

    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });

    await waitFor(() => { expect(result.current.status).toBe("error"); });
    expect(result.current.tooltip.toLowerCase()).toContain("api key");
  });

  it("returns 'error' when reachable=true but no models are loaded", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { useLmStudioConfig } = await import("@/hooks/useLmStudioConfig");
    const { useLmStudioHealth } = await import("@/hooks/useLmStudioHealth");

    vi.mocked(useModels).mockReturnValue(makeModelsData(0) as ReturnType<typeof useModels>);
    vi.mocked(useLmStudioConfig).mockReturnValue({
      data: undefined,
      isError: false,
      dataUpdatedAt: 0,
      isFetching: false,
    } as ReturnType<typeof useLmStudioConfig>);
    vi.mocked(useLmStudioHealth).mockReturnValue(
      makeHealthData(true, 0, false) as ReturnType<typeof useLmStudioHealth>
    );

    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });

    await waitFor(() => { expect(result.current.status).toBe("error"); });
    expect(result.current.tooltip.toLowerCase()).toContain("no models loaded");
  });

  it("returns 'error' when the health query itself fails", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { useLmStudioConfig } = await import("@/hooks/useLmStudioConfig");
    const { useLmStudioHealth } = await import("@/hooks/useLmStudioHealth");

    vi.mocked(useModels).mockReturnValue(makeModelsData(1) as ReturnType<typeof useModels>);
    vi.mocked(useLmStudioConfig).mockReturnValue({
      data: undefined,
      isError: false,
      dataUpdatedAt: 0,
      isFetching: false,
    } as ReturnType<typeof useLmStudioConfig>);
    // Health query errored (our own API is down)
    vi.mocked(useLmStudioHealth).mockReturnValue({
      data: undefined,
      isError: true,
      dataUpdatedAt: 0,
      isFetching: false,
    } as ReturnType<typeof useLmStudioHealth>);

    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });

    await waitFor(() => { expect(result.current.status).toBe("error"); });
    expect(result.current.tooltip.toLowerCase()).toContain("probe");
  });

  // Finding 2: mount-race false-RED — catalog `data` is undefined at mount
  // but health.loaded_count>0 (backend probe already returned a count).
  // The badge must NOT be red just because the catalog hasn't resolved yet.
  it("is NOT red when health.loaded_count>0 but catalog data is undefined (mount-race)", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { useLmStudioConfig } = await import("@/hooks/useLmStudioConfig");
    const { useLmStudioHealth } = await import("@/hooks/useLmStudioHealth");

    // Catalog not yet resolved — data is undefined, dataUpdatedAt=0.
    vi.mocked(useModels).mockReturnValue({
      data: undefined,
      isError: false,
      dataUpdatedAt: 0,
      isFetching: true,
    } as ReturnType<typeof useModels>);
    vi.mocked(useLmStudioConfig).mockReturnValue({
      data: undefined,
      isError: false,
      dataUpdatedAt: 0,
      isFetching: false,
    } as ReturnType<typeof useLmStudioConfig>);
    // Health probe already returned: reachable=true, 1 model loaded.
    vi.mocked(useLmStudioHealth).mockReturnValue(
      makeHealthData(true, 1, false) as ReturnType<typeof useLmStudioHealth>
    );

    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });

    // Must NOT be "error" — health.loaded_count is the authoritative signal.
    // The tooltip may say "Re-probing" (catalog still fetching) or "connected"
    // — either is fine; the key invariant is the badge is NOT red.
    await waitFor(() => { expect(result.current.status).toBe("ok"); });
    expect(result.current.status).not.toBe("error");
  });

  // Finding 2: reverse-staleness false-RED — catalog lags after a model loads.
  // health.loaded_count>0 but catalog data?.models is empty (stale 25s window).
  it("is NOT red when health.loaded_count>0 but catalog shows 0 loaded (staleness lag)", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { useLmStudioConfig } = await import("@/hooks/useLmStudioConfig");
    const { useLmStudioHealth } = await import("@/hooks/useLmStudioHealth");

    // Catalog has resolved but still shows 0 loaded (stale).
    vi.mocked(useModels).mockReturnValue(makeModelsData(0) as ReturnType<typeof useModels>);
    vi.mocked(useLmStudioConfig).mockReturnValue({
      data: undefined,
      isError: false,
      dataUpdatedAt: 0,
      isFetching: false,
    } as ReturnType<typeof useLmStudioConfig>);
    // Health probe already shows 1 model loaded (authoritative live signal).
    vi.mocked(useLmStudioHealth).mockReturnValue(
      makeHealthData(true, 1, false) as ReturnType<typeof useLmStudioHealth>
    );

    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });

    // Badge must NOT be red — health.loaded_count wins over stale catalog.
    await waitFor(() => { expect(result.current.status).toBe("ok"); });
    expect(result.current.tooltip.toLowerCase()).toContain("connected");
  });
});
