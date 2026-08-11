/**
 * Bug 6 fix (2026-07-10): the vision gate on file attachments must be
 * enforced in handleAttachFiles, not just via the file picker's `accept`
 * filter — drag/paste and model-switch bypass `accept`. These tests drive
 * the attach `<input>`'s onChange handler directly (the same code path
 * fireEvent.change exercises for drag/paste-staged files) against a
 * non-vision model:
 *
 *  - an IMAGE file is rejected: a warning toast is pushed and no chip stages.
 *  - a TEXT file always stages: a chip appears despite non-vision.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { Composer } from "@/components/Composer";

// ─── Mocks ────────────────────────────────────────────────────────────────────

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: pushMock }),
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

// Non-vision model — fixed for every test in this file (Bug 6 is about the
// non-vision path specifically; the vision-model path is unchanged and
// already covered by test_Composer_attach_button.spec.tsx).
const mockModelsData = {
  models: [
    {
      id: "test-model",
      name: "Ornith (non-vision)",
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

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: mockModelsData, isLoading: false, isError: false, refetch: vi.fn() }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("Composer attach gate — non-vision enforcement (Bug 6)", () => {
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
    pushMock.mockClear();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("rejects an image file for a non-vision model: warns, stages no chip", async () => {
    render(<Composer {...baseProps} />);
    const input = screen.getByTestId("attach-file-input") as HTMLInputElement;
    const imageFile = new File(["fake-bytes"], "photo.png", { type: "image/png" });

    fireEvent.change(input, { target: { files: [imageFile] } });

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "warning",
          message: expect.stringContaining("can't view images"),
        }),
      );
    });
    expect(screen.queryByTestId("attach-chip")).toBeNull();
  });

  it("stages a text file for a non-vision model: chip appears, no warning", async () => {
    render(<Composer {...baseProps} />);
    const input = screen.getByTestId("attach-file-input") as HTMLInputElement;
    const textFile = new File(["hello from a text file"], "notes.txt", { type: "text/plain" });

    fireEvent.change(input, { target: { files: [textFile] } });

    await waitFor(() => {
      expect(screen.getByTestId("attach-chip")).toBeTruthy();
    });
    expect(screen.getByText("notes.txt")).toBeTruthy();
    expect(pushMock).not.toHaveBeenCalled();
  });
});
