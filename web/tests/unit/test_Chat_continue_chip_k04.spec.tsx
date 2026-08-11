/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Continue-chip + K-04 closeout wiring tests (audit 2026-06-10).
 *
 * The Continue chip had been "addressed" in prior cluster rounds but the
 * Chat.tsx threading layer never passed `showContinue` to ChatMessage for
 * EITHER message source, and the desktop Cmd/Ctrl+Shift+E shortcut ticked
 * a signal nothing read. These tests pin the wiring:
 *
 * 1. streamingMessages threads `showContinue: sseState.showContinue`.
 * 2. serverMessages threads `showContinue: m.stop_reason === "length"`
 *    (the persisted source — the chip survives the post-stream refetch).
 * 3. Desktop K-04: ticking exportMenuSignal reaches the desktop
 *    ChatHeaderMenu's openSignal (previously only desktopShareSignal did).
 *
 * Scaffold mirrors test_Chat.spec.tsx (heavy hooks/components mocked);
 * ChatMessage + ChatHeaderMenu stubs surface the props under test.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ChatMessageData } from "@/components/ChatMessage";

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

vi.mock("@/hooks/useDocumentTitle", () => ({
  useDocumentTitle: () => undefined,
}));

vi.mock("@/hooks/useFocusTrap", () => ({
  useFocusTrap: () => undefined,
}));

vi.mock("@/hooks/usePresence", () => ({
  usePresence: () => ({
    notifyComposerFocused: vi.fn(),
    notifyComposerBlurred: vi.fn(),
    notifyTyping: vi.fn(),
  }),
}));

vi.mock("@/hooks/useMouseParallax", () => ({
  useMouseParallax: () => undefined,
}));

// K-04 wiring: capture the shortcut handlers Chat registers so the test can
// invoke onExportChat() exactly as the Cmd/Ctrl+Shift+E keydown path would.
type ShortcutBag = Record<string, (() => void) | undefined>;
let capturedShortcuts: ShortcutBag = {};
vi.mock("@/hooks/useKeyboardShortcuts", () => ({
  useKeyboardShortcuts: (handlers: ShortcutBag) => {
    capturedShortcuts = handlers;
  },
}));

interface MockSSEState {
  status: "idle" | "streaming" | "complete" | "error" | "stopped";
  messageId: number | null;
  responseId: string | null;
  contentDeltas: string[];
  reasoningDeltas: string[];
  toolCalls: unknown[];
  error: { code: string; message: string } | null;
  stats: { tokensPerSecond: number | null; ttftSeconds: number | null; outputTokens: number };
  loadPhase: null;
  truncated_without_terminal: boolean;
  stop_reason: string | null;
  showContinue: boolean;
  warnings: { code: string; message: string }[];
}

const idleSSEState: MockSSEState = {
  status: "idle",
  messageId: null,
  responseId: null,
  contentDeltas: [],
  reasoningDeltas: [],
  toolCalls: [],
  error: null,
  stats: { tokensPerSecond: null, ttftSeconds: null, outputTokens: 0 },
  loadPhase: null,
  truncated_without_terminal: false,
  stop_reason: null,
  showContinue: false,
  warnings: [],
};

let mockSSEState: MockSSEState = { ...idleSSEState };

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    state: mockSSEState,
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

// Persisted-message source — mutable so tests control stop_reason rows.
interface MockMessageRecord {
  id: number;
  chat_id: number;
  role: string;
  content: string;
  reasoning_content: string | null;
  stop_reason?: string | null;
  created_at: string;
}
let mockMessages: MockMessageRecord[] = [];

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
  useChatPresetStore: (sel: (s: { overrides: Record<number, string> }) => unknown) =>
    sel({ overrides: {} }),
}));

vi.mock("@/hooks/useMemory", () => ({
  usePinInsight: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}));

// Stores ----------------------------------------------------------------------

const mockAuthState = {
  user: { id: 1, username: "test", is_admin: false } as { id: number; username: string; is_admin: boolean } | null,
  isInitializing: false,
};

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => mockAuthState,
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

// Components ------------------------------------------------------------------

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

// ChatMessage stub: surfaces the showContinue prop the threading layer passes,
// so the test pins Chat.tsx's wiring (chip rendering itself is covered by
// test_ChatMessage_cluster3b.spec.tsx against the real component).
vi.mock("@/components/ChatMessage", () => ({
  ChatMessage: ({ message }: { message: ChatMessageData }) =>
    createElement("div", {
      "data-testid": `mock-chatmessage-${String(message.id)}`,
      "data-role": message.role,
      "data-show-continue": message.showContinue === true ? "1" : "0",
    }),
}));

vi.mock("@/components/ABCompareView", () => ({
  ABCompareView: () => createElement("div", { "data-testid": "mock-abcompare" }),
}));

// ThinkingIndicator was removed in the ProcessStream unification (the
// pre-token "thinking" state now lives inside the real ChatMessage →
// ProcessStream). No mock needed for the continue-chip cases.

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

// ChatHeaderMenu stub: surfaces openSignal so the K-04 test can assert the
// desktop branch's combined desktopShareSignal + exportMenuSignal reaches it.
vi.mock("@/components/ChatHeaderMenu", () => ({
  ChatHeaderMenu: ({ openSignal }: { openSignal: number }) =>
    createElement("div", {
      "data-testid": "mock-chatheadermenu",
      "data-open-signal": String(openSignal),
    }),
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
          createElement(Route, { path: "/chats", element: createElement(Chat) }),
          createElement(Route, { path: "/chats/:chatId", element: createElement(Chat) }),
          createElement(Route, { path: "/login", element: createElement("div", null, "login") }),
        ),
      ),
    ),
  );
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — Continue chip threading (F2 closeout)", () => {
  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    mockSSEState = { ...idleSSEState };
    mockMessages = [];
    capturedShortcuts = {};
    vi.clearAllMocks();
  });

  it("threads showContinue from sseState onto the streaming bubble", () => {
    mockSSEState = {
      ...idleSSEState,
      status: "streaming",
      messageId: 99,
      contentDeltas: ["partial reply"],
      stop_reason: "length",
      showContinue: true,
    };
    renderChat("/chats/1");

    const bubble = screen.getByTestId("mock-chatmessage-99");
    expect(bubble.getAttribute("data-show-continue")).toBe("1");
  });

  it("does NOT raise showContinue on the streaming bubble for a normal stream", () => {
    mockSSEState = {
      ...idleSSEState,
      status: "streaming",
      messageId: 99,
      contentDeltas: ["partial reply"],
    };
    renderChat("/chats/1");

    const bubble = screen.getByTestId("mock-chatmessage-99");
    expect(bubble.getAttribute("data-show-continue")).toBe("0");
  });

  it("threads showContinue onto persisted rows with stop_reason='length' (survives refetch)", () => {
    mockMessages = [
      {
        id: 11,
        chat_id: 1,
        role: "user",
        content: "long question",
        reasoning_content: null,
        stop_reason: null,
        created_at: "2026-06-10T00:00:00Z",
      },
      {
        id: 12,
        chat_id: 1,
        role: "assistant",
        content: "truncated answer…",
        reasoning_content: null,
        stop_reason: "length",
        created_at: "2026-06-10T00:00:01Z",
      },
    ];
    renderChat("/chats/1");

    expect(
      screen.getByTestId("mock-chatmessage-12").getAttribute("data-show-continue"),
    ).toBe("1");
    expect(
      screen.getByTestId("mock-chatmessage-11").getAttribute("data-show-continue"),
    ).toBe("0");
  });

  it("leaves showContinue off for persisted rows with stop_reason='stop' or null", () => {
    mockMessages = [
      {
        id: 21,
        chat_id: 1,
        role: "assistant",
        content: "complete answer",
        reasoning_content: null,
        stop_reason: "stop",
        created_at: "2026-06-10T00:00:00Z",
      },
      {
        id: 22,
        chat_id: 1,
        role: "assistant",
        content: "pre-migration answer",
        reasoning_content: null,
        created_at: "2026-06-10T00:00:01Z",
      },
    ];
    renderChat("/chats/1");

    expect(
      screen.getByTestId("mock-chatmessage-21").getAttribute("data-show-continue"),
    ).toBe("0");
    expect(
      screen.getByTestId("mock-chatmessage-22").getAttribute("data-show-continue"),
    ).toBe("0");
  });
});

describe("Chat — desktop K-04 export shortcut wiring (F5 closeout)", () => {
  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    mockSSEState = { ...idleSSEState };
    mockMessages = [];
    capturedShortcuts = {};
    vi.clearAllMocks();
  });

  it("ticking exportMenuSignal (Cmd/Ctrl+Shift+E) reaches the desktop ChatHeaderMenu openSignal", () => {
    renderChat("/chats/1");

    const menu = screen.getByTestId("mock-chatheadermenu");
    const before = Number(menu.getAttribute("data-open-signal"));

    const onExportChat = capturedShortcuts["onExportChat"];
    expect(onExportChat).toBeTypeOf("function");

    act(() => {
      onExportChat?.();
    });

    const after = Number(
      screen.getByTestId("mock-chatheadermenu").getAttribute("data-open-signal"),
    );
    // ChatHeaderMenu opens on ANY change vs its ref — the K-04 tick must
    // change the combined signal. Before this fix the desktop branch read
    // only desktopShareSignal and the shortcut was silently dead.
    expect(after).toBe(before + 1);
  });

  it("onExportChat is a no-op without crashing when no chat is selected", () => {
    renderChat("/chats");

    const onExportChat = capturedShortcuts["onExportChat"];
    expect(onExportChat).toBeTypeOf("function");
    expect(() => {
      act(() => {
        onExportChat?.();
      });
    }).not.toThrow();
  });
});
