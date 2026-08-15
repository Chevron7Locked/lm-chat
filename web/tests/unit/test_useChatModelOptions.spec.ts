/**
 * Unit tests for useChatModelOptions hook.
 *
 * Cluster 3a (audit 2026-06-10):
 *  - test_useChatModelOptions_expands_multi_instance_models: when a model has
 *    loaded_instance_ids.length > 1, the hook emits one option per instance id
 *    (not a single option for the stable model key).
 *
 * Operator directive (2026-07-30): an UNLOADED local (lmstudio) model must
 * never appear in — and never be the default of — a chat-model dropdown, since
 * it can't actually be picked. Cloud-provider models have no load/unload
 * concept, so they always appear regardless of `loaded`. See the dedicated
 * "operator directive" describe block below.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mock api ──────────────────────────────────────────────────────────────

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: { request: (...args: unknown[]) => mockRequest(...args), postForm: vi.fn() },
  ApiClient: vi.fn(),
}));

// ─── Mock authStore ──────────────────────────────────────────────────────────

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

describe("useChatModelOptions", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("filters out embedding models and returns only chat models", async () => {
    const { useChatModelOptions } = await import("@/hooks/useChatModelOptions");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([
      {
        key: "qwen3-8b",
        display_name: "Qwen3 8B",
        capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
        loaded_instances: 1,
        loaded_instance_ids: ["qwen3-8b"],
      },
      {
        key: "text-embedding-nomic-embed-text-v1.5",
        display_name: "Nomic Embed",
        capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: true },
        loaded_instances: 1,
        loaded_instance_ids: [],
      },
    ]);
    const { result } = renderHook(() => useChatModelOptions(), { wrapper });
    await waitFor(() => { expect(result.current.isLoading).toBe(false); });
    // Only the chat model — embedding is excluded.
    expect(result.current.options.length).toBe(1);
    expect(result.current.options[0]?.id).toBe("qwen3-8b");
  });

  // Cluster 3a Task 3 spec test (audit 2026-06-10).
  it("test_useChatModelOptions_expands_multi_instance_models: one option per instance when loaded_instance_ids.length > 1", async () => {
    const { useChatModelOptions } = await import("@/hooks/useChatModelOptions");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([
      {
        key: "qwen3-vl-8b-instruct",
        display_name: "Qwen3 VL 8B",
        capabilities: { vision: true, trained_for_tool_use: true, reasoning: null, embedding: false },
        loaded_instances: 2,
        loaded_instance_ids: ["model-a", "model-b"],
        max_context_length: 16384,
      },
      {
        key: "llama3-8b",
        display_name: "Llama 3 8B",
        capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
        loaded_instances: 1,
        loaded_instance_ids: ["llama3-8b"],
      },
      {
        // Unloaded, provider omitted → defaults to "lmstudio" (see
        // useModels.ts's normalizer). Under the 2026-07-30 operator
        // directive an unloaded LOCAL model is hidden entirely — this
        // fixture used to assert it appeared with an "(unloaded)" suffix;
        // it now asserts the opposite (see below).
        key: "mistral-7b",
        display_name: "Mistral 7B",
        capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
        loaded_instances: 0,
        loaded_instance_ids: [],
      },
    ]);
    const { result } = renderHook(() => useChatModelOptions(), { wrapper });
    await waitFor(() => { expect(result.current.isLoading).toBe(false); });

    const options = result.current.options;
    // Multi-instance model should produce 2 options (one per instance id).
    const multiOpts = options.filter((o) => o.id === "model-a" || o.id === "model-b");
    expect(multiOpts).toHaveLength(2);
    // Labels should identify the instance.
    expect(multiOpts[0]?.label).toMatch(/model-a/);
    expect(multiOpts[1]?.label).toMatch(/model-b/);
    // Both should carry the loaded flag.
    expect(multiOpts[0]?.loaded).toBe(true);
    expect(multiOpts[1]?.loaded).toBe(true);
    // Single-instance loaded model: emits its stable key.
    const singleOpt = options.find((o) => o.id === "llama3-8b");
    expect(singleOpt).toBeDefined();
    expect(singleOpt?.loaded).toBe(true);
    // Unloaded LOCAL model (mistral-7b, provider defaults to "lmstudio"):
    // operator directive (2026-07-30) — hidden entirely, not merely
    // suffixed. See the dedicated describe block below for the full
    // local-vs-cloud predicate coverage.
    const unloadedOpt = options.find((o) => o.id === "mistral-7b");
    expect(unloadedOpt).toBeUndefined();
  });

  // Bug B (2026-07-18 dogfood): "Encountered two children with the same
  // key, qwen3.6-35b-a3b-mtp" fired on model dropdowns. /api/models itself
  // has no duplicate top-level ids, but a model's OWN loaded_instance_ids
  // array can repeat an instance id (upstream LM Studio quirk) — the
  // multi-instance expansion loop then emitted the SAME id twice.
  it("test_useChatModelOptions_dedupes_repeated_instance_ids: a duplicated loaded_instance_ids entry collapses to one option", async () => {
    const { useChatModelOptions } = await import("@/hooks/useChatModelOptions");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([
      {
        key: "qwen3.6-35b-a3b",
        display_name: "Qwen3.6 35B A3B",
        capabilities: { vision: false, trained_for_tool_use: true, reasoning: null, embedding: false },
        loaded_instances: 2,
        // Upstream reported the SAME instance id twice.
        loaded_instance_ids: ["qwen3.6-35b-a3b-mtp", "qwen3.6-35b-a3b-mtp"],
      },
    ]);
    const { result } = renderHook(() => useChatModelOptions(), { wrapper });
    await waitFor(() => { expect(result.current.isLoading).toBe(false); });

    const matches = result.current.options.filter(
      (o) => o.id === "qwen3.6-35b-a3b-mtp",
    );
    // Exactly one option — not two — for the repeated instance id.
    expect(matches).toHaveLength(1);
    // And the option list overall carries no duplicate ids anywhere.
    const ids = result.current.options.map((o) => o.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("forwards capabilities from ModelInfo onto each emitted option", async () => {
    const { useChatModelOptions } = await import("@/hooks/useChatModelOptions");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([
      {
        key: "vision-model",
        display_name: "Vision Model",
        capabilities: {
          vision: true,
          trained_for_tool_use: true,
          reasoning: { default: "medium", allowed_options: ["off", "low", "medium", "high"] },
          embedding: false,
        },
        loaded_instances: 1,
        loaded_instance_ids: ["vision-model"],
      },
    ]);
    const { result } = renderHook(() => useChatModelOptions(), { wrapper });
    await waitFor(() => { expect(result.current.isLoading).toBe(false); });
    const opt = result.current.options[0];
    expect(opt?.capabilities.vision).toBe(true);
    expect(opt?.capabilities.trained_for_tool_use).toBe(true);
    expect(opt?.capabilities.reasoning).not.toBeNull();
  });

  // Operator directive (2026-07-30): model-picker dropdowns must NEVER show
  // — and must never default to — an UNLOADED local (lmstudio) model. Only
  // currently-loaded local models may appear. Cloud-provider models
  // (provider !== "lmstudio") are unaffected: "loaded" is not a meaningful
  // concept for them, so they always show regardless of the flag.
  describe("operator directive: unloaded LOCAL models are hidden (2026-07-30)", () => {
    it("excludes an unloaded lmstudio model from both options and groups", async () => {
      const { useChatModelOptions } = await import("@/hooks/useChatModelOptions");
      const { wrapper } = makeWrapper();
      mockRequest.mockResolvedValue([
        {
          key: "loaded-local-7b",
          display_name: "Loaded Local 7B",
          capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
          loaded_instances: 1,
          loaded_instance_ids: ["loaded-local-7b"],
        },
        {
          // provider omitted → defaults to "lmstudio" (useModels.ts
          // normalizer). loaded_instances: 0 → loaded === false.
          key: "unloaded-local-7b",
          display_name: "Unloaded Local 7B",
          capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
          loaded_instances: 0,
          loaded_instance_ids: [],
        },
      ]);
      const { result } = renderHook(() => useChatModelOptions(), { wrapper });
      await waitFor(() => { expect(result.current.isLoading).toBe(false); });

      // Absent from the flat options list.
      expect(result.current.options.find((o) => o.id === "unloaded-local-7b")).toBeUndefined();
      // Present: the loaded local model.
      const loadedOpt = result.current.options.find((o) => o.id === "loaded-local-7b");
      expect(loadedOpt).toBeDefined();
      expect(loadedOpt?.loaded).toBe(true);

      // Also absent from every provider group (not just the flat list).
      for (const group of result.current.groups) {
        expect(group.options.find((o) => o.id === "unloaded-local-7b")).toBeUndefined();
      }
      const lmstudioGroup = result.current.groups.find((g) => g.provider === "lmstudio");
      expect(lmstudioGroup).toBeDefined();
      expect(lmstudioGroup?.options.map((o) => o.id)).toEqual(["loaded-local-7b"]);
    });

    it("keeps a cloud-provider model present even when loaded === false", async () => {
      const { useChatModelOptions } = await import("@/hooks/useChatModelOptions");
      const { wrapper } = makeWrapper();
      mockRequest.mockResolvedValue([
        {
          key: "meta-llama/llama-3.3-70b-instruct",
          display_name: "Llama 3.3 70B",
          provider: "openrouter",
          capabilities: { vision: false, trained_for_tool_use: true, reasoning: null, embedding: false },
          loaded_instances: 0,
          loaded_instance_ids: [],
        },
      ]);
      const { result } = renderHook(() => useChatModelOptions(), { wrapper });
      await waitFor(() => { expect(result.current.isLoading).toBe(false); });

      const cloudOpt = result.current.options.find(
        (o) => o.id === "meta-llama/llama-3.3-70b-instruct",
      );
      expect(cloudOpt).toBeDefined();
      expect(cloudOpt?.loaded).toBe(false);
      expect(cloudOpt?.provider).toBe("openrouter");
      expect(cloudOpt?.label).toMatch(/\(unloaded\)/);

      const orGroup = result.current.groups.find((g) => g.provider === "openrouter");
      expect(orGroup?.options.map((o) => o.id)).toEqual([
        "meta-llama/llama-3.3-70b-instruct",
      ]);
    });

    // Red-on-revert check (verified manually, see PR notes): reverting the
    // `if (m.provider === "lmstudio") continue;` guard in
    // useChatModelOptions.ts back to unconditionally pushing unloaded
    // options makes this test fail, since "unloaded-local-7b" would then
    // reappear in both `options` and the "lmstudio" group.
    it("mixed catalog: unloaded local hidden, unloaded cloud shown, loaded local/cloud both shown", async () => {
      const { useChatModelOptions } = await import("@/hooks/useChatModelOptions");
      const { wrapper } = makeWrapper();
      mockRequest.mockResolvedValue([
        {
          key: "loaded-local",
          display_name: "Loaded Local",
          capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
          loaded_instances: 1,
          loaded_instance_ids: ["loaded-local"],
        },
        {
          key: "unloaded-local",
          display_name: "Unloaded Local",
          capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
          loaded_instances: 0,
          loaded_instance_ids: [],
        },
        {
          key: "cloud-loaded",
          display_name: "Cloud Loaded",
          provider: "groq",
          capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
          loaded_instances: 1,
          loaded_instance_ids: ["cloud-loaded"],
        },
        {
          key: "cloud-unloaded",
          display_name: "Cloud Unloaded",
          provider: "groq",
          capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
          loaded_instances: 0,
          loaded_instance_ids: [],
        },
      ]);
      const { result } = renderHook(() => useChatModelOptions(), { wrapper });
      await waitFor(() => { expect(result.current.isLoading).toBe(false); });

      const ids = result.current.options.map((o) => o.id).sort();
      expect(ids).toEqual(["cloud-loaded", "cloud-unloaded", "loaded-local"].sort());
      expect(ids).not.toContain("unloaded-local");
    });
  });
});
