/* SPDX-License-Identifier: Apache-2.0 */
/**
 * /compare slash command + picker — Composer unit tests.
 *
 * Covers:
 *  1. /compare appears in BUILTIN_COMMANDS (SlashMenu registry).
 *  2. With ≥2 models: selecting /compare opens the picker.
 *  3. Confirming with two distinct models calls onABCompareStart.
 *  4. With <2 models: /compare shows a toast, no picker.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { BUILTIN_COMMANDS } from "@/components/SlashMenu";
import { Composer } from "@/components/Composer";

// ─── Mock harness ─────────────────────────────────────────────────────────────

let mockPush: ReturnType<typeof vi.fn>;

vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
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

vi.mock("@/components/MicButton", () => ({ MicButton: () => null }));
vi.mock("@/components/InProjectChip", () => ({ InProjectChip: () => null }));
vi.mock("@/components/RagModeBadge", () => ({ RagModeBadge: () => null }));

vi.mock("@/components/SlashMenu", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/components/SlashMenu")>();
  return {
    ...real,
    // Keep BUILTIN_COMMANDS and helpers real; only stub the menu widget
    // so keyboard dispatch is exercised through dispatchSlashCommand.
    SlashMenu: () => null,
  };
});

vi.mock("@/hooks/usePrompts", () => ({
  usePrompts: () => ({ data: [], isLoading: false, isError: false }),
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

vi.mock("@/hooks/useIntegrationsList", () => ({
  useIntegrationsList: () => ({ data: [], isLoading: false, isError: false }),
  useUpdateIntegrationsList: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useViewport", () => ({
  useViewport: () => ({ isMobile: false }),
}));

vi.mock("@/hooks/usePlatform", () => ({
  usePlatform: () => ({ modLabel: "Ctrl", isMac: false, isWindows: false, isLinux: true }),
}));

// Controllable mock for model list — mutated per-test.
let mockModelsData:
  | {
      models: Array<{
        id: string;
        name: string;
        loaded: boolean;
        loaded_instance_ids: string[];
        max_context_length: number;
        loaded_context_length?: number;
        size_bytes: number;
        params_string: string;
        capabilities?: Record<string, unknown>;
      }>;
    }
  | undefined = undefined;

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: mockModelsData, isLoading: false, isError: false, refetch: vi.fn() }),
}));

// ─── Helpers ──────────────────────────────────────────────────────────────────

function makeModel(id: string, name: string) {
  return {
    id,
    name,
    loaded: true,
    loaded_instance_ids: [id],
    max_context_length: 4096,
    size_bytes: 0,
    params_string: "",
  };
}

const baseProps = {
  chatId: 1,
  streaming: false,
  onSubmit: vi.fn(),
  onStop: vi.fn(),
  onClear: vi.fn(),
  onFork: vi.fn(),
  onCompact: vi.fn(),
  onMemoryPin: vi.fn(),
  modelId: "model-a",
};

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("/compare slash command — Composer (Part 2)", () => {
  beforeEach(() => {
    mockModelsData = undefined;
    mockPush = vi.fn();
    vi.clearAllMocks();
    if (typeof localStorage !== "undefined") localStorage.clear();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("BUILTIN_COMMANDS contains a 'compare' entry", () => {
    const cmd = BUILTIN_COMMANDS.find((c) => c.name === "compare");
    expect(cmd).toBeDefined();
    expect(cmd?.comingSoon).not.toBe(true);
  });

  it("picker opens when ≥2 models are available and /compare is dispatched", async () => {
    mockModelsData = {
      models: [makeModel("model-a", "Model A"), makeModel("model-b", "Model B")],
    };
    render(<Composer {...baseProps} />);

    const textarea = screen.getByRole("textbox", { name: /message/i });

    // Type /compare and press Enter to dispatch.
    await act(async () => {
      fireEvent.change(textarea, { target: { value: "/compare" } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    });

    expect(screen.getByTestId("compare-picker")).toBeTruthy();
  });

  it("confirming with two distinct models calls onABCompareStart", async () => {
    mockModelsData = {
      models: [makeModel("model-a", "Model A"), makeModel("model-b", "Model B")],
    };
    const onABCompareStart = vi.fn();
    render(<Composer {...baseProps} onABCompareStart={onABCompareStart} />);

    const textarea = screen.getByRole("textbox", { name: /message/i });

    await act(async () => {
      fireEvent.change(textarea, { target: { value: "/compare" } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    });

    // Picker should be open — select Model B in the second select.
    const selects = screen.getAllByRole("combobox");
    // Model A select defaults to baseProps.modelId; change Model B.
    const modelBSelect = selects[1];
    await act(async () => {
      fireEvent.change(modelBSelect!, { target: { value: "model-b" } });
    });

    // Confirm.
    const confirmBtn = screen.getByTestId("compare-picker-confirm");
    await act(async () => {
      fireEvent.click(confirmBtn);
    });

    expect(onABCompareStart).toHaveBeenCalledWith("model-a", "model-b");
    // Picker should close after confirm.
    expect(screen.queryByTestId("compare-picker")).toBeNull();
  });

  it("shows a toast and no picker when fewer than 2 models are available", async () => {
    mockModelsData = {
      models: [makeModel("model-a", "Model A")],
    };
    render(<Composer {...baseProps} />);

    const textarea = screen.getByRole("textbox", { name: /message/i });

    await act(async () => {
      fireEvent.change(textarea, { target: { value: "/compare" } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    });

    // Toast should fire.
    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "warning" }),
    );
    // Picker must NOT open.
    expect(screen.queryByTestId("compare-picker")).toBeNull();
  });
});
