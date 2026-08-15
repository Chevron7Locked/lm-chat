/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Cluster 3b Task 4 closeout — Chat.tsx serverMessages mapping for persisted
 * tool_calls.
 *
 * The original Task 4 landing persisted tool calls live-only (SSE state);
 * the integration step that threads the REFETCHED `m.tool_calls` wire field
 * onto ChatMessageData.toolCalls was lost in a rebase. These tests pin the
 * full FE half of the reload path:
 *
 *   GET /api/chats/{id} → MessageRecord.tool_calls
 *     → serverMessages mapping (Chat.tsx)
 *       → ChatMessageData.toolCalls
 *         → real <ChatMessage> renders <ToolCallCard>s.
 *
 * ChatMessage is deliberately NOT mocked here (unlike test_Chat.spec.tsx) so
 * the assertion reaches the actual ToolCallCard markup.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { MessageRecord } from "@/hooks/useChats";

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

// Mutable so each test can vary the persisted message page.
let mockMessages: MessageRecord[] = [];

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: [], isLoading: false, isError: false }),
  useMessages: () => ({ data: { messages: mockMessages }, refetch: vi.fn() }),
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

// Stores ----------------------------------------------------------------------

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({
    user: { id: 1, username: "test", is_admin: false },
    isInitializing: false,
  }),
}));

vi.mock("@/stores/toastStore", () => {
  // The REAL ChatMessage (not mocked in this spec) reads useToastStore as a
  // zustand selector hook (CopyMessageButton), so both exports are needed.
  const toastState = { push: vi.fn(), dismiss: vi.fn(), toasts: [] };
  const useToastStore = (selector?: (s: typeof toastState) => unknown) =>
    selector ? selector(toastState) : toastState;
  useToastStore.getState = () => toastState;
  return {
    useToast: () => ({ push: vi.fn() }),
    useToastStore,
  };
});

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

// Heavy shell components stubbed — but ChatMessage stays REAL (the point of
// this spec is that ToolCallCards reach the DOM from persisted data).
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

vi.mock("@/components/ABCompareView", () => ({
  ABCompareView: () => createElement("div", { "data-testid": "mock-abcompare" }),
}));

// ThinkingIndicator was removed in the ProcessStream unification; the
// pre-token "thinking" state + tool lines now render inside the real
// ChatMessage → ProcessStream (intentionally NOT mocked here so this test
// exercises the real persisted-tool-call rendering path).

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

function persistedAssistantMessage(
  // Narrower than MessageRecord["tool_calls"] (which includes `undefined`
  // via the optional key) — every call site below passes an array or
  // `null`, never `undefined`, and assigning an explicit `undefined` value
  // into an optional key is what exactOptionalPropertyTypes objects to.
  toolCalls: NonNullable<MessageRecord["tool_calls"]> | null,
): MessageRecord {
  return {
    id: 42,
    chat_id: 1,
    role: "assistant",
    content: "I searched for you.",
    reasoning_content: null,
    stop_reason: "stop",
    tool_calls: toolCalls,
    created_at: "2026-06-10T12:00:00Z",
  };
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — persisted tool_calls thread into ProcessStream tool lines (Cluster 3b Task 4)", () => {
  beforeEach(() => {
    mockMessages = [];
    vi.clearAllMocks();
  });

  it("renders inline tool lines from a refetched message carrying tool_calls", async () => {
    mockMessages = [
      persistedAssistantMessage([
        {
          id: "tc_persisted_1",
          name: "search_web",
          arguments: JSON.stringify({ query: "lm studio" }),
          status: "success",
          result: "LM Studio is a desktop application.",
        },
      ]),
    ];

    const { container } = await renderChat("/chats/1");

    // The real ChatMessage → ProcessStream rendered an inline tool line from
    // the persisted record (quiet <details> line, no longer a card).
    const lines = container.querySelectorAll(".lmchat-process-tool");
    expect(lines.length).toBe(1);
    // formatToolName converts "search_web" → "Search Web".
    expect(screen.getByText(/Search Web/i)).toBeTruthy();
    // Result body is present inside the line (args + results survive reload).
    expect(screen.getByText("LM Studio is a desktop application.")).toBeTruthy();
  });

  it("renders one inline tool line per persisted call, preserving status", async () => {
    mockMessages = [
      persistedAssistantMessage([
        { id: "tc_a", name: "tool_alpha", arguments: "{}", status: "success", result: "ok" },
        { id: "tc_b", name: "tool_beta", arguments: "{}", status: "failure" },
      ]),
    ];

    const { container } = await renderChat("/chats/1");

    const lines = container.querySelectorAll(".lmchat-process-tool");
    expect(lines.length).toBe(2);
    expect(screen.getByText(/Tool Alpha/i)).toBeTruthy();
    expect(screen.getByText(/Tool Beta/i)).toBeTruthy();
    expect(screen.getByText("failure")).toBeTruthy();
  });

  it("renders no tool lines when tool_calls is null (pre-0024 rows)", async () => {
    mockMessages = [persistedAssistantMessage(null)];

    const { container } = await renderChat("/chats/1");

    expect(container.querySelectorAll(".lmchat-process-tool").length).toBe(0);
    // The message itself still renders.
    expect(screen.getByText("I searched for you.")).toBeTruthy();
  });

  it("renders no tool lines when the field is absent (back-compat wire)", async () => {
    const msg = persistedAssistantMessage(null);
    delete (msg as Partial<MessageRecord>).tool_calls;
    mockMessages = [msg];

    const { container } = await renderChat("/chats/1");

    expect(container.querySelectorAll(".lmchat-process-tool").length).toBe(0);
  });
});
