/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests — Chat.tsx handleCompact: rid-clear + honest toast (PLAN §4.2 P0).
 *
 * Exercises the REAL handleCompact closure defined inside Chat.tsx by
 * rendering the real <Chat> component (mirroring test_Chat.spec.tsx's mocking
 * approach) and driving the compact path through the Composer's `onCompact`
 * prop — the same callback the real Composer invokes for the `/compact`
 * slash command and the SlashPalette dispatches to (Chat.tsx `handlePaletteSelect`
 * "compact" case / Composer.tsx "compact" case both ultimately call the same
 * `handleCompact` passed down as `onCompact`).
 *
 * Tests:
 *   - on a successful compact with archived_count > 0: the cached rid key
 *     `lmchat:sse:{chatId}:rid` is removed from localStorage, the compactions
 *     list query is invalidated, and the honest success toast fires with the
 *     exact message.
 *   - on a successful compact with archived_count === 0: the honest "Already
 *     compact" info toast fires instead.
 *
 * Red-on-revert: temporarily removing the `clearResponseId(chatId)` call from
 * Chat.tsx's handleCompact makes the first test's rid-key assertion fail;
 * the rid-clear is asserted directly below.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// jsdom doesn't implement scrollIntoView; Chat's auto-scroll effect crashes
// without this stub.
if (typeof window !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function (): void { /* no-op */ };
}

// ─── Mocks for heavy hooks / network surfaces (mirrors test_Chat.spec.tsx) ───

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

// T-COMPACT-1: mutateAsync is hoisted so each test can control the resolved
// CompactResultResponse independently. This is the ONLY seam the rid-clear
// tests care about — everything else in useChats is a no-op stub identical
// to test_Chat.spec.tsx's baseline mock.
const mockCompactMutateAsync = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: [], isLoading: false, isError: false }),
  useMessages: () => ({ data: { messages: [] }, refetch: vi.fn() }),
  useUpdateChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useDeleteChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useClearChatMessages: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useForkChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useCompactChat: () => ({ mutate: vi.fn(), mutateAsync: mockCompactMutateAsync, isPending: false }),
  useAppendMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useCreateChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useEditMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useRegenerateMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useDeleteMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useGenerateTitle: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  chatKeys: { messages: (id: number) => ["messages", id] },
}));

// T-COMPACT-2: keep the REAL compactionKeys export (so the invalidateQueries
// assertion checks the actual key shape Chat.tsx uses) but stub the network
// hooks — this test doesn't exercise the compaction-tab interleave, only the
// rid-clear + toast + invalidate side effects of a successful compact.
vi.mock("@/hooks/useCompactions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useCompactions")>();
  return {
    ...actual,
    useCompactions: () => ({ data: [] }),
    useCompactionMessages: () => ({ data: null, isLoading: false }),
  };
});

vi.mock("@/hooks/useChatPreset", () => ({
  useChatPreset: () => ({ activePreset: "", preset: null, setPreset: vi.fn(), clearPreset: vi.fn() }),
  useHydrateChatPresets: () => undefined,
  useChatPresetStore: (sel: (s: { overrides: Record<number, string> }) => unknown) =>
    sel({ overrides: {} }),
}));

vi.mock("@/hooks/useMemory", () => ({
  usePinInsight: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}));

vi.mock("@/hooks/useEmbeddingStatus", () => ({
  useEmbeddingStatus: () => ({ data: undefined, isLoading: false, isError: false }),
}));

// Stores ----------------------------------------------------------------------

const mockAuthState = {
  user: { id: 1, username: "test", is_admin: false } as { id: number; username: string; is_admin: boolean } | null,
  isInitializing: false,
};

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => mockAuthState,
}));

const pushMock = vi.hoisted(() => vi.fn());
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: pushMock }),
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

// Heavy components stubbed so the render isn't gated on their internals.
vi.mock("@/components/Sidebar", () => ({
  Sidebar: () => createElement("div", { "data-testid": "mock-sidebar" }, "sidebar"),
}));

// T-COMPACT-3: the ONLY meaningfully different mock vs. test_Chat.spec.tsx.
// Composer is still stubbed (its own slash-command parsing isn't under test
// here — Composer.tsx:559-560 already just calls `onCompact()` verbatim), but
// the stub renders a real button wired to the `onCompact` prop so a click
// invokes the REAL `handleCompact` closure Chat.tsx created and passed down.
vi.mock("@/components/Composer", () => ({
  Composer: (props: { onCompact: () => void }) =>
    createElement(
      "div",
      { "data-testid": "mock-composer" },
      createElement("textarea", { "aria-label": "Message" }),
      createElement("button", { "aria-label": "Send message", type: "button" }, "Send"),
      createElement(
        "button",
        { "aria-label": "Compact chat", type: "button", onClick: () => { props.onCompact(); } },
        "Compact",
      ),
    ),
}));

vi.mock("@/components/ChatMessage", () => ({
  ChatMessage: () => createElement("div", { "data-testid": "mock-chatmessage" }),
}));

vi.mock("@/components/ABCompareView", () => ({
  ABCompareView: () => createElement("div", { "data-testid": "mock-abcompare" }),
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

function chatRouteTree(initialPath: string, queryClient: QueryClient) {
  return createElement(
    QueryClientProvider,
    { client: queryClient },
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

function renderChat(initialPath: string, queryClient: QueryClient) {
  return render(chatRouteTree(initialPath, queryClient));
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat handleCompact (real component — PLAN §4.2 rid-clear)", () => {
  const chatId = 123;
  const ridKey = `lmchat:sse:${chatId}:rid`;

  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("clears the rid, invalidates the compactions list, and shows the honest success toast", async () => {
    localStorage.setItem(ridKey, "old-rid-value");
    expect(localStorage.getItem(ridKey)).toBe("old-rid-value");

    mockCompactMutateAsync.mockResolvedValueOnce({
      archived_count: 3,
      original_token_count: 900,
      summary_token_count: 120,
      remaining_token_count: 200,
      removed_message_ids: [1, 2, 3],
      chat_id: chatId,
      compaction_id: 10,
      summary: "Summary of archived messages",
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderChat(`/chats/${String(chatId)}`, queryClient);

    fireEvent.click(screen.getByLabelText("Compact chat"));

    // The real handleCompact path: mutateAsync -> clearResponseId -> invalidate -> toast.
    await waitFor(() => {
      expect(localStorage.getItem(ridKey)).toBeNull();
    });

    expect(pushMock).toHaveBeenCalledWith({
      variant: "success",
      message: "Compacted 3 messages (~900 → ~120 tokens).",
    });

    const { compactionKeys } = await import("@/hooks/useCompactions");
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: compactionKeys.list(chatId),
    });
  });

  it("shows the 'Already compact' info toast when archived_count is 0", async () => {
    localStorage.setItem(ridKey, "old-rid-value");

    mockCompactMutateAsync.mockResolvedValueOnce({
      archived_count: 0,
      original_token_count: 0,
      summary_token_count: 0,
      remaining_token_count: 0,
      removed_message_ids: [],
      chat_id: chatId,
      compaction_id: null,
      summary: null,
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
    });

    renderChat(`/chats/${String(chatId)}`, queryClient);

    fireEvent.click(screen.getByLabelText("Compact chat"));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith({
        variant: "info",
        message: "Already compact — nothing to trim.",
      });
    });

    // Rid is still cleared unconditionally on any successful compact call,
    // even a no-op one (Chat.tsx handleCompact clears before branching on
    // archived_count).
    expect(localStorage.getItem(ridKey)).toBeNull();
  });
});
