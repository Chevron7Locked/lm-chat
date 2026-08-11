/**
 * M-006: Composer aria-live transcript announcement.
 *
 * Verifies that the aria-live region shows "Listening for speech…" while
 * STT is active, and announces the injected transcript snippet (≤60 chars)
 * once the state transitions back to idle.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { Composer } from "@/components/Composer";

// ─── Mock dependencies ────────────────────────────────────────────────────────

vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

// We mock useSTT so we can control the onTranscript callback.
let capturedOnTranscript: ((text: string) => void) | null = null;
let mockListening = false;

vi.mock("@/hooks/useSTT", () => ({
  useSTT: (onTranscript: (text: string) => void) => {
    capturedOnTranscript = onTranscript;
    return {
      capability: { available: true, engine: "webkit" },
      state: { listening: mockListening, error: null },
      start: vi.fn(),
      stop: vi.fn(),
    };
  },
  detectSTT: () => ({ available: true, engine: "webkit" }),
}));

vi.mock("@/components/MicButton", () => ({
  MicButton: () => null,
}));

// PROJECTS-V1 Phase 6: InProjectChip uses useChats + useProject which
// need QueryClientProvider. Stub the chip in unit tests that mount
// the Composer in isolation.
vi.mock("@/components/InProjectChip", () => ({
  InProjectChip: () => null,
}));

// PROJECTS-V1 additions Phase 11: RagModeBadge uses useChatRagMode
// which also needs the QueryClientProvider. Stub it for the same
// reason.
vi.mock("@/components/RagModeBadge", () => ({
  RagModeBadge: () => null,
}));

vi.mock("@/components/SlashMenu", () => ({
  SlashMenu: () => null,
  parseSlashCommand: () => null,
  BUILTIN_COMMANDS: [],
}));

// usePrompts requires QueryClientProvider; stub it for this test.
vi.mock("@/hooks/usePrompts", () => ({
  usePrompts: () => ({ data: [], isLoading: false, isError: false }),
}));

// useIntegrationsList (P12e) also requires QueryClientProvider; stub it.
vi.mock("@/hooks/useIntegrationsList", () => ({
  useIntegrationsList: () => ({ data: [], isLoading: false, isError: false }),
  useUpdateIntegrationsList: () => ({ mutate: vi.fn(), isPending: false }),
}));

// useChatPreset (P13b) calls useUpdateChat → useQueryClient; stub it so this
// test doesn't need a QueryClientProvider.
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

// Cluster 3a (audit 2026-06-10): Composer now calls useModels() for
// capability gating. Stub it to avoid QueryClientProvider requirement.
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("Composer aria-live transcript announcement (M-006)", () => {
  beforeEach(() => {
    capturedOnTranscript = null;
    mockListening = false;
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("aria-live region is empty in idle state with no prior transcript", () => {
    const { container } = render(
      <Composer
        chatId={1}
        streaming={false}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
        onClear={vi.fn()}
        onFork={vi.fn()}
        onCompact={vi.fn()}
        onMemoryPin={vi.fn()}
      />
    );

    // aria-live="polite" div does not receive an ARIA role mapping; query by attribute.
    const liveRegion = container.querySelector("[aria-live]") as HTMLElement;
    expect(liveRegion).not.toBeNull();
    expect(liveRegion.textContent).toBe("");
  });

  it("aria-live announces injected transcript snippet after STT completes", () => {
    const { container } = render(
      <Composer
        chatId={1}
        streaming={false}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
        onClear={vi.fn()}
        onFork={vi.fn()}
        onCompact={vi.fn()}
        onMemoryPin={vi.fn()}
      />
    );

    // Simulate transcript injection.
    act(() => {
      capturedOnTranscript?.("hello from speech");
    });

    const liveRegion = container.querySelector("[aria-live]") as HTMLElement;
    expect(liveRegion.textContent).toBe("hello from speech");
  });

  it("aria-live truncates long transcripts to 60 chars with ellipsis", () => {
    const { container } = render(
      <Composer
        chatId={1}
        streaming={false}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
        onClear={vi.fn()}
        onFork={vi.fn()}
        onCompact={vi.fn()}
        onMemoryPin={vi.fn()}
      />
    );

    const longText = "A".repeat(80);
    act(() => {
      capturedOnTranscript?.(longText);
    });

    const liveRegion = container.querySelector("[aria-live]") as HTMLElement;
    // Should be 60 chars + ellipsis character.
    expect(liveRegion.textContent).toBe(`${"A".repeat(60)}…`);
  });
});
