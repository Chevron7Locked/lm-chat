/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat — stale pinned-model auto-switch (2026-07-29 live bug).
 *
 * Root cause: a chat can be pinned to a model whose catalog key no longer
 * exists in LM Studio (operator renamed/replaced the model). The
 * `<select>` can't render a value that isn't one of its options, so it
 * visually falls back to the first loaded model while React state — and
 * therefore the send payload — still holds the dead key. The backend then
 * hard-errors on a model the user never picked ("sees 9b, but errors on
 * 122b").
 *
 * Fix: Chat.tsx detects (via useChatModelOptions) when the chat's
 * persisted `model_id` is absent from the catalog and auto-switches to a
 * valid model (`updateChat.mutate`) plus surfaces a non-blocking warning
 * toast. This pins that contract:
 *
 *  1. Stale pin (not in options) → auto-switch fires: exactly one
 *     `updateChat.mutate({ model_id, provider })` PATCH to the fallback
 *     model, and exactly one warning toast naming the stale model. The
 *     fallback must be a LOADED model — a saved default that is merely
 *     catalog-present but idled-out (loaded: false) is not an acceptable
 *     target, since the backend's explicit-unloaded gate would still
 *     hard-error on it (swapping one unloaded pin for another).
 *  2. Pin already valid (in options) → no PATCH, no toast.
 *  3. Empty/implicit model_id (no explicit pin) → no PATCH, no toast — the
 *     backend already resolves an implicit default; persisting a
 *     masquerade here would reintroduce the documented implicit-default
 *     persistence bug.
 *
 * Mock prelude mirrors test_Chat_selectedModel_chatswitch.spec.tsx.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { __resetChatScopedMemoryForTests } from "@/hooks/useChatScopedState";
import { AUTO_MODEL_VALUE } from "@/components/chat/shared";

// jsdom doesn't implement scrollIntoView; Chat's auto-scroll effect crashes
// without this stub.
if (typeof window !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function (): void { /* no-op */ };
}

// ─── Mocks for heavy hooks / network surfaces ────────────────────────────────

vi.mock("@/hooks/useViewport", () => ({
  useViewport: () => ({ isMobile: false }),
}));

vi.mock("@/hooks/useKeyboardInset", () => ({
  useKeyboardInset: () => undefined,
}));

vi.mock("@/hooks/usePlatform", () => ({
  usePlatform: () => ({ modLabel: "Ctrl", isMac: false, isWindows: false, isLinux: true }),
}));

vi.mock("@/hooks/useDocumentTitle", () => ({
  useDocumentTitle: () => undefined,
}));

vi.mock("@/hooks/useFocusTrap", () => ({
  useFocusTrap: () => undefined,
}));

vi.mock("@/hooks/usePresence", () => ({
  usePresence: () => ({
    composerCbs: {},
    isAnyoneTyping: false,
    typingUsers: [],
    onlineUsers: [],
  }),
}));

vi.mock("@/hooks/useMouseParallax", () => ({
  useMouseParallax: () => undefined,
}));

vi.mock("@/hooks/useKeyboardShortcuts", () => ({
  useKeyboardShortcuts: () => undefined,
}));

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    state: {
      status: "idle",
      messageId: null,
      responseId: null,
      contentDeltas: [],
      reasoningDeltas: [],
      toolCalls: [],
      error: null,
      stats: { tokensPerSecond: null, ttftSeconds: null, outputTokens: 0 },
      loadPhase: null,
      warnings: [],
    },
    start: vi.fn(),
    stop: vi.fn(),
  }),
}));

vi.mock("@/hooks/useABStream", () => ({
  useABStream: () => ({
    state: { status: "idle", paneA: { status: "idle", contentDeltas: [] }, paneB: { status: "idle", contentDeltas: [] } },
    start: vi.fn(),
    stop: vi.fn(),
  }),
}));

vi.mock("@/hooks/useSubSessionSSE", () => ({
  useSubSessionSSE: () => ({
    state: { status: "idle", content: "", error: null },
    stream: vi.fn(),
    finalize: vi.fn(),
    abort: vi.fn(),
    reset: vi.fn(),
  }),
}));

vi.mock("@/hooks/useModelList", () => ({
  useModelList: () => ({
    status: "connected",
    models: [],
    loadedModels: [],
    error: null,
    isFetching: false,
    refresh: async () => undefined,
  }),
}));

// Saved default model — present in the catalog but IDLED OUT (loaded:
// false). This is the crux of the loaded-fallback refinement: the saved
// default alone is not a safe fallback target if it isn't actually loaded
// (the backend's explicit-unloaded gate would still hard-error), so the
// auto-switch must prefer a genuinely LOADED option over the merely
// catalog-present default.
vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({ data: { default_model: "idled-default-7b" } }),
}));

// The catalog: "pinqwen-9b" is loaded; "idled-default-7b" (the saved
// default) is present but NOT loaded. "oym-qimi-122b-a10b-k2.6-i1" (a
// chat's stored pin in the live bug report) is NOT in this list at all —
// the operator renamed/replaced it in LM Studio.
const mockChatModelCapabilities = {
  vision: false,
  trained_for_tool_use: true,
  reasoning: null,
  embedding: false,
};
const mockChatModelOptions = [
  {
    id: "pinqwen-9b",
    label: "pinqwen 9b",
    loaded: true,
    provider: "lmstudio",
    capabilities: mockChatModelCapabilities,
  },
  {
    id: "idled-default-7b",
    label: "idled default 7b (unloaded)",
    loaded: false,
    provider: "lmstudio",
    capabilities: mockChatModelCapabilities,
  },
];
vi.mock("@/hooks/useChatModelOptions", () => ({
  useChatModelOptions: () => ({
    options: mockChatModelOptions,
    groups: [
      { provider: "lmstudio", label: "LM Studio", options: mockChatModelOptions },
    ],
    isLoading: false,
    isError: false,
  }),
}));

// Three chats: a stale pin, a valid pin, and an implicit (empty) pin.
const mockChats = [
  {
    id: 1,
    title: "Stale pin chat",
    model_id: "oym-qimi-122b-a10b-k2.6-i1",
    pinned: false,
    incognito: false,
    updated_at: "2026-07-29T00:00:00Z",
    settings: {},
  },
  {
    id: 2,
    title: "Valid pin chat",
    model_id: "pinqwen-9b",
    pinned: false,
    incognito: false,
    updated_at: "2026-07-29T00:00:00Z",
    settings: {},
  },
  {
    id: 3,
    title: "Implicit default chat",
    model_id: null,
    pinned: false,
    incognito: false,
    updated_at: "2026-07-29T00:00:00Z",
    settings: {},
  },
];

// Records every updateChat.mutate call along with the chat id the
// useUpdateChat hook instance was created for.
const updateChatCalls = vi.hoisted(
  () => [] as { chatId: number; payload: unknown }[],
);

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: mockChats, isLoading: false, isError: false }),
  useMessages: () => ({ data: { messages: [] }, refetch: vi.fn() }),
  useUpdateChat: (chatId: number) => ({
    mutate: (payload: unknown) => {
      updateChatCalls.push({ chatId, payload });
    },
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useDeleteChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useClearChatMessages: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useForkChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useCompactChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useAppendMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useCreateChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useEditMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useRegenerateMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useDeleteMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useGenerateTitle: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  chatKeys: { messages: (id: number) => ["messages", id] },
}));

vi.mock("@/hooks/useChatPreset", () => ({
  useChatPreset: () => ({ activePreset: "", preset: null, setPreset: vi.fn(), clearPreset: vi.fn() }),
  useHydrateChatPresets: () => undefined,
  useChatPresetStore: (sel: (s: { overrides: Record<number, string>; sources: Record<number, "user" | "model"> }) => unknown) =>
    sel({ overrides: {}, sources: {} }),
}));

vi.mock("@/hooks/useMemory", () => ({
  usePinInsight: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}));

// ModelSelectControl stub: surfaces the resolved composite modelId so tests
// can assert what's actually shown after the auto-switch.
vi.mock("@/components/ModelSelectControl", () => ({
  ModelSelectControl: ({ value }: { value: string; onChange: (id: string) => void }) =>
    createElement(
      "div",
      { "data-testid": "mock-model-select" },
      createElement("span", { "data-testid": "model-select-value" }, value),
    ),
  ModelCapabilityIcons: () => null,
}));

// Stores ----------------------------------------------------------------------

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({
    user: { id: 1, username: "test", is_admin: false },
    isInitializing: false,
  }),
}));

// Records every toast push() call so tests can assert the warning notice.
const toastPushCalls = vi.hoisted(
  () => [] as { variant: string; message: string }[],
);
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({
    push: (opts: { variant: string; message: string }) => {
      toastPushCalls.push(opts);
      return "toast-id";
    },
  }),
}));

vi.mock("@/stores/titleGenerationStore", () => ({
  useTitleGenerationStore: (selector: (s: { begin: () => void; end: () => void }) => unknown) =>
    selector({ begin: vi.fn(), end: vi.fn() }),
}));

vi.mock("@/stores/chatSettingsStore", () => ({
  useChatSettingsStore: () => ({
    hydrateFromChats: vi.fn(),
    chatOverrides: {},
  }),
}));

// Heavy components stubbed (mirrors test_Chat.spec.tsx).
vi.mock("@/components/Sidebar", () => ({
  Sidebar: () => createElement("div", { "data-testid": "mock-sidebar" }, "sidebar"),
}));

vi.mock("@/components/Composer", () => ({
  Composer: () =>
    createElement(
      "div",
      { "data-testid": "mock-composer" },
      createElement("textarea", { "aria-label": "Message" }),
    ),
}));

vi.mock("@/components/ChatMessage", () => ({
  ChatMessage: () => createElement("div", { "data-testid": "mock-chatmessage" }),
}));

vi.mock("@/components/ABCompareView", () => ({
  ABCompareView: () => createElement("div", { "data-testid": "mock-abcompare" }),
}));

vi.mock("@/components/ThinkingIndicator", () => ({
  ThinkingIndicator: () => createElement("div", { "data-testid": "mock-thinking" }),
}));

vi.mock("@/components/LmStudioStatusBadge", () => ({
  LmStudioStatusBadge: () => createElement("div", { "data-testid": "mock-lm-badge" }),
}));

vi.mock("@/components/PinNavStrip", () => ({
  PinNavStrip: () => createElement("div", { "data-testid": "mock-pinnav" }),
}));

vi.mock("@/components/PinnedMessagesPanel", () => ({
  PinnedMessagesPanel: () => createElement("div", { "data-testid": "mock-pinned-panel" }),
}));

vi.mock("@/components/BrandMark", () => ({
  BRAND_NAME: "LMChat",
  BrandMark: () => createElement("div", { "data-testid": "mock-brandmark" }),
}));

vi.mock("@/components/OverflowMenu", () => ({
  OverflowMenu: () => createElement("div", { "data-testid": "mock-overflow" }),
}));

vi.mock("@/components/ReasoningToggle", () => ({
  ReasoningToggle: () => createElement("div", { "data-testid": "mock-reasoning-toggle" }),
}));

vi.mock("@/components/InterruptedRow", () => ({
  InterruptedRow: () => createElement("div", { "data-testid": "mock-interrupted" }),
  clearOrphanedSSEKeys: vi.fn(),
  loadOrphanedResponseId: () => null,
}));

vi.mock("@/components/SlashPalette", () => ({
  SlashPalette: () => createElement("div", { "data-testid": "mock-slash-palette" }),
}));

vi.mock("@/components/SlashMenu", () => ({
  SlashMenu: () => createElement("div", { "data-testid": "mock-slash-menu" }),
  BUILTIN_COMMANDS: [],
}));

vi.mock("@/components/KeyboardHelp", () => ({
  KeyboardHelp: () => createElement("div", { "data-testid": "mock-keyboardhelp" }),
}));

vi.mock("@/components/ChatHeaderMenu", () => ({
  ChatHeaderMenu: () => createElement("div", { "data-testid": "mock-chatheadermenu" }),
}));

vi.mock("@/components/ui/Drawer", () => ({
  Drawer: ({ children }: { children: React.ReactNode }) =>
    createElement("div", { "data-testid": "mock-drawer" }, children),
}));

// ─── Helpers ─────────────────────────────────────────────────────────────────

import Chat from "@/pages/Chat";

function renderChat(initialPath: string) {
  return render(
    createElement(
      QueryClientProvider,
      {
        client: new QueryClient({
          defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
        }),
      },
      createElement(
        MemoryRouter,
        { initialEntries: [initialPath] },
        createElement(
          Routes,
          null,
          createElement(Route, { path: "/chats/:chatId", element: createElement(Chat) }),
        ),
      ),
    ),
  );
}

function shownModel(): string {
  return screen.getByTestId("model-select-value").textContent ?? "";
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — stale pinned-model auto-switch", () => {
  beforeEach(() => {
    __resetChatScopedMemoryForTests();
    updateChatCalls.length = 0;
    toastPushCalls.length = 0;
    vi.clearAllMocks();
  });

  it("auto-switches to the LOADED model, not the merely-catalog-present (but idled) saved default", () => {
    renderChat("/chats/1");

    // savedDefaultModel is "idled-default-7b" — present in the catalog but
    // loaded:false. The fix must skip it and land on "pinqwen-9b", the
    // first genuinely loaded option. (Pre-fix behavior would have picked
    // "idled-default-7b" here, since it only checked catalog presence —
    // that's exactly the bug this refinement closes.)
    expect(shownModel()).toBe("lmstudio::pinqwen-9b");

    expect(updateChatCalls).toEqual([
      { chatId: 1, payload: { model_id: "pinqwen-9b", provider: "lmstudio" } },
    ]);

    expect(toastPushCalls).toHaveLength(1);
    expect(toastPushCalls[0]?.variant).toBe("warning");
    expect(toastPushCalls[0]?.message).toContain("oym-qimi-122b-a10b-k2.6-i1");
    expect(toastPushCalls[0]?.message).toContain("pinqwen-9b");
    expect(toastPushCalls[0]?.message).not.toContain("idled-default-7b");
  });

  it("does NOT fire when the persisted model is already a valid catalog entry", () => {
    renderChat("/chats/2");

    expect(shownModel()).toBe("lmstudio::pinqwen-9b");
    expect(updateChatCalls).toHaveLength(0);
    expect(toastPushCalls).toHaveLength(0);
  });

  it("does NOT fire for an empty/implicit model_id (backend-resolved default)", () => {
    renderChat("/chats/3");

    // No explicit per-chat override → the picker shows "Auto" (it resolves to
    // the saved default at send time; the DISPLAY no longer surfaces the
    // default model's name). Critically, the auto-switch effect stays quiet:
    // the implicit-default path must be left untouched regardless of the
    // default's loaded state — no PATCH, no toast.
    expect(shownModel()).toBe(AUTO_MODEL_VALUE);
    expect(updateChatCalls).toHaveLength(0);
    expect(toastPushCalls).toHaveLength(0);
  });
});
