/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Smoke render test for Chat.tsx (2605 LOC, previously untested).
 *
 * The goal is a first foothold of coverage — Chat.tsx renders without
 * crashing under three baseline conditions:
 *   - no chat selected → welcome / empty state surfaces.
 *   - a chat selected → top-level shell (top bar + composer) renders.
 *   - unauthenticated user → redirected to /login.
 *
 * Behaviour tests for previously-fixed sub-session bugs land alongside
 * the bug fixes (PR-D); this test is intentionally scoped to render
 * smoke + auth-gate.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

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

// useSSE mock: mutable state so tests can flip the SSE status/error between
// renders to exercise stateful behavior (e.g. MTP-suspected dedupe).
type _SSEState = {
  status: "idle" | "streaming" | "complete" | "error";
  messageId: number | null;
  responseId: string | null;
  contentDeltas: string[];
  reasoningDeltas: string[];
  toolCalls: unknown[];
  error: { code: string; message: string; cumulative_tool_rounds?: number; hint?: string } | null;
  stats: { tokensPerSecond: number | null; ttftSeconds: number | null; outputTokens: number };
  loadPhase: null;
  warnings: { code: string; message: string }[];
};

const _idleSSEState: _SSEState = {
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
};

let mockSSEState: _SSEState = { ..._idleSSEState };

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
  useChatPresetStore: (sel: (s: { overrides: Record<number, string> }) => unknown) =>
    sel({ overrides: {} }),
}));

vi.mock("@/hooks/useMemory", () => ({
  usePinInsight: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}));

// Bug 3: useEmbeddingStatus is now called in Chat.tsx to derive the resolved
// RAG-enabled smart default. Mutable so individual tests can override it.
const mockEmbeddingStatus = vi.hoisted(() => ({
  data: undefined as
    | {
        active_model_id: string | null;
        loaded_embedding_models: string[];
        total_indexed_messages: number;
        last_indexed_at: number | null;
        models_in_use: Record<string, number>;
        embedding_status: "ok" | "no_embedding_model" | "pinned_model_unavailable";
      }
    | undefined,
  isLoading: false as false,
  isError: false as false,
}));

vi.mock("@/hooks/useEmbeddingStatus", () => ({
  useEmbeddingStatus: () => mockEmbeddingStatus,
}));

// Stores ----------------------------------------------------------------------

const mockAuthState = {
  user: { id: 1, username: "test", is_admin: false } as { id: number; username: string; is_admin: boolean } | null,
  isInitializing: false,
};

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => mockAuthState,
}));

// T0-1: hoisted so the warning-toast tests can assert on push calls.
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

// Heavy components stubbed so a smoke render isn't gated on their internals.
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

// ThinkingIndicator was removed in the ProcessStream unification (the
// pre-token "thinking" state now lives inside the real ChatMessage →
// ProcessStream). No mock needed — ProcessStream renders nothing for these
// non-streaming-tool-call cases.

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

vi.mock("@/components/PinNavStrip", () => ({
  PinNavStrip: () => createElement("div", { "data-testid": "mock-pinnav" }),
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

// vi.mock calls above are hoisted by Vitest, so this static import resolves
// AFTER all mocks register. Previously this was a dynamic `await import` inside
// renderChat — but that pattern reintroduces module-cache fragility across the
// dedupe tests (which rely on a single Chat instance retaining its ref state
// across rerender calls). Static import keeps the same Chat module reference
// for the lifetime of the test file.
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

function renderChat(initialPath: string) {
  return render(chatRouteTree(initialPath));
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat (smoke render)", () => {
  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    mockSSEState = { ..._idleSSEState };
    vi.clearAllMocks();
  });

  it("renders the welcome state when no chat is selected", async () => {
    await renderChat("/chats");
    // Top-bar / sidebar shell render without crashing.
    expect(screen.getByTestId("mock-sidebar")).toBeTruthy();
    // EmptyState surfaces the empty-canvas marker.
    expect(screen.getByTestId("chat-empty-state")).toBeTruthy();
  });

  it("renders the shell with composer when a chat id is selected", async () => {
    await renderChat("/chats/1");
    expect(screen.getByTestId("mock-sidebar")).toBeTruthy();
    expect(screen.getByTestId("mock-composer")).toBeTruthy();
    // Composer's textarea + send button reach the DOM.
    expect(screen.getByLabelText("Message")).toBeTruthy();
    expect(screen.getByLabelText("Send message")).toBeTruthy();
  });

  it("redirects to /login when no user is authenticated", async () => {
    mockAuthState.user = null;
    await renderChat("/chats/1");
    expect(screen.getByTestId("login-page")).toBeTruthy();
  });
});

describe("Chat (mtp_suspected dedupe)", () => {
  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    mockSSEState = { ..._idleSSEState };
    vi.clearAllMocks();
  });

  it("renders the banner on the first mtp_suspected error and suppresses it on the second for the same chat", async () => {
    // First render: idle — no banner.
    const { rerender } = await renderChat("/chats/1");
    expect(screen.queryByTestId("chat-stream-error")).toBeNull();

    // Flip SSE state to a fresh mtp_suspected error for chat 1.
    mockSSEState = {
      ..._idleSSEState,
      status: "error",
      error: {
        code: "mtp_suspected",
        message: "Long tool chain — possible MTP misbehavior.",
        cumulative_tool_rounds: 20,
        hint: "Try disabling MTP in LM Studio's model load config.",
      },
    };
    // Force a re-render with the new state.
    rerender(chatRouteTree("/chats/1"));
    // First mtp_suspected: banner appears (dedupe Set was empty when this render computed).
    expect(screen.queryByTestId("chat-stream-error")).not.toBeNull();

    // Flip back to idle so the effect can run a no-op cycle; the dedupe Set
    // now contains chatId=1 from the previous render's useEffect-commit
    // (the dedupe-set-add lives in a useEffect that runs after each render
    // where status === "error" and code === "mtp_suspected").
    mockSSEState = { ..._idleSSEState };
    rerender(chatRouteTree("/chats/1"));
    expect(screen.queryByTestId("chat-stream-error")).toBeNull();

    // Flip to mtp_suspected error again for the SAME chat — must be suppressed.
    mockSSEState = {
      ..._idleSSEState,
      status: "error",
      error: {
        code: "mtp_suspected",
        message: "Long tool chain — possible MTP misbehavior.",
        cumulative_tool_rounds: 25,
        hint: "Try disabling MTP in LM Studio's model load config.",
      },
    };
    rerender(chatRouteTree("/chats/1"));
    // Dedupe: banner suppressed on the second mtp_suspected for the same chat.
    expect(screen.queryByTestId("chat-stream-error")).toBeNull();
  });

  it("renders the banner for a non-mtp error code regardless of prior mtp dedupe state", async () => {
    // First, fire mtp_suspected to populate the dedupe Set.
    mockSSEState = {
      ..._idleSSEState,
      status: "error",
      error: {
        code: "mtp_suspected",
        message: "Long tool chain.",
        cumulative_tool_rounds: 20,
      },
    };
    const { rerender } = await renderChat("/chats/1");
    expect(screen.queryByTestId("chat-stream-error")).not.toBeNull();

    // Now fire a DIFFERENT error code — must NOT be dedupe'd.
    mockSSEState = {
      ..._idleSSEState,
      status: "error",
      error: { code: "context_window_exceeded", message: "Too much context." },
    };
    rerender(chatRouteTree("/chats/1"));
    expect(screen.queryByTestId("chat-stream-error")).not.toBeNull();
  });
});
describe("Chat (warning toasts — T0-1)", () => {
  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    mockSSEState = { ..._idleSSEState };
    vi.clearAllMocks();
  });

  function warningCalls() {
    return pushMock.mock.calls.filter(
      (c) => (c[0] as { variant?: string } | undefined)?.variant === "warning",
    ) as [{ variant: string; message: string }][];
  }

  it("pushes one toast per new SSE warning and never double-toasts", async () => {
    const { rerender } = await renderChat("/chats/1");
    expect(warningCalls()).toHaveLength(0);

    // A warning frame arrives (budget gate trimmed integrations).
    mockSSEState = {
      ..._idleSSEState,
      status: "streaming",
      warnings: [
        {
          code: "integrations_trimmed_for_context",
          message: "This model's context only fits 2 of 5 tools.",
        },
      ],
    };
    rerender(chatRouteTree("/chats/1"));
    expect(warningCalls()).toHaveLength(1);
    expect(warningCalls()[0]?.[0]?.message).toBe(
      "This model's context only fits 2 of 5 tools.",
    );

    // Unrelated re-render with the SAME warnings array — no double toast.
    rerender(chatRouteTree("/chats/1"));
    expect(warningCalls()).toHaveLength(1);

    // A second warning arrives — exactly one more toast, for the new entry.
    mockSSEState = {
      ...mockSSEState,
      warnings: [
        ...mockSSEState.warnings,
        { code: "other_warning", message: "Second warning." },
      ],
    };
    rerender(chatRouteTree("/chats/1"));
    expect(warningCalls()).toHaveLength(2);
    expect(warningCalls()[1]?.[0]?.message).toBe("Second warning.");
  });
});

describe("Chat (focus mode)", () => {
  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    mockSSEState = { ..._idleSSEState };
    vi.clearAllMocks();
  });

  it("toggles the is-focus-mode shell class via the TopBar toggle, then exits via the affordance", async () => {
    await renderChat("/chats/1");
    const shell = document.querySelector(".lmchat-chat-shell");
    expect(shell).not.toBeNull();
    // Off = today's layout: no focus class.
    expect(shell?.classList.contains("is-focus-mode")).toBe(false);
    // The always-reachable exit affordance is absent until focus mode is on.
    expect(screen.queryByTestId("focus-mode-exit")).toBeNull();

    // Enter focus mode via the TopBar toggle button.
    fireEvent.click(screen.getByTestId("topbar-focus-toggle"));
    expect(shell?.classList.contains("is-focus-mode")).toBe(true);

    // The slim exit affordance is now visible; clicking it leaves focus mode
    // and the shell returns to exactly the base classes.
    fireEvent.click(screen.getByTestId("focus-mode-exit"));
    expect(shell?.classList.contains("is-focus-mode")).toBe(false);
    expect(screen.queryByTestId("focus-mode-exit")).toBeNull();
  });
});

describe("Chat (Bug 3 — RAG smart-default)", () => {
  // Tests for resolved RAG enabled state.
  // The server defaults RAG to ON when an embedder is loaded and the chat
  // has no explicit rag_enabled setting. The FE toggle must reflect this.

  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    mockSSEState = { ..._idleSSEState };
    mockEmbeddingStatus.data = undefined;
    vi.clearAllMocks();
  });

  it("shows RAG as enabled when embedder is loaded and rag_enabled is unset (null)", async () => {
    // useChatsDirect returns data:[] so currentChat is undefined, meaning
    // rag_enabled is null (unset). With embedding_status="ok" the smart-
    // default must flip resolvedRagEnabled to true — the RAG button aria label
    // should read "Disable RAG for this chat" (meaning it currently shows ON).
    mockEmbeddingStatus.data = {
      active_model_id: "bge-m3",
      loaded_embedding_models: ["bge-m3"],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };

    render(chatRouteTree("/chats/1"));

    await waitFor(
      () => {
        const ragBtn = document.querySelector(
          '[aria-label="Disable RAG for this chat"]',
        );
        expect(ragBtn).not.toBeNull();
      },
      { timeout: 500 },
    );
  });

  it("shows RAG as disabled when no embedder is loaded and rag_enabled is unset", async () => {
    // No embedder → smart default is OFF.
    mockEmbeddingStatus.data = {
      active_model_id: null,
      loaded_embedding_models: [],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "no_embedding_model",
    };

    render(chatRouteTree("/chats/1"));

    await waitFor(
      () => {
        const ragBtn = document.querySelector(
          '[aria-label="Enable RAG for this chat"]',
        );
        expect(ragBtn).not.toBeNull();
      },
      { timeout: 500 },
    );
  });
});
