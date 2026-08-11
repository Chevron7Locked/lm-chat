/**
 * Unit tests for ChatSettingsRail (P13a, S-01..S-12).
 *
 * Covers:
 *  - renders preset selector, system prompt textarea, temperature input,
 *    advanced expander, quality toggles
 *  - persists temperature on blur via useUpdateChat.mutate
 *  - persists system prompt on blur
 *  - quality toggles persist on change
 *  - reasoning select inside advanced persists with empty-string clear
 *  - initial values are seeded from the chat row's settings
 *
 * Cluster 2 Task 1: mutate() is now called with a second `{ onError }` options
 * argument on every persist. Tests that check the mutation payload now use
 * `toHaveBeenCalledWith(payload, expect.objectContaining({ onError: ... }))`.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { useChatSettingsStore } from "@/stores/chatSettingsStore";

// ─── Mocks ────────────────────────────────────────────────────────────────────

const mockMutate = vi.fn();
let chatSettings: Record<string, unknown> = {};

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({
    data: [
      {
        id: 7,
        title: "Test chat",
        folder: null,
        pinned: false,
        updated_at: "2026-01-01T00:00:00Z",
        model_id: null,
        display_order: 0,
        settings: chatSettings,
      },
    ],
    isLoading: false,
    isError: false,
  }),
  useUpdateChat: () => ({ mutate: mockMutate, isPending: false }),
}));

// useProject is consumed by the ResolvedPromptPreview to surface the
// project layer. The fixtures don't supply a QueryClientProvider so we
// stub the hook directly. We import the actual module to retain the
// other exports the rail (indirectly) needs.
vi.mock("@/hooks/useProjects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useProjects")>();
  return {
    ...actual,
    useProject: () => ({ data: undefined, isLoading: false }),
  };
});

// Cluster 3a (audit 2026-06-10): ChatSettingsRailBody now calls useModels()
// to look up the model's capabilities for reasoning gating. The tests
// don't provide a QueryClientProvider, so mock useModels directly.
vi.mock("@/hooks/useModels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useModels")>();
  return {
    ...actual,
    useModels: () => ({ data: undefined, isLoading: false, isError: false }),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  chatSettings = {};
});

async function renderRail(opts?: { settings?: Record<string, unknown> }) {
  if (opts?.settings !== undefined) {
    chatSettings = opts.settings;
  }
  const { ChatSettingsRail } = await import("@/components/ChatSettingsRail");
  return render(createElement(ChatSettingsRail, { chatId: 7 }));
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("ChatSettingsRail — render", () => {
  it("renders the preset selector", async () => {
    await renderRail();
    expect(screen.getByTestId("chat-settings-preset")).toBeTruthy();
  });

  it("renders the system prompt textarea", async () => {
    await renderRail();
    expect(screen.getByTestId("chat-settings-system-prompt")).toBeTruthy();
  });

  it("renders the temperature input", async () => {
    await renderRail();
    expect(screen.getByTestId("chat-settings-temperature")).toBeTruthy();
  });

  it("renders the advanced expander summary", async () => {
    await renderRail();
    expect(screen.getByTestId("chat-settings-advanced-summary")).toBeTruthy();
  });

  it("renders the three quality toggles", async () => {
    await renderRail();
    expect(screen.getByTestId("chat-settings-sc")).toBeTruthy();
    expect(screen.getByTestId("chat-settings-cove")).toBeTruthy();
    expect(screen.getByTestId("chat-settings-stateless")).toBeTruthy();
  });

  it("seeds initial values from chat settings", async () => {
    await renderRail({
      settings: {
        system_prompt: "Be concise.",
        temperature: 0.5,
        self_consistency_enabled: true,
      },
    });
    const sp = screen.getByTestId("chat-settings-system-prompt") as HTMLTextAreaElement;
    expect(sp.value).toBe("Be concise.");
    const temp = screen.getByTestId("chat-settings-temperature") as HTMLInputElement;
    expect(temp.value).toBe("0.5");
    const sc = screen.getByTestId("chat-settings-sc") as HTMLInputElement;
    expect(sc.checked).toBe(true);
  });
});

describe("ChatSettingsRail — persist", () => {
  it("persists temperature on blur", async () => {
    await renderRail();
    const input = screen.getByTestId("chat-settings-temperature") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "0.8" } });
    fireEvent.blur(input);
    // Cluster 2 Task 1: mutate now called with (payload, { onError }).
    expect(mockMutate).toHaveBeenCalledWith(
      { temperature: 0.8 },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("sends explicit null when temperature is cleared (empty string)", async () => {
    // F3 fix: previously the rail returned early on empty string, silently
    // dropping the user's intent to clear the override. Now it sends an
    // explicit null so the backend wipes the stored value.
    await renderRail({ settings: { temperature: 0.5 } });
    const input = screen.getByTestId("chat-settings-temperature") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.blur(input);
    expect(mockMutate).toHaveBeenCalledWith(
      { temperature: null },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("persists system prompt on blur", async () => {
    await renderRail();
    const ta = screen.getByTestId("chat-settings-system-prompt") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "Be very concise." } });
    fireEvent.blur(ta);
    expect(mockMutate).toHaveBeenCalledWith(
      { system_prompt: "Be very concise." },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("persists SC toggle on change", async () => {
    await renderRail();
    const cb = screen.getByTestId("chat-settings-sc") as HTMLInputElement;
    fireEvent.click(cb);
    expect(mockMutate).toHaveBeenCalledWith(
      { self_consistency_enabled: true },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("persists CoVe toggle on change", async () => {
    await renderRail();
    const cb = screen.getByTestId("chat-settings-cove") as HTMLInputElement;
    fireEvent.click(cb);
    expect(mockMutate).toHaveBeenCalledWith(
      { chain_of_verification_enabled: true },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("persists stateless toggle on change", async () => {
    await renderRail();
    const cb = screen.getByTestId("chat-settings-stateless") as HTMLInputElement;
    fireEvent.click(cb);
    expect(mockMutate).toHaveBeenCalledWith(
      { stateless: true },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("persists preset selection", async () => {
    // P13b: dropdown ids now use canonical preset ids from
    // @/lib/presets (e.g. "coder", not the slash-command alias "code").
    // useChatPreset writes via useUpdateChat → same mocked mutate as the
    // other rail fields.
    await renderRail();
    const sel = screen.getByTestId("chat-settings-preset") as HTMLSelectElement;
    fireEvent.change(sel, { target: { value: "coder" } });
    expect(mockMutate).toHaveBeenCalledWith({ active_preset: "coder" });
  });

  it("persists reasoning select clear with empty string", async () => {
    // Cluster 3a Task 5 (audit 2026-06-10, locked decision 2): the rail
    // now writes to ``reasoning_effort`` (canonical key), not ``reasoning``.
    // Settings seeded from ``reasoning_effort`` first, falling back to
    // ``reasoning`` for backward-compat with pre-fix chats.
    await renderRail({ settings: { reasoning_effort: "high" } });
    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    fireEvent.change(sel, { target: { value: "" } });
    expect(mockMutate).toHaveBeenCalledWith(
      { reasoning_effort: "" },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("persists reasoning select choice to ``reasoning_effort`` canonical key", async () => {
    // Cluster 3a Task 5 (audit 2026-06-10, locked decision 2): canonical key is
    // ``reasoning_effort`` (NOT the alias ``reasoning``).
    await renderRail();
    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    fireEvent.change(sel, { target: { value: "high" } });
    expect(mockMutate).toHaveBeenCalledWith(
      { reasoning_effort: "high" },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("persists advanced top_p on blur", async () => {
    await renderRail();
    const input = screen.getByTestId("chat-settings-top-p") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "0.9" } });
    fireEvent.blur(input);
    expect(mockMutate).toHaveBeenCalledWith(
      { top_p: 0.9 },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("persists advanced max_tokens on blur", async () => {
    await renderRail();
    const input = screen.getByTestId("chat-settings-max-tokens") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "1024" } });
    fireEvent.blur(input);
    expect(mockMutate).toHaveBeenCalledWith(
      { max_tokens: 1024 },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  // Cluster 3a Task 5 spec test (audit 2026-06-10): dedicated
  // test_ChatSettingsRail_reasoning_effort_canonical_key as required by spec.
  it("test_ChatSettingsRail_reasoning_effort_canonical_key: seeds from reasoning_effort and writes reasoning_effort canonical key", async () => {
    // Seed from the canonical key.
    await renderRail({ settings: { reasoning_effort: "medium" } });
    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    // Seeded value should match.
    expect(sel.value).toBe("medium");
    // Writing a new value must use the canonical key (not the alias "reasoning").
    fireEvent.change(sel, { target: { value: "low" } });
    expect(mockMutate).toHaveBeenCalledWith(
      { reasoning_effort: "low" },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  // Finding 3 fix (revision 2026-06-10): reasoning select changes must also
  // update chatSettingsStore.chatOverrides so the adjacent ReasoningToggle
  // stays in sync. Previously select→toggle direction was broken because
  // persistReasoning only called updateChat.mutate and never setChatReasoning.
  it("reasoning select change syncs chatSettingsStore (select→toggle direction)", async () => {
    // Reset the store so it has no prior override for chat 7.
    useChatSettingsStore.setState({ chatOverrides: {} });
    await renderRail({ settings: { reasoning_effort: "off" } });
    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    fireEvent.change(sel, { target: { value: "high" } });
    // The BE mutation should fire with reasoning_effort + Cluster 2
    // onError handler (so a failed PATCH surfaces a toast).
    expect(mockMutate).toHaveBeenCalledWith(
      { reasoning_effort: "high" },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
    // The zustand store must also be updated so the ReasoningToggle reads
    // the new value immediately (without waiting for a BE round-trip).
    const storeOverride = useChatSettingsStore.getState().chatOverrides[7];
    expect(storeOverride).toBe("high");
  });

  it("reasoning select clear syncs chatSettingsStore with empty sentinel", async () => {
    useChatSettingsStore.setState({ chatOverrides: { 7: "high" } });
    await renderRail({ settings: { reasoning_effort: "high" } });
    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    fireEvent.change(sel, { target: { value: "" } });
    expect(mockMutate).toHaveBeenCalledWith(
      { reasoning_effort: "" },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
    // Empty string ("use global default") should land in the store as "".
    const storeOverride = useChatSettingsStore.getState().chatOverrides[7];
    expect(storeOverride).toBe("");
  });

  // Finding 3 fix (Round 3, 2026-06-10): toggle→select direction.
  // When ReasoningToggle cycles chatOverrides[chatId] in Zustand, the
  // rail's local reasoning select must reflect the new value immediately —
  // before the K-001 PATCH round-trip completes. The fix: a useEffect in
  // ChatSettingsRailBody watches chatOverrides[chatId] and calls setReasoning.
  it("toggle→select direction: reasoning select updates when chatOverrides changes", async () => {
    // Start with no override — select shows server value "off".
    useChatSettingsStore.setState({ chatOverrides: {} });
    const { rerender } = await renderRail({ settings: { reasoning_effort: "off" } });
    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    expect(sel.value).toBe("off");

    // Simulate what ReasoningToggle does: write a new override to the store.
    // This mimics the toggle cycling from "off" to "medium".
    const { act } = await import("@testing-library/react");
    await act(async () => {
      useChatSettingsStore.getState().setChatReasoning(7, "medium");
    });

    // The select must now show "medium" — without a PATCH round-trip.
    expect(sel.value).toBe("medium");
    void rerender;
  });
});

// ─── fe-components-state-12: effective-value consistency contract ──────────
//
// Rail's reasoning <select> and the mounted ReasoningToggle button previously
// derived their displayed level from TWO different sources: the select read
// `chat.settings.reasoning_effort` (with a legacy-alias fallback); the toggle
// read ONLY `chatSettingsStore.chatOverrides`, which is seeded from settings
// at a single one-time hydrate point elsewhere (Chat.tsx) and never consults
// `chat.settings` directly. A chat visited before that hydrate ran (or one
// whose settings changed via a path that never touched chatOverrides) showed
// a correct value on the select and a WRONG one on the toggle. These tests
// pin the fix: both surfaces must read the SAME effective value, and a write
// on either surface must be observed by the other without a reload.
describe("ChatSettingsRail + ReasoningToggle — effective-value consistency contract (fe-components-state-12)", () => {
  function toggleAriaLabel(): string {
    return (
      screen.getByTestId("reasoning-toggle").getAttribute("aria-label") ?? ""
    );
  }

  it("contract: with a per-chat reasoning_effort in settings and NO in-session override yet, the select and the toggle show the same level", async () => {
    // Simulates a chat whose reasoning_effort was set server-side (e.g. a
    // fork, or a slash-command write) before chatSettingsStore's one-time
    // hydrate ever ran for this chat id — chatOverrides has no entry.
    useChatSettingsStore.setState({ chatOverrides: {}, globalReasoning: "off" });
    await renderRail({ settings: { reasoning_effort: "high" } });

    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    expect(sel.value).toBe("high");

    // RED ON REVERT: before the fix, ReasoningToggle ignored chat.settings
    // entirely when chatOverrides[chatId] was undefined and fell straight
    // through to globalReasoning ("off") — disagreeing with the select.
    expect(toggleAriaLabel()).toBe(
      "Reasoning effort: high. Click to cycle, right-click for options.",
    );
  });

  it("contract: a write on the select surface is observed by the toggle without a reload", async () => {
    useChatSettingsStore.setState({ chatOverrides: {}, globalReasoning: "off" });
    await renderRail({ settings: { reasoning_effort: "off" } });

    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    expect(toggleAriaLabel()).toBe(
      "Reasoning effort: off. Click to cycle, right-click for options.",
    );

    fireEvent.change(sel, { target: { value: "high" } });

    // mockMutate is a no-op stub — there is no PATCH round-trip in this
    // test env, so this only passes if both surfaces share one derivation.
    expect(toggleAriaLabel()).toBe(
      "Reasoning effort: high. Click to cycle, right-click for options.",
    );
  });

  it("contract: a write on the toggle surface is observed by the select without a reload", async () => {
    useChatSettingsStore.setState({ chatOverrides: {}, globalReasoning: "off" });
    await renderRail({ settings: { reasoning_effort: "off" } });

    const sel = screen.getByTestId("chat-settings-reasoning") as HTMLSelectElement;
    const toggleBtn = screen.getByTestId("reasoning-toggle");
    expect(sel.value).toBe("off");

    fireEvent.click(toggleBtn); // cycles off -> low

    expect(sel.value).toBe("low");
  });
});
