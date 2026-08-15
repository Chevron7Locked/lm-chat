/* SPDX-License-Identifier: Apache-2.0 */
/**
 * CHARACTERIZATION tests for Chat.tsx's message-merge derivations and the
 * `pendingUser` optimistic-bubble lifecycle. These tests PIN CURRENT BEHAVIOR
 * (no production changes) so a later extraction of this logic out of Chat.tsx
 * is provably behavior-preserving.
 *
 * Derivations under test (current line numbers, Chat.tsx):
 *   - serverMessages            ~1150
 *   - activeServerMessages      ~1174
 *   - optimisticUserMessages    ~1180
 *   - streamActive              ~1197
 *   - displayStreamContent      ~1258
 *   - streamingKey              ~1270
 *   - persistedHasSameKey       ~1279
 *   - streamingMessages         ~1282
 *   - allMessages               ~1314
 *   - pendingUser state         ~299
 *   - drop-on-growth effect     ~476 (if (count > pendingUser.baseline) setPendingUser(null))
 *   - wipe-on-chat-switch       ~481
 *   - set-on-submit             ~688 (handleSubmit)
 *
 * IMPORTANT FINDING (read before touching this file): `allMessages` itself is
 * NOT what gets rendered as message rows. The JSX render loop (Chat.tsx
 * ~1558-1617) walks `activeServerMessages` (interleaved with compaction tabs)
 * and separately appends `[...optimisticUserMessages, ...streamingMessages]`
 * after it. `allMessages` is only consulted for a `.length === 0` empty-state
 * check (~1544). All assertions below are against the RENDERED DOM (real
 * <ChatMessage> rows), not the `allMessages` array directly, since that's
 * what the app's users (and PinNavStrip/PinnedMessagesPanel, which use the
 * separate `allMessagesForPins` derivation) actually see.
 *
 * Harness: merges test_Chat_toolcalls_persisted.spec.tsx's pattern (real
 * ChatMessage, mutable `mockMessages`) with test_Chat.spec.tsx's mutable
 * `mockSSEState` pattern, plus a Composer stub with a real textarea + Send
 * button (mirrors test_Chat_compact_rid_clear.spec.tsx's "stub renders a
 * real control wired to the real prop" approach) so pendingUser can be
 * driven through the REAL handleSubmit closure.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import type { StreamState } from "@/hooks/useSSE";
import { createElement, useState, useEffect } from "react";
import type { ChangeEvent } from "react";
import { MemoryRouter, Routes, Route, useNavigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { MessageRecord } from "@/hooks/useChats";
import type { ChatStreamPayload } from "@/hooks/useSSE";

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

// ─── Mutable SSE state — module-level so tests can flip status/content
//     between renders (mirrors test_Chat.spec.tsx / test_Chat_continue_chip
//     _k04.spec.tsx's pattern). Includes the FULL StreamState shape (unlike
//     test_Chat.spec.tsx's slimmer type) since several scenarios below need
//     showContinue / stop_reason / followups.
// Real StreamState import (added near the top) — not a hand-rolled shadow.
// This local mirror was already kept closer to date than its siblings (see
// the comment above) but still missed `memorySaved` and `modeAdopt` (C3
// role-adoption, shipped 08-14) — invisible until now because a vi.mock
// factory's return value isn't checked against the real hook's type.
const idleSSEState: StreamState = {
  status: "idle",
  // Matches the /chats/1 target every test in this file renders — see
  // StreamState.chatId.
  chatId: 1,
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
  followups: [],
  memorySaved: undefined,
  modeAdopt: undefined,
};

let mockSSEState: StreamState = { ...idleSSEState };

// Stable function identities across renders (mirrors real react-query /
// useCallback-memoized hook returns). A FRESH `vi.fn()` on every call would
// change `startStream`/`refetchMessages` etc.'s reference on every render,
// which re-triggers any effect that lists them as a dependency — including
// the stream-complete effect (~404-438) that unconditionally calls
// `setFollowupSuggestions(followups)` with a brand-new array literal each
// time. A new array is never referentially equal to the previous state, so
// React never bails out — that combination is an infinite render loop (OOM
// observed empirically before this fix; not a Chat.tsx bug, a mock-fidelity
// bug: real react-query hook return values ARE stable across re-renders when
// the underlying query data hasn't changed).
const mockStartStream = vi.fn();
const mockStopStream = vi.fn();
const mockResetStream = vi.fn();

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    state: mockSSEState,
    start: mockStartStream,
    stop: mockStopStream,
    reset: mockResetStream,
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

// ─── Mutable persisted-message source ────────────────────────────────────────
let mockMessages: MessageRecord[] = [];

// Stable `refetch` identity (see the useSSE comment above for why) + a
// `data` wrapper that only gets a NEW reference when `mockMessages` itself
// is reassigned (matching react-query's actual behavior: the query result
// object is stable across re-renders when the underlying data hasn't
// changed).
const mockRefetchMessages = vi.fn();
let _cachedMessagesArr: MessageRecord[] | null = null;
let _cachedMessagesData: { messages: MessageRecord[] } | null = null;
function getMessagesData(): { messages: MessageRecord[] } {
  if (_cachedMessagesArr !== mockMessages) {
    _cachedMessagesArr = mockMessages;
    _cachedMessagesData = { messages: mockMessages };
  }
  // Non-null: the assignment above always runs before this on first call.
  return _cachedMessagesData as { messages: MessageRecord[] };
}

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: [], isLoading: false, isError: false }),
  useMessages: () => ({ data: getMessagesData(), refetch: mockRefetchMessages }),
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
  // mutateAsync MUST resolve to a real Promise: useAutotitleEffect calls
  // `.catch()` on its result unconditionally once the first assistant turn
  // completes (which several scenarios below trigger via status:"complete").
  // A bare `vi.fn()` returns undefined, crashing with "Cannot read
  // properties of undefined (reading 'catch')" — a mock-fidelity gap, not a
  // Chat.tsx bug (the real mutateAsync from useMutation always returns a
  // Promise).
  useGenerateTitle: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(async () => undefined),
    isPending: false,
  }),
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
  // The REAL ChatMessage (not mocked here) reads useToastStore as a zustand
  // selector hook (CopyMessageButton), so both exports are needed.
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

// Heavy shell components stubbed — ChatMessage stays REAL (the point of this
// spec is that the merge derivations reach real DOM rows).
vi.mock("@/components/Sidebar", () => ({
  Sidebar: () => createElement("div", { "data-testid": "mock-sidebar" }, "sidebar"),
}));

// Composer stub: a real textarea + Send button wired to the REAL onSubmit
// prop (Chat.tsx's handleSubmit), so tests can drive pendingUser through the
// actual closure rather than reaching into component internals. Mirrors
// test_Chat_compact_rid_clear.spec.tsx's "stub renders a real control wired
// to the real prop" approach.
function ComposerStub(props: {
  chatId: number | null;
  onSubmit: (
    chatId: number,
    payload: ChatStreamPayload,
    userText: string,
  ) => void;
}): ReturnType<typeof createElement> {
  const [text, setText] = useState("");
  return createElement(
    "div",
    { "data-testid": "mock-composer" },
    createElement("textarea", {
      "aria-label": "Message",
      value: text,
      onChange: (e: ChangeEvent<HTMLTextAreaElement>) => {
        setText(e.target.value);
      },
    }),
    createElement(
      "button",
      {
        "aria-label": "Send message",
        type: "button",
        onClick: () => {
          if (props.chatId === null || text.trim() === "") return;
          props.onSubmit(
            props.chatId,
            { input: [{ type: "text", content: text }], model: "test-model" },
            text,
          );
          setText("");
        },
      },
      "Send",
    ),
  );
}

vi.mock("@/components/Composer", () => ({
  Composer: (props: {
    chatId: number | null;
    onSubmit: (
      chatId: number,
      payload: ChatStreamPayload,
      userText: string,
    ) => void;
  }) => createElement(ComposerStub, props),
  resolveChatIntegrationsField: () => undefined,
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

// Captures the router's `navigate` so tests can drive REAL client-side
// navigation (changing the :chatId param on the SAME mounted Chat instance)
// instead of remounting a fresh MemoryRouter — MemoryRouter only consumes
// `initialEntries` on its first render, so passing a different initialPath
// to a `rerender()` call is a no-op for the already-mounted router and would
// silently fail to exercise the wipe-on-chat-switch effect.
let capturedNavigate: ((path: string) => void) | null = null;
function NavCapture(): null {
  const navigate = useNavigate();
  useEffect(() => {
    capturedNavigate = navigate;
  }, [navigate]);
  return null;
}

function chatRouteTree(initialPath: string, queryClient: QueryClient) {
  return createElement(
    QueryClientProvider,
    { client: queryClient },
    createElement(
      MemoryRouter,
      { initialEntries: [initialPath] },
      createElement(NavCapture),
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

function newQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
}

function renderChat(initialPath: string, queryClient: QueryClient = newQueryClient()) {
  return render(chatRouteTree(initialPath, queryClient));
}

function persistedMessage(overrides: Partial<MessageRecord> & { id: number; role: MessageRecord["role"] }): MessageRecord {
  return {
    chat_id: 1,
    content: "",
    reasoning_content: null,
    stop_reason: null,
    tool_calls: null,
    compaction_id: null,
    created_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

/** All rendered message rows, in DOM order, as {id, role}. */
function messageRows(container: HTMLElement): { id: string | null; role: string | null }[] {
  return Array.from(container.querySelectorAll("[data-message-id]")).map((el) => ({
    id: el.getAttribute("data-message-id"),
    role: el.getAttribute("data-message-role"),
  }));
}

function assistantRows(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll('[data-message-role="assistant"]'));
}

function userRows(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll('[data-message-role="user"]'));
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — message-merge derivations + pendingUser lifecycle (characterization)", () => {
  beforeEach(() => {
    mockMessages = [];
    mockSSEState = { ...idleSSEState };
    vi.clearAllMocks();
  });

  // 1. History render — pins serverMessages (~1150) / activeServerMessages
  //    (~1174) feeding the activeServerMessages render loop (~1558-1599).
  it("renders persisted history in order with no SSE activity", () => {
    mockMessages = [
      persistedMessage({ id: 1, role: "user", content: "Hi there" }),
      persistedMessage({ id: 2, role: "assistant", content: "Hello! How can I help?" }),
    ];

    const { container } = renderChat("/chats/1");

    const rows = messageRows(container);
    expect(rows).toEqual([
      { id: "1", role: "user" },
      { id: "2", role: "assistant" },
    ]);
    expect(screen.getByText("Hello! How can I help?")).toBeTruthy();
  });

  // 2. Live streaming row — pins streamingMessages (~1282) under
  //    `streamActive && !persistedHasSameKey` when only status==="streaming"
  //    drives streamActive (~1197-1200), and streamingKey (~1270).
  it("renders a live streaming assistant row when no persisted row shares its key", () => {
    mockMessages = [persistedMessage({ id: 1, role: "user", content: "Hi" })];
    mockSSEState = {
      ...idleSSEState,
      status: "streaming",
      messageId: 50,
      contentDeltas: ["Hello"],
    };

    const { container } = renderChat("/chats/1");

    const assistants = assistantRows(container);
    expect(assistants).toHaveLength(1);
    expect(assistants[0]?.getAttribute("data-message-id")).toBe("50");
    expect(assistants[0]?.textContent).toContain("Hello");
  });

  // 3. persistedHasSameKey — the key guard (~1279-1281). Isolated from
  //    pendingUser entirely: status stays "streaming" (not "complete"), so
  //    streamActive (~1197) is true purely from the streaming status, with
  //    no dependency on pendingUser. This isolates persistedHasSameKey as
  //    the ONLY thing suppressing the would-be-duplicate streaming copy.
  //
  //    DISCREPANCY NOTE vs the task's literal recipe ("submit first so
  //    pendingUser is set, THEN land messages + status:'complete'"): that
  //    recipe does NOT actually isolate persistedHasSameKey. Verified
  //    empirically (see the second test in this block) — landing any
  //    persisted row grows messagesData past pendingUser.baseline, so the
  //    drop-on-growth effect (~476) fires and nulls pendingUser as part of
  //    the SAME render-testing-library `act()` flush used to observe the
  //    result. By the time assertions run, streamActive is already false
  //    (pendingUser === null, status !== "streaming"/"stopped"), so
  //    persistedHasSameKey is never even consulted — the dedup in that path
  //    is fully explained by the baseline-drop effect, not this guard.
  it("persistedHasSameKey suppresses the streaming copy when a persisted row already shares its key", () => {
    mockMessages = [
      persistedMessage({ id: 1, role: "user", content: "Hi" }),
      persistedMessage({ id: 50, role: "assistant", content: "Final answer" }),
    ];
    mockSSEState = {
      ...idleSSEState,
      status: "streaming",
      messageId: 50,
      contentDeltas: ["X"],
    };

    const { container } = renderChat("/chats/1");

    const assistants = assistantRows(container);
    expect(assistants).toHaveLength(1);
    expect(assistants[0]?.getAttribute("data-message-id")).toBe("50");
    // The persisted content wins (not the SSE "X") — proves the row shown is
    // the persisted one, not a surviving streaming duplicate.
    expect(assistants[0]?.textContent).toContain("Final answer");
    expect(assistants[0]?.textContent).not.toContain("X");
  });

  it("[discrepancy] the submit-driven complete+dedup recipe is actually explained by the baseline-drop effect, not persistedHasSameKey", () => {
    mockMessages = [];
    const queryClient = newQueryClient();
    const { container, rerender } = renderChat("/chats/1", queryClient);

    // Submit via the real Composer → real handleSubmit sets
    // pendingUser = { text, baseline: 0 } (messagesData was empty at submit).
    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "my question" },
    });
    fireEvent.click(screen.getByLabelText("Send message"));
    // Confirms pendingUser really is set right after submit (optimistic bubble up).
    expect(userRows(container)).toHaveLength(1);

    // Land BOTH the user + assistant rows on the SAME Chat instance, with the
    // assistant sharing the SSE messageId (50) — the literal task recipe.
    mockMessages = [
      persistedMessage({ id: 1, role: "user", content: "my question" }),
      persistedMessage({ id: 50, role: "assistant", content: "Final answer" }),
    ];
    mockSSEState = {
      ...idleSSEState,
      status: "complete",
      messageId: 50,
      contentDeltas: ["X"],
    };
    rerender(chatRouteTree("/chats/1", queryClient));

    // Assistant id=50 renders exactly once (the described outcome holds)...
    const assistants = assistantRows(container);
    expect(assistants).toHaveLength(1);
    expect(assistants[0]?.getAttribute("data-message-id")).toBe("50");

    // ...but the optimistic user bubble is ALSO gone by now (only the
    // persisted user row remains), proving pendingUser was already nulled by
    // the drop-on-growth effect (messagesData grew 0 -> 2, past baseline 0)
    // in the SAME act() flush this rerender() performed. Since pendingUser
    // is null, streamActive is false regardless of persistedHasSameKey — the
    // guard at ~1279 was never the operative mechanism in this path.
    expect(userRows(container)).toHaveLength(1);
    expect(userRows(container)[0]?.getAttribute("data-message-id")).toBe("1");
  });

  // 4. streamActive holds through the complete→refetch gap — pins the
  //    `(sseState.status === "complete" && pendingUser !== null)` clause
  //    (~1197-1200). Requires pendingUser.baseline to already cover the one
  //    pre-existing persisted message, so the drop-on-growth effect (~476)
  //    does NOT fire until we deliberately grow past it.
  it("keeps the streaming bubble mounted through the complete+pendingUser gap, then drops it once messages grow past baseline", () => {
    mockMessages = [persistedMessage({ id: 1, role: "user", content: "existing question" })];
    const queryClient = newQueryClient();
    const { container, rerender } = renderChat("/chats/1", queryClient);

    // Submit a NEW turn — baseline is captured as the CURRENT length (1),
    // so as long as mockMessages stays at length 1, pendingUser survives.
    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "second question" },
    });
    fireEvent.click(screen.getByLabelText("Send message"));

    // Stream flips straight to "complete" before the refetch lands —
    // mockMessages is UNCHANGED (still length 1, == baseline).
    mockSSEState = {
      ...idleSSEState,
      status: "complete",
      messageId: 99,
      contentDeltas: ["draft"],
    };
    rerender(chatRouteTree("/chats/1", queryClient));

    const midAssistants = assistantRows(container);
    expect(midAssistants).toHaveLength(1);
    expect(midAssistants[0]?.getAttribute("data-message-id")).toBe("99");
    expect(midAssistants[0]?.textContent).toContain("draft");

    // Now the refetch lands: messagesData grows past baseline (1 -> 2).
    // The drop-on-growth effect (~476) fires, nulling pendingUser, which
    // flips streamActive back to false (status is "complete", not
    // "streaming"/"stopped", and pendingUser is now null).
    mockMessages = [
      persistedMessage({ id: 1, role: "user", content: "existing question" }),
      persistedMessage({ id: 2, role: "user", content: "second question" }),
    ];
    rerender(chatRouteTree("/chats/1", queryClient));

    expect(assistantRows(container)).toHaveLength(0);
  });

  // 5. displayStreamContent strips the followups marker (~1256-1261), even
  //    mid-stream before the closing "-->" has arrived (the marker check is
  //    a plain indexOf, not a closed-tag regex).
  it("strips the hidden followups marker from the displayed streaming content", () => {
    mockMessages = [];
    mockSSEState = {
      ...idleSSEState,
      status: "streaming",
      messageId: 7,
      // PARTIAL marker — no closing "-->" yet (the real streaming race the
      // strip exists for). With a *complete* comment the browser hides it as
      // an HTML comment regardless of the strip, so only the unclosed form
      // makes displayStreamContent's indexOf-strip observable.
      contentDeltas: ["Answer here", '<!--followups:["q1"'],
    };

    const { container } = renderChat("/chats/1");

    const assistants = assistantRows(container);
    expect(assistants).toHaveLength(1);
    expect(assistants[0]?.textContent).toContain("Answer here");
    expect(assistants[0]?.textContent).not.toContain("<!--followups");
  });

  // 6. showContinue from persisted stop_reason (~1160: serverMessages maps
  //    `showContinue: m.stop_reason === "length"`), surfaced by the REAL
  //    ChatMessage as the `data-testid="continue-chip"` "Cut off" marker.
  it("surfaces the Continue ('Cut off') affordance only for a persisted row with stop_reason='length'", () => {
    mockMessages = [
      persistedMessage({ id: 10, role: "assistant", content: "truncated answer", stop_reason: "length" }),
      persistedMessage({ id: 11, role: "assistant", content: "complete answer", stop_reason: null }),
    ];

    const { container } = renderChat("/chats/1");

    const row10 = container.querySelector('[data-message-id="10"]');
    const row11 = container.querySelector('[data-message-id="11"]');
    expect(row10?.querySelector('[data-testid="continue-chip"]')).not.toBeNull();
    expect(row11?.querySelector('[data-testid="continue-chip"]')).toBeNull();
  });

  // 7. pendingUser optimistic bubble + drop-on-growth — pins
  //    optimisticUserMessages (~1180-1190) and the drop effect (~473-477).
  it("shows an optimistic user bubble immediately on submit, then drops it (not duplicated) once the persisted row lands", () => {
    mockMessages = [];
    const queryClient = newQueryClient();
    const { container, rerender } = renderChat("/chats/1", queryClient);

    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "optimistic text" },
    });
    fireEvent.click(screen.getByLabelText("Send message"));

    // Optimistic bubble appears immediately (pendingUser !== null), with
    // the synthetic "pending-user" id.
    const beforeUsers = userRows(container);
    expect(beforeUsers).toHaveLength(1);
    expect(beforeUsers[0]?.getAttribute("data-message-id")).toBe("pending-user");
    expect(beforeUsers[0]?.textContent).toContain("optimistic text");

    // Persisted copy lands — messagesData grows past baseline (0 -> 1).
    mockMessages = [persistedMessage({ id: 5, role: "user", content: "optimistic text" })];
    rerender(chatRouteTree("/chats/1", queryClient));

    // Exactly one user row remains — the persisted one, not a duplicate.
    const afterUsers = userRows(container);
    expect(afterUsers).toHaveLength(1);
    expect(afterUsers[0]?.getAttribute("data-message-id")).toBe("5");
  });

  // Wipe-on-chat-switch (~481) — pendingUser is discarded when chatId changes.
  it("wipes the pending optimistic bubble when the chat is switched", () => {
    mockMessages = [];
    const queryClient = newQueryClient();
    const { container } = renderChat("/chats/1", queryClient);

    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "text for chat 1" },
    });
    fireEvent.click(screen.getByLabelText("Send message"));
    expect(userRows(container)).toHaveLength(1);

    // Real client-side navigation to a different chat, on the SAME mounted
    // Chat instance (see the capturedNavigate/NavCapture comment above).
    expect(capturedNavigate).toBeTypeOf("function");
    act(() => {
      capturedNavigate?.("/chats/2");
    });

    expect(userRows(container)).toHaveLength(0);
  });
});
