/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat — the model picker defaults to "Auto".
 *
 * Pins the "Auto" model-picker behaviour:
 *
 *  1. A chat with NO explicit per-chat override (no memory-tier dropdown pick
 *     AND no persisted ``chats.model_id``) shows "Auto" in the header picker —
 *     even when the user HAS a saved default model. "Auto" stands in for that
 *     default at display time; the send path still resolves it to the default.
 *  2. Picking a specific model sets the per-chat override (PATCH model_id +
 *     provider) and the picker then shows that model's composite id.
 *  3. A chat with an explicit persisted model shows that model, not "Auto".
 *  4. Selecting "Auto" resets the override — it PATCHes ``clear=model_id``
 *     (the flat ``model_id=""`` param is ignored server-side) and the picker
 *     returns to "Auto".
 *
 * Mock prelude mirrors test_Chat_selectedModel_chatswitch.spec.tsx; additions:
 * a saved default model via useLmStudioConfig, a fixture whose model_id the
 * useUpdateChat spy actually mutates (so the reset's display update is
 * exercised), and a ModelSelectControl stub that surfaces value + pick/reset
 * buttons.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { __resetChatScopedMemoryForTests } from "@/hooks/useChatScopedState";
import { AUTO_MODEL_VALUE } from "@/components/chat/shared";
import type { ChatSummary } from "@/hooks/useChats";

// jsdom doesn't implement scrollIntoView; Chat's auto-scroll effect crashes
// without this stub.
if (typeof window !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function (): void {
    /* no-op */
  };
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
vi.mock("@/hooks/useDocumentTitle", () => ({ useDocumentTitle: () => undefined }));
vi.mock("@/hooks/useFocusTrap", () => ({ useFocusTrap: () => undefined }));
vi.mock("@/hooks/usePresence", () => ({
  usePresence: () => ({
    composerCbs: {},
    isAnyoneTyping: false,
    typingUsers: [],
    onlineUsers: [],
  }),
}));
vi.mock("@/hooks/useMouseParallax", () => ({ useMouseParallax: () => undefined }));
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
    state: {
      status: "idle",
      paneA: { status: "idle", contentDeltas: [] },
      paneB: { status: "idle", contentDeltas: [] },
    },
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

// The user HAS a saved default — the whole point of the first test is that the
// picker still shows "Auto" rather than surfacing this model's name. Mutable so
// a test can drop the default and assert the no-default prompt path.
let mockDefaultModel: string | undefined = "default-model";
vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({
    data:
      mockDefaultModel !== undefined
        ? { default_model: mockDefaultModel }
        : {},
  }),
}));

// Mutable fixture — the useUpdateChat spy mutates model_id in place so the
// reset-to-Auto display update is realistically exercised.
// Real ChatSummary import above — not a hand-rolled shadow. A local mirror
// of this shape (the previous `ChatFixture`) was missing `folder`,
// `display_order`, `tags`, and `archived_at` — same class of drift as
// `TestChat`/`ChatRow`, invisible because useChatsDirect is mocked and its
// return value isn't checked against the real hook's type.
function makeChats(): ChatSummary[] {
  return [
    {
      id: 1,
      title: "Auto chat",
      folder: null,
      model_id: null, // no explicit override → "Auto"
      pinned: false,
      incognito: false,
      updated_at: "2026-08-11T00:00:00Z",
      settings: {},
      display_order: 0,
      tags: [],
      archived_at: null,
    },
    {
      id: 2,
      title: "Pinned chat",
      folder: null,
      model_id: "model-x", // explicit override
      pinned: false,
      incognito: false,
      updated_at: "2026-08-11T00:00:00Z",
      settings: {},
      display_order: 1,
      tags: [],
      archived_at: null,
    },
  ];
}

let mockChats: ChatSummary[] = makeChats();

const updateChatCalls = vi.hoisted(
  () => [] as { chatId: number; payload: Record<string, unknown> }[],
);

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: mockChats, isLoading: false, isError: false }),
  useMessages: () => ({ data: { messages: [] }, refetch: vi.fn() }),
  useUpdateChat: (chatId: number) => ({
    mutate: (payload: Record<string, unknown>) => {
      updateChatCalls.push({ chatId, payload });
      const row = mockChats.find((c) => c.id === chatId);
      if (row === undefined) return;
      // Mirror the backend: a non-empty model_id SETS the pin; clear=model_id
      // NULLs it. (The flat model_id="" param is ignored server-side.)
      if (typeof payload.model_id === "string" && payload.model_id !== "") {
        row.model_id = payload.model_id;
      }
      if (typeof payload.clear === "string" && payload.clear.includes("model_id")) {
        row.model_id = null;
      }
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

// ModelSelectControl stub: surfaces the resolved value and two buttons —
// "pick a model" fires onChange("lmstudio::model-y"); "reset to auto" fires
// onChange(AUTO_MODEL_VALUE).
vi.mock("@/components/ModelSelectControl", () => ({
  ModelSelectControl: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (id: string) => void;
  }) =>
    createElement(
      "div",
      { "data-testid": "mock-model-select" },
      createElement("span", { "data-testid": "model-select-value" }, value),
      createElement(
        "button",
        {
          type: "button",
          "data-testid": "model-select-pick-y",
          onClick: () => {
            onChange("lmstudio::model-y");
          },
        },
        "pick model-y",
      ),
      createElement(
        "button",
        {
          type: "button",
          "data-testid": "model-select-pick-auto",
          onClick: () => {
            onChange("__auto__");
          },
        },
        "reset to auto",
      ),
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
vi.mock("@/stores/toastStore", () => ({ useToast: () => ({ push: vi.fn() }) }));
vi.mock("@/stores/titleGenerationStore", () => ({
  useTitleGenerationStore: (selector: (s: { begin: () => void; end: () => void }) => unknown) =>
    selector({ begin: vi.fn(), end: vi.fn() }),
}));
vi.mock("@/stores/chatSettingsStore", () => ({
  useChatSettingsStore: () => ({ hydrateFromChats: vi.fn(), chatOverrides: {} }),
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

describe("Chat — model picker defaults to Auto", () => {
  beforeEach(() => {
    __resetChatScopedMemoryForTests();
    updateChatCalls.length = 0;
    mockChats = makeChats();
    mockDefaultModel = "default-model";
    vi.clearAllMocks();
  });

  it("shows Auto when the chat has no override — even with a saved default model", () => {
    renderChat("/chats/1");
    expect(shownModel()).toBe(AUTO_MODEL_VALUE);
    // Rendering an Auto chat never PATCHes anything.
    expect(updateChatCalls).toHaveLength(0);
  });

  it("picking a model sets the per-chat override and shows it (composite id)", () => {
    renderChat("/chats/1");
    expect(shownModel()).toBe(AUTO_MODEL_VALUE);

    fireEvent.click(screen.getByTestId("model-select-pick-y"));
    expect(shownModel()).toBe("lmstudio::model-y");
    expect(updateChatCalls).toEqual([
      { chatId: 1, payload: { model_id: "model-y", provider: "lmstudio" } },
    ]);
  });

  it("a chat with an explicit persisted model shows that model, not Auto", () => {
    renderChat("/chats/2");
    expect(shownModel()).toBe("lmstudio::model-x");
    expect(updateChatCalls).toHaveLength(0);
  });

  it("selecting Auto resets the override via clear=model_id and returns to Auto", () => {
    renderChat("/chats/2");
    expect(shownModel()).toBe("lmstudio::model-x");

    fireEvent.click(screen.getByTestId("model-select-pick-auto"));
    // Persisted override cleared through the explicit-NULL path.
    expect(updateChatCalls).toEqual([
      { chatId: 2, payload: { clear: "model_id" } },
    ]);
    // Picker returns to Auto (the fixture's model_id is now NULL).
    expect(shownModel()).toBe(AUTO_MODEL_VALUE);
  });

  it("with NO default configured, a no-override chat prompts (value '') rather than Auto", () => {
    // "Auto" only makes sense when there's a default to resolve to. With no
    // default AND no override, the picker value falls to "" so the header
    // prompts "Select a model…" (the composer would otherwise block the send).
    mockDefaultModel = undefined;
    renderChat("/chats/1");
    expect(shownModel()).toBe("");
    expect(updateChatCalls).toHaveLength(0);
  });

  it("selecting Auto on an already-Auto chat is a no-op (no PATCH)", () => {
    renderChat("/chats/1");
    expect(shownModel()).toBe(AUTO_MODEL_VALUE);

    fireEvent.click(screen.getByTestId("model-select-pick-auto"));
    // Nothing persisted to clear → no network call.
    expect(updateChatCalls).toHaveLength(0);
    expect(shownModel()).toBe(AUTO_MODEL_VALUE);
  });
});
