/**
 * Unit tests for useModels hook.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mock api ──────────────────────────────────────────────────────────────

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

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children),
  };
}

describe("useModels", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("fetches /api/models on mount", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { wrapper } = makeWrapper();
    // Backend returns ModelInfo[] — plain array, no envelope.
    mockRequest.mockResolvedValue([]);

    const { result } = renderHook(() => useModels(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockRequest).toHaveBeenCalledWith("/api/models");
  });

  it("normalises plain array to { models, total }", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { wrapper } = makeWrapper();
    // Wire shape: backend field names (key, display_name, loaded_instances).
    // The hook normalises to the UI shape (id, name, loaded).
    const wireModel = {
      key: "qwen3",
      display_name: "Qwen 3.6B",
      capabilities: {},
      loaded_instances: 1,
    };
    mockRequest.mockResolvedValue([wireModel]);

    const { result } = renderHook(() => useModels(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.models[0]?.id).toBe("qwen3");
    expect(result.current.data?.models[0]?.name).toBe("Qwen 3.6B");
    expect(result.current.data?.models[0]?.loaded).toBe(true);
    expect(result.current.data?.total).toBe(1);
  });

  it("staleTime is 2 minutes", async () => {
    // Verify by checking that a second invocation (within 2min) doesn't refetch.
    const { useModels } = await import("@/hooks/useModels");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([]);

    const { result: r1 } = renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(r1.current.isSuccess).toBe(true));

    // Second hook instance — should hit cache, not re-fetch.
    const { result: r2 } = renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(r2.current.isSuccess).toBe(true));

    // mockRequest called once (cache hit on second).
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("isError on rejection", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { wrapper } = makeWrapper();
    mockRequest.mockRejectedValue(new Error("models unavailable"));

    const { result } = renderHook(() => useModels(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });

  it("modelKeys.list() is stable", async () => {
    const { modelKeys } = await import("@/hooks/useModels");
    expect(modelKeys.list()).toEqual(modelKeys.list());
    expect(modelKeys.all).toEqual(["models"]);
  });

  // Cluster 0 (audit 2026-06-10): max_context_length wire-through.
  // FastAPI's default response_model_by_alias=True puts aliased fields on
  // the wire as camelCase. The normalizer must accept either shape.
  it("carries max_context_length from camelCase wire", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([{
      key: "qwen3-vl-8b-instruct",
      displayName: "Qwen3 VL 8B Instruct",
      capabilities: { vision: true, trained_for_tool_use: true },
      loaded_instances: 2,
      loaded_instance_ids: ["model-a", "model-b"],
      maxContextLength: 16384,
      sizeBytes: 7454273952,
      paramsString: "8B",
    }]);
    const { result } = renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const m = result.current.data?.models[0];
    expect(m?.max_context_length).toBe(16384);
    expect(m?.size_bytes).toBe(7454273952);
    expect(m?.params_string).toBe("8B");
  });

  it("carries max_context_length from snake_case wire (fallback path)", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([{
      key: "qwen3.5-122b",
      display_name: "Qwen3.5 122B",
      capabilities: {},
      loaded_instances: 1,
      loaded_instance_ids: ["qwen3.5-122b-i1"],
      max_context_length: 131072,
      size_bytes: 87773581344,
      params_string: "122B-A10B",
    }]);
    const { result } = renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.models[0]?.max_context_length).toBe(131072);
  });

  it("defaults max_context_length to 0 when wire omits the field", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([{
      key: "text-embedding-nomic-embed-text-v1.5",
      displayName: "Nomic Embed Text v1.5",
      capabilities: {},
      loaded_instances: 0,
      loaded_instance_ids: [],
    }]);
    const { result } = renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.models[0]?.max_context_length).toBe(0);
  });

  // Cluster 3a Task 1 (audit 2026-06-10): ModelInfo.capabilities is now typed
  // as ModelCapabilities (not Record<string,boolean>). The normalizer must
  // produce the correct shape including reasoning as object|null.
  it("test_useModels_ModelInfo_carries_capabilities_object_shape: normalizer produces correct ModelCapabilities shape", async () => {
    const { useModels } = await import("@/hooks/useModels");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([
      {
        key: "qwen3-vl-8b-instruct",
        displayName: "Qwen3 VL 8B",
        capabilities: {
          vision: true,
          trained_for_tool_use: true,
          reasoning: {
            default: "medium",
            allowed_options: ["off", "low", "medium", "high"],
          },
          embedding: false,
        },
        loaded_instances: 1,
        loaded_instance_ids: ["model-a"],
        maxContextLength: 32768,
      },
      {
        key: "nomic-embed",
        displayName: "Nomic Embed",
        capabilities: null, // legacy wire: no capabilities
        loaded_instances: 0,
        loaded_instance_ids: [],
      },
    ]);
    const { result } = renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const vision = result.current.data?.models[0];
    expect(vision?.capabilities.vision).toBe(true);
    expect(vision?.capabilities.trained_for_tool_use).toBe(true);
    expect(vision?.capabilities.reasoning).not.toBeNull();
    expect(vision?.capabilities.reasoning?.default).toBe("medium");
    expect(vision?.capabilities.embedding).toBe(false);

    const embed = result.current.data?.models[1];
    // null capabilities wire → safe defaults
    expect(embed?.capabilities.vision).toBe(false);
    expect(embed?.capabilities.trained_for_tool_use).toBe(false);
    expect(embed?.capabilities.reasoning).toBeNull();
    expect(embed?.capabilities.embedding).toBe(false);
  });
});
