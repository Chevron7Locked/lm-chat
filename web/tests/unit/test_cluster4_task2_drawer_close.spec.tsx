/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Cluster 4 Task 2 — test_mobile_chat_select_closes_drawer
 *
 * Regression test for the chatId-change useEffect in Chat.tsx (lines ~178-183).
 *
 * The spec requirement: "On mobile, fire setSidebarCollapsed(true) in a
 * useEffect on chatId change (only when mobile && mobileDrawerOpen)."
 *
 * This test renders the full Chat component with isMobile=true, opens the
 * drawer via the hamburger button, then navigates in-place to a different
 * chatId using createMemoryRouter + router.navigate() so the chatId prop
 * changes within the same React component instance (triggering the useEffect).
 *
 * It asserts that the drawer closes (sidebar-backdrop disappears) after the
 * navigation — proving the useEffect fired and called setSidebarCollapsed(true).
 *
 * Reverting the useEffect at Chat.tsx:~178-183 leaves this test red because
 * the backdrop remains visible after the chatId change.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// jsdom doesn't implement scrollIntoView.
const elementProto = Element.prototype as { scrollIntoView?: () => void };
if (typeof window !== "undefined" && !elementProto.scrollIntoView) {
  elementProto.scrollIntoView = function (): void { /* no-op */ };
}

// ─── Mocks — same contract as test_Chat.spec.tsx but with isMobile=true ──────

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
    refresh: () => undefined,
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

// Static import after all vi.mock() hoisting completes.
import Chat from "@/pages/Chat";

/** Build a createMemoryRouter-based tree that allows in-place navigation. */
function makeRouterTree(initialPath: string) {
  const router = createMemoryRouter(
    [
      { path: "/chats", element: createElement(Chat) },
      { path: "/chats/:chatId", element: createElement(Chat) },
      { path: "/login", element: createElement("div", { "data-testid": "login-page" }, "login") },
    ],
    { initialEntries: [initialPath] },
  );

  const tree = createElement(
    QueryClientProvider,
    {
      client: new QueryClient({
        defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
      }),
    },
    createElement(RouterProvider, { router }),
  );

  return { router, tree };
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("test_mobile_chat_select_closes_drawer (exercises Chat.tsx useEffect)", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("chatId change on mobile closes the drawer when it was open", async () => {
    // 1. Render Chat at /chats/1 with isMobile=true.
    //    useSidebarCollapsed(true) starts collapsed on mobile — drawer is CLOSED.
    const { router, tree } = makeRouterTree("/chats/1");
    render(tree);

    // 2. Open the drawer by clicking the hamburger button.
    //    topbar-mobile-menu only renders when isMobile && !mobileDrawerOpen.
    const hamburger = screen.getByTestId("topbar-mobile-menu");
    fireEvent.click(hamburger);

    // 3. Drawer is now OPEN — the backdrop scrim is present.
    expect(screen.getByTestId("sidebar-backdrop")).toBeTruthy();

    // 4. Navigate to a different chat (chatId: 1 → 2) within the SAME router
    //    instance so chatId changes as a prop on the already-mounted Chat.
    //    This is what triggers Chat.tsx's useEffect at ~line 178:
    //      if (isMobile && mobileDrawerOpen) setSidebarCollapsed(true)
    await act(async () => {
      await router.navigate("/chats/2");
    });

    // 5. Assert the drawer is now CLOSING — Optimize R1 (F10): the backdrop
    //    stays mounted through the 320ms exit fade (--closing modifier,
    //    pointer-events: none) instead of vanishing instantly, then unmounts
    //    on animationend (or the timeout fallback in jsdom, where animations
    //    never run). If the useEffect in Chat.tsx is reverted,
    //    setSidebarCollapsed(true) is never called: the backdrop stays
    //    mounted WITHOUT the --closing modifier and never unmounts, failing
    //    both assertions below.
    expect(
      screen
        .getByTestId("sidebar-backdrop")
        .classList.contains("lmchat-mobile-backdrop--closing"),
    ).toBe(true);
    await waitFor(() => {
      expect(screen.queryByTestId("sidebar-backdrop")).toBeNull();
    });
  });

  it("chatId change on mobile is a no-op when drawer is already closed", async () => {
    // Regression guard: changing chatId when the drawer is already closed
    // should not cause any error.
    const { router, tree } = makeRouterTree("/chats/1");
    render(tree);

    // Drawer starts closed — backdrop is absent.
    expect(screen.queryByTestId("sidebar-backdrop")).toBeNull();

    // Navigate to another chat — drawer stays closed, no error thrown.
    await act(async () => {
      await router.navigate("/chats/3");
    });
    expect(screen.queryByTestId("sidebar-backdrop")).toBeNull();
  });
});
