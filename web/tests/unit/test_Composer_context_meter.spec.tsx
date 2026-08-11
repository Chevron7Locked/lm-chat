/**
 * Cluster 3a Finding 2 fix (revision 2026-06-10): Context meter includes
 * historyTokens (conversation history) not just the current draft.
 *
 * Tests:
 *  - meter shows when max_context_length > 0 and history tokens push usage
 *    over the threshold for the warning to fire
 *  - meter warns at ≥80% when history+draft crosses the threshold
 *  - meter correctly adds historyTokens to the draft estimate
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { Composer } from "@/components/Composer";

// ─── Mocks (mirrors test_Composer_attach_button.spec.tsx pattern) ─────────────

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

// Model with a known max_context_length of 16384.
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({
    data: {
      models: [
        {
          id: "test-model",
          name: "Test Model",
          loaded: true,
          loaded_instance_ids: ["test-model"],
          capabilities: {
            vision: false,
            trained_for_tool_use: false,
            reasoning: null,
            embedding: false,
          },
          max_context_length: 16384,
          loaded_context_length: 16384,
          size_bytes: 0,
          params_string: "",
        },
      ],
    },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("Composer context meter — historyTokens (Finding 2 fix)", () => {
  const baseProps = {
    chatId: 1,
    streaming: false,
    onSubmit: vi.fn(),
    onStop: vi.fn(),
    onClear: vi.fn(),
    onFork: vi.fn(),
    onCompact: vi.fn(),
    onMemoryPin: vi.fn(),
    modelId: "test-model",
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows context meter when model has max_context_length", () => {
    render(<Composer {...baseProps} historyTokens={0} />);
    expect(screen.getByTestId("context-meter")).toBeTruthy();
  });

  it("does not warn when history+draft is below 80%", () => {
    // historyTokens = 1000, draft = 0 chars → 0 draft tokens
    // total = 1000 / 16384 ≈ 6.1% — no warning
    render(<Composer {...baseProps} historyTokens={1000} />);
    const meter = screen.getByTestId("context-meter");
    expect(meter.getAttribute("data-warn")).toBe("false");
  });

  it("warns at ≥80% when historyTokens alone crosses the threshold", () => {
    // 80% of 16384 = 13107. Pass historyTokens = 13200 (>80%) with empty draft.
    // Without historyTokens the meter would show ~0/16384 — no warning.
    // With historyTokens the total is 13200 → warning should fire.
    render(<Composer {...baseProps} historyTokens={13200} />);
    const meter = screen.getByTestId("context-meter");
    expect(meter.getAttribute("data-warn")).toBe("true");
  });

  it("adds historyTokens to draft estimate in displayed total", () => {
    // historyTokens = 5000 with no draft text → displayed should include ~5000.
    // The meter text is "~N / M" where N = historyTokens + draftTokens.
    render(<Composer {...baseProps} historyTokens={5000} />);
    const meter = screen.getByTestId("context-meter");
    // The meter text should start with ~5,000 (localeString may vary by env).
    // Check the raw number is in the text. 5000 draft tokens, 0 from draft.
    expect(meter.textContent).toMatch(/5[,.]?000/);
  });
});
