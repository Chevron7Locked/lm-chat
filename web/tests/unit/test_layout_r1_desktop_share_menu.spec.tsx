/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Layout R1 (F1, audit 2026-06-10) — desktop Share/Export menu is
 * structurally visible.
 *
 * Before this round, Chat.tsx's desktop branch mounted ChatHeaderMenu
 * inside a 0×0 `opacity: 0; pointer-events: none; overflow: hidden`
 * aria-hidden span. The openSignal arithmetic worked, but the dropdown
 * panel rendered clipped inside the zero box — Cmd/Ctrl+Shift+E and the
 * overflow "Share / Export" item silently no-opped.
 *
 * The fix mounts ChatHeaderMenu with `hiddenTrigger` (sr-only trigger,
 * panel in normal flow). This test renders the REAL ChatHeaderMenu inside
 * the real Chat.tsx desktop TopBar and asserts that ticking the export
 * signal (the Cmd/Ctrl+Shift+E handler) produces a panel that is:
 *   - present in the DOM,
 *   - not inside any aria-hidden / opacity-0 / pointer-events-none ancestor,
 *   - actually clickable (the Markdown export item fires the export fn).
 * It also exercises the overflow "Share / Export" path through the same
 * hidden-trigger mount.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act, within } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// jsdom doesn't implement scrollIntoView.
if (typeof window !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function (): void {
    /* no-op */
  };
}

// ─── Mocks — same contract as test_cluster4_mobile_overflow, isMobile=false ──

vi.mock("@/hooks/useViewport", () => ({
  useViewport: () => ({ isMobile: false }),
}));

vi.mock("@/hooks/useKeyboardInset", () => ({
  useKeyboardInset: () => undefined,
}));

vi.mock("@/hooks/usePlatform", () => ({
  usePlatform: () => ({
    modLabel: "Ctrl",
    isMac: false,
    isWindows: false,
    isLinux: true,
  }),
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

// Capture the shortcut handlers Chat.tsx wires up so the test can invoke
// the Cmd/Ctrl+Shift+E handler (onExportChat) directly — same code path
// as the real keydown, minus the key-event plumbing the hook owns.
const captured = vi.hoisted(() => ({
  handlers: {} as { onExportChat?: () => void },
}));

vi.mock("@/hooks/useKeyboardShortcuts", () => ({
  useKeyboardShortcuts: (handlers: { onExportChat?: () => void }) => {
    captured.handlers = handlers;
  },
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

// Real chat data — the real ChatHeaderMenu returns null when chat is null,
// so Chat.tsx must resolve a currentChat for /chats/1.
vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({
    data: [
      {
        id: 1,
        title: "Layout R1 chat",
        model_id: "test-model",
        pinned: false,
        incognito: false,
        updated_at: "2026-06-10T00:00:00Z",
        settings: {},
      },
    ],
    isLoading: false,
    isError: false,
  }),
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
  useChatPreset: () => ({
    activePreset: "",
    preset: null,
    setPreset: vi.fn(),
    clearPreset: vi.fn(),
  }),
  useHydrateChatPresets: () => undefined,
  useChatPresetStore: (sel: (s: { overrides: Record<number, string> }) => unknown) =>
    sel({ overrides: {} }),
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
  useTitleGenerationStore: (
    selector: (s: { begin: () => void; end: () => void }) => unknown,
  ) => selector({ begin: vi.fn(), end: vi.fn() }),
}));

vi.mock("@/stores/chatSettingsStore", () => ({
  useChatSettingsStore: () => ({
    hydrateFromChats: vi.fn(),
    chatOverrides: {},
  }),
}));

// The real ChatHeaderMenu hydrates an existing share token on open — keep
// the network out of the test.
vi.mock("@/lib/api", () => ({
  api: { request: vi.fn(async () => null) },
}));

// Export side effects — asserted as the "panel is clickable" proof.
vi.mock("@/lib/chatExport", () => ({
  downloadChatAsMarkdown: vi.fn(() => true),
  downloadChatAsJson: vi.fn(() => true),
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
      createElement(
        "button",
        { "aria-label": "Send message", type: "button" },
        "Send",
      ),
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
  PinnedMessagesPanel: () =>
    createElement("div", { "data-testid": "mock-pinned-panel" }),
}));

vi.mock("@/components/BrandMark", () => ({
  BRAND_NAME: "LMChat",
  BrandMark: () => createElement("div", { "data-testid": "mock-brandmark" }),
}));

// OverflowMenu stub — renders its actions as labeled buttons so the test
// can fire the desktop "Share / Export" item against the REAL hidden
// ChatHeaderMenu mount.
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
  ReasoningToggle: () =>
    createElement("div", { "data-testid": "mock-reasoning-toggle" }),
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

vi.mock("@/components/ui/Drawer", () => ({
  Drawer: ({ children }: { children: React.ReactNode }) =>
    createElement("div", { "data-testid": "mock-drawer" }, children),
}));

// NOTE: @/components/ChatHeaderMenu is deliberately NOT mocked — the whole
// point of this spec is that the real menu's panel is structurally visible.

// Static import after all mocks are hoisted.
import Chat from "@/pages/Chat";
import { downloadChatAsMarkdown } from "@/lib/chatExport";

function chatRouteTree(initialPath: string) {
  return createElement(
    QueryClientProvider,
    {
      client: new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false },
        },
      }),
    },
    createElement(
      MemoryRouter,
      { initialEntries: [initialPath] },
      createElement(
        Routes,
        null,
        createElement(Route, { path: "/chats/:chatId", element: createElement(Chat) }),
        createElement(Route, {
          path: "/login",
          element: createElement("div", { "data-testid": "login-page" }, "login"),
        }),
      ),
    ),
  );
}

/** Walk ancestors asserting nothing structurally hides the node. */
function expectStructurallyVisible(node: HTMLElement): void {
  expect(node.closest('[aria-hidden="true"]')).toBeNull();
  let el: HTMLElement | null = node;
  while (el !== null) {
    expect(el.style.opacity).not.toBe("0");
    expect(el.style.pointerEvents).not.toBe("none");
    expect(el.style.width).not.toBe("0px");
    expect(el.style.height).not.toBe("0px");
    el = el.parentElement;
  }
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("test_layout_r1_desktop_share_menu (real ChatHeaderMenu in desktop TopBar)", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("Cmd/Ctrl+Shift+E (onExportChat) opens a visible, clickable panel", () => {
    render(chatRouteTree("/chats/1"));

    // The menu mounts closed — no panel yet.
    expect(screen.queryByTestId("chat-header-menu-panel")).toBeNull();

    // Fire the K-04 shortcut handler that Chat.tsx registered.
    act(() => {
      captured.handlers.onExportChat?.();
    });

    // The dropdown panel is now in the DOM and structurally visible —
    // before F1 it rendered inside a 0×0 opacity-0 aria-hidden span.
    const panel = screen.getByTestId("chat-header-menu-panel");
    expectStructurallyVisible(panel);

    // …and actually clickable: the Markdown export item fires the export.
    fireEvent.click(screen.getByTestId("chat-export-markdown"));
    expect(downloadChatAsMarkdown).toHaveBeenCalledTimes(1);
  });

  it("overflow 'Share / Export' opens the same hidden-trigger menu", () => {
    render(chatRouteTree("/chats/1"));

    fireEvent.click(
      within(screen.getByTestId("mock-overflow")).getByRole("button", {
        name: "Share / Export",
      }),
    );

    const panel = screen.getByTestId("chat-header-menu-panel");
    expectStructurallyVisible(panel);
    // Share section reflects the non-incognito chat.
    expect(screen.getByTestId("chat-share-create")).toBeTruthy();
  });
});
