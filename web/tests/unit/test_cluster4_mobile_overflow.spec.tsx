/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Cluster 4 Task 3 + Layout R1 (F2) — mobile Share/Export wiring.
 *
 * Layout R1 (audit 2026-06-10, F2) folded the mobile Share/Export trigger
 * into the ⋯ OverflowMenu: the visible ChatHeaderMenu trigger was redundant
 * chrome at 390px. The menu now mounts hidden-trigger (zero footprint) and
 * is opened imperatively — the overflow "Share / Export" item ticks the
 * shared signal that flows into ChatHeaderMenu's openSignal prop.
 *
 * This test renders the full Chat page with isMobile=true and a real chatId,
 * then asserts:
 *   1. the OverflowMenu carries a "Share / Export" action,
 *   2. ChatHeaderMenu mounts in hidden-trigger mode with the active chatId,
 *   3. clicking the overflow action ticks ChatHeaderMenu's openSignal.
 * Reverting the F2 wiring (overflow action or hidden mount) leaves this red.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// jsdom doesn't implement scrollIntoView.
if (typeof window !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function (): void { /* no-op */ };
}

// ─── Mocks — same contract as test_Chat.spec.tsx, isMobile=true ──────────────

vi.mock("@/hooks/useViewport", () => ({
  useViewport: () => ({ isMobile: true }),
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

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: [], isLoading: false, isError: false }),
  useMessages: () => ({ data: { messages: [] }, refetch: vi.fn() }),
  useUpdateChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
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

// Heavy components stubbed.
vi.mock("@/components/Sidebar", () => ({
  Sidebar: () => createElement("div", { "data-testid": "mock-sidebar" }, "sidebar"),
}));

vi.mock("@/components/Composer", () => ({
  Composer: () =>
    createElement(
      "div",
      { "data-testid": "mock-composer" },
      createElement("textarea", { "aria-label": "Message" }),
      createElement("button", { "aria-label": "Send message", type: "button" }, "Send"),
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

// OverflowMenu stub — renders its actions as labeled buttons (onClick wired)
// so the test can assert the "Share / Export" action exists and fire it.
vi.mock("@/components/OverflowMenu", () => ({
  OverflowMenu: ({
    actions,
  }: {
    actions: { label: string; onClick: () => void }[];
  }) =>
    createElement(
      "div",
      { "data-testid": "mock-overflow" },
      actions.map((a) =>
        createElement(
          "button",
          { key: a.label, type: "button", onClick: a.onClick },
          a.label,
        ),
      ),
    ),
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

// ChatHeaderMenu stub — captures the props the F2 wiring depends on
// (chatId, hiddenTrigger, openSignal) so we can assert the hidden-trigger
// mount and that the overflow action actually ticks the open signal.
vi.mock("@/components/ChatHeaderMenu", () => ({
  ChatHeaderMenu: ({
    chatId,
    hiddenTrigger,
    openSignal,
  }: {
    chatId: number | null;
    hiddenTrigger?: boolean;
    openSignal: number;
  }) =>
    createElement(
      "div",
      {
        "data-testid": "mock-chatheadermenu",
        "data-chatid": String(chatId),
        "data-hidden-trigger": String(hiddenTrigger === true),
        "data-open-signal": String(openSignal),
      },
      "share/export",
    ),
}));

vi.mock("@/components/ui/Drawer", () => ({
  Drawer: ({ children }: { children: React.ReactNode }) =>
    createElement("div", { "data-testid": "mock-drawer" }, children),
}));

// Static import after all mocks are hoisted.
import Chat from "@/pages/Chat";

function chatRouteTree(initialPath: string) {
  return createElement(
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
        createElement(Route, { path: "/chats", element: createElement(Chat) }),
        createElement(Route, { path: "/chats/:chatId", element: createElement(Chat) }),
        createElement(Route, { path: "/login", element: createElement("div", { "data-testid": "login-page" }, "login") }),
      ),
    ),
  );
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("test_mobile_overflow_menu_has_share_export (renders real Chat.tsx mobile TopBar)", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("mobile OverflowMenu carries the Share / Export action; ChatHeaderMenu mounts hidden-trigger", () => {
    // Render Chat at /chats/1 with isMobile=true.
    // Layout R1 (F2): the mobile TopBar folds Share/Export into the ⋯
    // overflow; ChatHeaderMenu mounts hidden-trigger (no visible chrome).
    render(chatRouteTree("/chats/1"));

    // OverflowMenu is present and carries the Share / Export action.
    const overflow = screen.getByTestId("mock-overflow");
    expect(overflow).toBeTruthy();
    expect(
      within(overflow).getByRole("button", { name: "Share / Export" }),
    ).toBeTruthy();

    // ChatHeaderMenu still mounts (it owns the export/share logic) but in
    // hidden-trigger mode — no second visible trigger at 390px.
    const menu = screen.getByTestId("mock-chatheadermenu");
    expect(menu).toBeTruthy();
    expect(menu.getAttribute("data-hidden-trigger")).toBe("true");
  });

  it("clicking the overflow Share / Export action ticks ChatHeaderMenu's openSignal", () => {
    render(chatRouteTree("/chats/1"));

    const menu = screen.getByTestId("mock-chatheadermenu");
    expect(menu.getAttribute("data-open-signal")).toBe("0");

    fireEvent.click(
      within(screen.getByTestId("mock-overflow")).getByRole("button", {
        name: "Share / Export",
      }),
    );

    expect(
      screen.getByTestId("mock-chatheadermenu").getAttribute("data-open-signal"),
    ).toBe("1");
  });

  it("mobile TopBar renders ChatHeaderMenu with the active chatId", () => {
    // Verify the real chatId flows through to ChatHeaderMenu in the mobile
    // branch (our stub captures it in data-chatid).
    render(chatRouteTree("/chats/42"));

    const menu = screen.getByTestId("mock-chatheadermenu");
    expect(menu).toBeTruthy();
    // chatId=42 should be passed to ChatHeaderMenu (our stub captures it).
    expect(menu.getAttribute("data-chatid")).toBe("42");
  });
});
