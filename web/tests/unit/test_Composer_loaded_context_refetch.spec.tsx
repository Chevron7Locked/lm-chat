/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Bug 2 fix regression test — Composer refetches models when the selected
 * model is loaded but `loaded_context_length` is still 0 (stale cache).
 *
 * Without the fix, the context meter falls back to `max_context_length`
 * (e.g. 1M arch-max) on a new chat until the 25s refetchInterval fires.
 * With the fix, a targeted refetch fires immediately so the displayed
 * context reflects the actual loaded window (e.g. 96k).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { Composer } from "@/components/Composer";

// ─── Mocks ────────────────────────────────────────────────────────────────────

vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

vi.mock("@/hooks/useSTT", () => ({
  useSTT: () => ({
    capability: { available: false, engine: null },
    state: { listening: false, error: null },
    start: vi.fn(),
    stop: vi.fn(),
  }),
  detectSTT: () => ({ available: false, engine: null }),
}));

vi.mock("@/components/MicButton", () => ({
  MicButton: () => null,
}));

vi.mock("@/components/InProjectChip", () => ({
  InProjectChip: () => null,
}));

vi.mock("@/components/RagModeBadge", () => ({
  RagModeBadge: () => null,
}));

vi.mock("@/components/SlashMenu", () => ({
  SlashMenu: () => null,
  parseSlashCommand: () => null,
  BUILTIN_COMMANDS: [],
  filterCommands: () => [],
}));

vi.mock("@/hooks/usePrompts", () => ({
  usePrompts: () => ({ data: [], isLoading: false, isError: false }),
}));

vi.mock("@/hooks/useIntegrationsList", () => ({
  useIntegrationsList: () => ({ data: [], isLoading: false, isError: false }),
  useUpdateIntegrationsList: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useChatPreset", () => ({
  useChatPreset: () => ({
    activePreset: "",
    preset: null,
    setPreset: vi.fn(),
    clearPreset: vi.fn(),
  }),
  useHydrateChatPresets: () => undefined,
  useChatPresetStore: vi.fn(),
}));

vi.mock("@/components/ModelSelectControl", () => ({
  ModelSelectControl: () => null,
}));

// The mutable refetch spy — each test can assert it was / wasn't called.
const mockRefetch = vi.hoisted(() => vi.fn().mockResolvedValue({ data: undefined }));

// Mutable models state so tests can control loaded_context_length.
const mockModelsState = vi.hoisted(() => ({
  models: [] as Array<{
    id: string;
    name: string;
    loaded: boolean;
    loaded_instance_ids: string[];
    loaded_context_length: number;
    max_context_length: number;
    capabilities: {
      vision: boolean;
      trained_for_tool_use: boolean;
      reasoning: null;
      embedding: boolean;
    };
    size_bytes: number;
    params_string: string;
  }>,
}));

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({
    data: { models: mockModelsState.models },
    isLoading: false,
    isError: false,
    refetch: mockRefetch,
  }),
}));

// ─── Helpers ──────────────────────────────────────────────────────────────────

const baseProps = {
  chatId: 1,
  streaming: false,
  onSubmit: vi.fn(),
  onStop: vi.fn(),
  onClear: vi.fn(),
  onFork: vi.fn(),
  onCompact: vi.fn(),
  onMemoryPin: vi.fn(),
};

function makeModel(opts: {
  id: string;
  loaded: boolean;
  loaded_context_length: number;
  max_context_length: number;
}) {
  return {
    id: opts.id,
    name: opts.id,
    loaded: opts.loaded,
    loaded_instance_ids: opts.loaded ? [opts.id] : [],
    loaded_context_length: opts.loaded_context_length,
    max_context_length: opts.max_context_length,
    capabilities: {
      vision: false,
      trained_for_tool_use: false,
      reasoning: null,
      embedding: false,
    },
    size_bytes: 0,
    params_string: "",
  };
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("Composer — Bug 2: refetch when loaded_context_length is 0", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefetch.mockResolvedValue({ data: undefined });
    mockModelsState.models = [];
  });

  it("calls refetch when selected model is loaded but loaded_context_length=0 (stale cache)", async () => {
    // Simulates the state right after LM Studio loads a model but the FE
    // cache hasn't refreshed yet — loaded=true but loaded_context_length=0.
    mockModelsState.models = [
      makeModel({
        id: "qwen3-8b",
        loaded: true,
        loaded_context_length: 0,
        max_context_length: 131072,
      }),
    ];

    render(<Composer {...baseProps} modelId="qwen3-8b" />);

    await waitFor(() => {
      expect(mockRefetch).toHaveBeenCalledTimes(1);
    });
  });

  it("does NOT call refetch when loaded_context_length is already populated", async () => {
    // Normal case: model is loaded and context length is known — no refetch needed.
    mockModelsState.models = [
      makeModel({
        id: "qwen3-8b",
        loaded: true,
        loaded_context_length: 98304,
        max_context_length: 131072,
      }),
    ];

    render(<Composer {...baseProps} modelId="qwen3-8b" />);

    // Give effects a chance to fire.
    await new Promise((r) => setTimeout(r, 50));
    expect(mockRefetch).not.toHaveBeenCalled();
  });

  it("does NOT call refetch when no model is selected", async () => {
    mockModelsState.models = [
      makeModel({
        id: "qwen3-8b",
        loaded: false,
        loaded_context_length: 0,
        max_context_length: 131072,
      }),
    ];

    // No modelId prop — selectedModel will be undefined.
    render(<Composer {...baseProps} />);

    await new Promise((r) => setTimeout(r, 50));
    expect(mockRefetch).not.toHaveBeenCalled();
  });
});
