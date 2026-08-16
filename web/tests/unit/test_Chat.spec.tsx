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
import type { ChatSummary, MessageRecord, RegenerateResult } from "@/hooks/useChats";

// jsdom doesn't implement scrollIntoView; Chat's auto-scroll effect crashes
// without this stub.
if (typeof window !== "undefined" && typeof Element.prototype.scrollIntoView !== "function") {
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
  chatId: number | null;
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
  // Matches the /chats/1 target every test in this describe block renders
  // — see StreamState.chatId / useMtpSuspectedDedupe's cross-chat guard.
  chatId: 1,
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
    refresh: () => undefined,
  }),
}));

// Resend-disappears regression (see "Chat (resend disappears bug)" below):
// mutable so a single test can seed a real chat + message and drive the
// regenerate mutation's onSuccess directly, instead of the static empty
// stubs every other describe block in this file relies on.
const mockChatsData = vi.hoisted(() => ({ data: [] as ChatSummary[] }));
const mockMessagesData = vi.hoisted(() => ({
  data: { messages: [] as MessageRecord[] },
}));
const mockRegenerateMutate = vi.hoisted(() => vi.fn());
// Stable reference across renders — a fresh vi.fn() every useMessages()
// call would make the stream-complete effect's dep array (`refetchMessages`
// is one of its deps) see a "changed" function on every render and re-fire
// forever once `.then()` schedules a state update. The real useQuery
// `refetch` is stable per query; this mirrors that.
const mockRefetchMessages = vi.hoisted(() => vi.fn(() => Promise.resolve()));

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: mockChatsData.data, isLoading: false, isError: false }),
  // refetch must return a thenable — the stream-complete effect in
  // Chat.tsx chains `.then()` off it to clear the resend/regenerate
  // optimistic bubble (see "Chat (resend disappears bug)" below).
  useMessages: () => ({ data: mockMessagesData.data, refetch: mockRefetchMessages }),
  useUpdateChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useDeleteChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useClearChatMessages: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useForkChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useCompactChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useAppendMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useCreateChat: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useEditMessage: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useRegenerateMessage: () => ({ mutate: mockRegenerateMutate, mutateAsync: vi.fn(), isPending: false }),
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
  isLoading: false as const,
  isError: false as const,
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
  // submitTurn (Chat.tsx) calls this directly when no explicit integrations
  // are passed — the regenerate/resend/edit path. No test here exercises
  // stored per-chat integrations, so a stubbed "none configured" is enough.
  resolveChatIntegrationsField: () => undefined,
}));

// Resend-disappears regression needs the real message content/role and the
// onResend wiring to reach the DOM — every other describe block only checks
// for the mock's presence, so widening the mock stays backward compatible.
vi.mock("@/components/ChatMessage", () => ({
  ChatMessage: (props: {
    message: { id: number | string; role: string; content: string };
    onResend?: (messageId: number) => void;
  }) =>
    createElement(
      "div",
      {
        "data-testid": "mock-chatmessage",
        "data-role": props.message.role,
        "data-content": props.message.content,
      },
      props.message.content,
      props.onResend !== undefined && typeof props.message.id === "number"
        ? createElement(
            "button",
            {
              type: "button",
              "data-testid": `mock-resend-${String(props.message.id)}`,
              onClick: () => {
                props.onResend?.(props.message.id as number);
              },
            },
            "resend",
          )
        : null,
    ),
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

  it("renders the welcome state when no chat is selected", () => {
    renderChat("/chats");
    // Top-bar / sidebar shell render without crashing.
    expect(screen.getByTestId("mock-sidebar")).toBeTruthy();
    // EmptyState surfaces the empty-canvas marker.
    expect(screen.getByTestId("chat-empty-state")).toBeTruthy();
  });

  it("renders the shell with composer when a chat id is selected", () => {
    renderChat("/chats/1");
    expect(screen.getByTestId("mock-sidebar")).toBeTruthy();
    expect(screen.getByTestId("mock-composer")).toBeTruthy();
    // Composer's textarea + send button reach the DOM.
    expect(screen.getByLabelText("Message")).toBeTruthy();
    expect(screen.getByLabelText("Send message")).toBeTruthy();
  });

  it("redirects to /login when no user is authenticated", () => {
    mockAuthState.user = null;
    renderChat("/chats/1");
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

  it("renders the banner on the first mtp_suspected error and suppresses it on the second for the same chat", () => {
    // First render: idle — no banner.
    const { rerender } = renderChat("/chats/1");
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

  it("renders the banner for a non-mtp error code regardless of prior mtp dedupe state", () => {
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
    const { rerender } = renderChat("/chats/1");
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

  it("pushes one toast per new SSE warning and never double-toasts", () => {
    const { rerender } = renderChat("/chats/1");
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

  it("toggles the is-focus-mode shell class via the TopBar toggle, then exits via the affordance", () => {
    renderChat("/chats/1");
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

describe("Chat (resend disappears bug)", () => {
  // Regression for: "when i resend a message, it disappears while the
  // model works". Root cause: submitTurn() — the shared turn-dispatch
  // primitive behind regenerate/resend/edit — never set `pendingUser`
  // (Chat.tsx ~926), unlike handleSubmit's normal send path (~842). Both
  // delete_from_user_message_for_resend and delete_assistant_turn_for_
  // regenerate delete the boundary row itself server-side (at least 1 row,
  // see message_service.py's delete_from_user_message_for_resend
  // docstring), so once the messages refetch drops it there was nothing —
  // no persisted row, no optimistic bubble — to render it until the
  // replayed turn's new row landed at stream-complete.
  beforeEach(() => {
    mockAuthState.user = { id: 1, username: "test", is_admin: false };
    mockAuthState.isInitializing = false;
    mockSSEState = { ..._idleSSEState };
    mockChatsData.data = [
      {
        id: 1,
        title: "test chat",
        folder: null,
        pinned: false,
        updated_at: new Date(0).toISOString(),
        model_id: "test-model",
        display_order: 0,
        tags: [],
        archived_at: null,
      },
    ];
    mockMessagesData.data = {
      messages: [
        {
          id: 10,
          chat_id: 1,
          role: "user",
          content: "hello",
          reasoning_content: null,
          created_at: new Date(0).toISOString(),
        },
      ],
    };
    vi.clearAllMocks();
    mockRegenerateMutate.mockReset();
  });

  it("keeps the resent message visible via the optimistic bubble while the replayed turn streams", () => {
    const { rerender } = renderChat("/chats/1");

    // Starting state: the persisted user message with a resend affordance.
    expect(screen.getByTestId("mock-resend-10")).toBeTruthy();
    expect(
      document.querySelector('[data-role="user"][data-content="hello"]'),
    ).not.toBeNull();

    // Drive the regenerate mutation the way the real
    // onSuccess -> submitTurn chain does (useMessageActions.ts
    // handleRegenerateClick): the backend deleted the boundary row and
    // handed back its content to replay as a fresh turn.
    mockRegenerateMutate.mockImplementation(
      (
        _vars: { messageId: number; confirm: boolean },
        opts: { onSuccess?: (result: RegenerateResult) => void },
      ) => {
        opts.onSuccess?.({ deleted: 1, chat_id: 1, prior_user_content: "hello" });
      },
    );
    fireEvent.click(screen.getByTestId("mock-resend-10"));

    // Simulate what lands next in production: the messages refetch reflects
    // the delete (row gone), and the replayed turn is now streaming — "while
    // the model works".
    mockMessagesData.data = { messages: [] };
    mockSSEState = { ..._idleSSEState, status: "streaming", chatId: 1 };
    rerender(chatRouteTree("/chats/1"));

    // The resent text must still be on screen for the whole generation, not
    // just before/after it.
    expect(
      document.querySelector('[data-role="user"][data-content="hello"]'),
    ).not.toBeNull();
  });

  it("clears the optimistic bubble once the replayed turn's refetch lands, without a stuck duplicate", async () => {
    const { rerender } = renderChat("/chats/1");

    // A plain Regenerate (or a Resend with a reply after it) deletes >= 2
    // rows — the boundary row(s) ITSELF, not just what follows (see
    // delete_assistant_turn_for_regenerate / delete_from_user_message_for_
    // resend) — while the replay always adds back exactly 2 (user +
    // assistant). A length-based "count > baseline" auto-clear can never
    // fire here (final count == starting count) — this is what the
    // stream-complete effect's explicit clear exists to handle.
    mockRegenerateMutate.mockImplementation(
      (
        _vars: { messageId: number; confirm: boolean },
        opts: { onSuccess?: (result: RegenerateResult) => void },
      ) => {
        opts.onSuccess?.({ deleted: 2, chat_id: 1, prior_user_content: "hello" });
      },
    );
    fireEvent.click(screen.getByTestId("mock-resend-10"));

    mockMessagesData.data = { messages: [] };
    mockSSEState = { ..._idleSSEState, status: "streaming", chatId: 1 };
    rerender(chatRouteTree("/chats/1"));
    expect(
      document.querySelector('[data-role="user"][data-content="hello"]'),
    ).not.toBeNull();

    // The replayed turn's refetch resolves with the new persisted rows.
    mockMessagesData.data = {
      messages: [
        {
          id: 20,
          chat_id: 1,
          role: "user",
          content: "hello",
          reasoning_content: null,
          created_at: new Date(0).toISOString(),
        },
        {
          id: 21,
          chat_id: 1,
          role: "assistant",
          content: "hi",
          reasoning_content: null,
          created_at: new Date(0).toISOString(),
        },
      ],
    };
    mockSSEState = {
      ..._idleSSEState,
      status: "complete",
      chatId: 1,
      contentDeltas: ["hi"],
    };
    rerender(chatRouteTree("/chats/1"));

    await waitFor(() => {
      expect(
        document.querySelectorAll('[data-role="user"][data-content="hello"]'),
      ).toHaveLength(1);
    });
  });
});
