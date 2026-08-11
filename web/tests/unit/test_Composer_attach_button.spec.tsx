/**
 * Cluster 3a Task 6 (audit 2026-06-10); Bug 6 fix (2026-07-10): Attach button.
 *
 * BUG 6: the attach control used to be hidden entirely unless
 * capabilities.vision === true, even though text-file attachments are
 * decoded and folded into the message text and never need vision. The gate
 * now only applies to IMAGE uploads (enforced in handleAttachFiles, not just
 * the `accept` filter) — the attach control itself is always shown.
 *
 *  - shows the attach button for a vision model, with image+text `accept`.
 *  - shows the attach button for a non-vision model too, but `accept`
 *    excludes image/* (text-only picker hint).
 *  - shows the attach button when capabilities are unknown (not-yet-loaded
 *    model), treated like non-vision: text-only `accept`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
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

// Controllable models mock for capability gating.
type ModelCapabilitiesShape = {
  vision: boolean;
  trained_for_tool_use: boolean;
  reasoning: { default: string; allowed_options: string[] } | null;
  embedding: boolean;
};

let mockModelsData:
  | {
      models: Array<{
        id: string;
        name: string;
        loaded: boolean;
        loaded_instance_ids: string[];
        capabilities: ModelCapabilitiesShape;
        max_context_length: number;
        size_bytes: number;
        params_string: string;
      }>;
    }
  | undefined = undefined;

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: mockModelsData, isLoading: false, isError: false, refetch: vi.fn() }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("Composer attach button (Cluster 3a Task 6)", () => {
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
    mockModelsData = undefined;
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  // Spec test: test_Composer_attach_button_gated_on_vision_capability
  it("test_Composer_attach_button_gated_on_vision_capability: shows attach button for vision model, accept includes images", () => {
    mockModelsData = {
      models: [
        {
          id: "test-model",
          name: "Test Vision Model",
          loaded: true,
          loaded_instance_ids: ["test-model"],
          capabilities: {
            vision: true,
            trained_for_tool_use: false,
            reasoning: null,
            embedding: false,
          },
          max_context_length: 8192,
          size_bytes: 0,
          params_string: "",
        },
      ],
    };
    render(<Composer {...baseProps} />);
    expect(screen.getByTestId("attach-button")).toBeTruthy();
    const input = screen.getByTestId("attach-file-input");
    expect(input.getAttribute("accept")).toContain("image/");
    expect(input.getAttribute("accept")).toContain("text/plain");
  });

  // BUG 6: non-vision models must still show the attach control — text
  // attachments never need vision. Only the image accept-hint is dropped.
  it("shows attach button for non-vision model, but accept excludes images", () => {
    mockModelsData = {
      models: [
        {
          id: "test-model",
          name: "Test Non-Vision Model",
          loaded: true,
          loaded_instance_ids: ["test-model"],
          capabilities: {
            vision: false,
            trained_for_tool_use: false,
            reasoning: null,
            embedding: false,
          },
          max_context_length: 8192,
          size_bytes: 0,
          params_string: "",
        },
      ],
    };
    render(<Composer {...baseProps} />);
    expect(screen.getByTestId("attach-button")).toBeTruthy();
    const input = screen.getByTestId("attach-file-input");
    expect(input.getAttribute("accept")).not.toContain("image/");
    expect(input.getAttribute("accept")).toContain("text/plain");
  });

  it("shows attach button when model capabilities are unknown (text-only fallback)", () => {
    // mockModelsData = undefined — no models data, capabilities unknown.
    render(<Composer {...baseProps} />);
    expect(screen.getByTestId("attach-button")).toBeTruthy();
    const input = screen.getByTestId("attach-file-input");
    expect(input.getAttribute("accept")).not.toContain("image/");
    expect(input.getAttribute("accept")).toContain("text/plain");
  });
});
