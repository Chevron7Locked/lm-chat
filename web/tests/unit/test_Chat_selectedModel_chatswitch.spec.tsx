/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat — selectedModel chat-switch isolation (Item 6, 2026-06-12
 * chat-flow remediation).
 *
 * Pins the fix for the cross-chat model bleed: `selectedModel` moved from an
 * un-keyed `useState` to `useChatScopedState(chatId, "selectedModel",
 * "memory", undefined)`. The contract under test:
 *
 *  1. Open chat A (persisted model X), override to model Y via the dropdown
 *     → A's `chats.model_id` PATCH fires exactly once.
 *  2. Navigate to chat B (persisted model Z) → dropdown shows Z (NOT Y) and
 *     no PATCH fires for B. Previously, chat A's override bled into B's
 *     dropdown AND a send could silently rewrite B's persisted model.
 *  3. Navigate back to A → dropdown shows Y (the memory tier kept A's
 *     override for the tab session).
 *
 * Mock prelude mirrors test_Chat.spec.tsx; additions: a controllable
 * useChatsDirect fixture (two chats with distinct model_ids), a recording
 * useUpdateChat spy, a stubbed ModelSelectControl that surfaces
 * value/onChange, and an in-router navigate capture.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { createElement } from "react";
import {
  MemoryRouter,
  Routes,
  Route,
  useNavigate,
  type NavigateFunction,
} from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { __resetChatScopedMemoryForTests } from "@/hooks/useChatScopedState";

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

vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({ data: undefined }),
}));

// Two chats with distinct persisted models — the heart of the fixture.
const mockChats = [
  {
    id: 1,
    title: "Chat A",
    model_id: "model-x",
    pinned: false,
    incognito: false,
    updated_at: "2026-06-12T00:00:00Z",
    settings: {},
  },
  {
    id: 2,
    title: "Chat B",
    model_id: "model-z",
    pinned: false,
    incognito: false,
    updated_at: "2026-06-12T00:00:00Z",
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
  useChatPresetStore: (sel: (s: { overrides: Record<number, string> }) => unknown) =>
    sel({ overrides: {} }),
}));

vi.mock("@/hooks/useMemory", () => ({
  usePinInsight: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}));

// ModelSelectControl stub: surfaces the resolved modelId and a button that
// fires onChange("model-y") — stands in for the user picking model Y.
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
            onChange("model-y");
          },
        },
        "pick model-y",
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

vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: vi.fn() }),
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

// Captures the router's navigate function so tests can navigate between
// chats WITHOUT remounting the router (the Chat instance must stay mounted —
// that's exactly the condition under which the old un-keyed useState bled
// state across chats).
let capturedNavigate: NavigateFunction | null = null;
function NavCapture(): null {
  capturedNavigate = useNavigate();
  return null;
}

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
        createElement(NavCapture),
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

function navigateTo(path: string): void {
  act(() => {
    if (capturedNavigate === null) throw new Error("navigate not captured");
    capturedNavigate(path);
  });
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — selectedModel chat-switch isolation (Item 6)", () => {
  beforeEach(() => {
    __resetChatScopedMemoryForTests();
    updateChatCalls.length = 0;
    capturedNavigate = null;
    vi.clearAllMocks();
  });

  it("override in chat A PATCHes A once; chat B shows its own model with no PATCH; back in A the override is retained", () => {
    renderChat("/chats/1");

    // Chat A resolves its persisted model (no override yet).
    // modelId is now composite "<provider>::<model_id>" — "lmstudio" is the
    // default provider since the mock chat has no settings.provider set.
    expect(shownModel()).toBe("lmstudio::model-x");
    expect(updateChatCalls).toHaveLength(0);

    // User overrides to model Y via the dropdown.
    // The mock onChange fires "model-y" (no provider prefix) → onModelChange
    // decodes it as provider="lmstudio", modelId="model-y" (indexOf returns -1).
    // setSelectedModel stores "model-y"; next render rebuilds composite.
    fireEvent.click(screen.getByTestId("model-select-pick-y"));
    expect(shownModel()).toBe("lmstudio::model-y");
    // Multi-provider: PATCH now includes provider alongside model_id.
    expect(updateChatCalls).toEqual([
      { chatId: 1, payload: { model_id: "model-y", provider: "lmstudio" } },
    ]);

    // Navigate to chat B — dropdown shows B's persisted model Z, NOT A's
    // override, and navigation alone fires NO PATCH (the old bug silently
    // rewrote B's model_id on the next send).
    navigateTo("/chats/2");
    expect(shownModel()).toBe("lmstudio::model-z");
    expect(updateChatCalls).toHaveLength(1);

    // Back to A — the memory-tier override survives for the tab session.
    navigateTo("/chats/1");
    expect(shownModel()).toBe("lmstudio::model-y");
    expect(updateChatCalls).toHaveLength(1);
  });

  it("a chat with no override and no interaction never PATCHes on visit", () => {
    renderChat("/chats/2");
    expect(shownModel()).toBe("lmstudio::model-z");
    navigateTo("/chats/1");
    expect(shownModel()).toBe("lmstudio::model-x");
    navigateTo("/chats/2");
    expect(shownModel()).toBe("lmstudio::model-z");
    expect(updateChatCalls).toHaveLength(0);
  });
});
