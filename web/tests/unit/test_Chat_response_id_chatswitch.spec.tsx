/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat — response_id / message-cache cross-chat scoping.
 *
 * `sseState` (useSSE) is a single instance kept alive across chat
 * navigation — NOT chat-scoped (see the followupSuggestions wipe comment in
 * Chat.tsx). The response-id-persistence effect used the live `chatId`
 * prop for `storeResponseId`/`clearResponseId`/`qc.invalidateQueries`, with
 * no check against which chat's stream actually completed. So: send a
 * message in chat 1, navigate to chat 2 before it finishes, chat 1
 * completes in the background — chat 2 would get chat 1's `response_id`
 * written under ITS OWN localStorage key, and chat 2's own message-list
 * cache would get invalidated (pointless) instead of chat 1's (which
 * needed it). The next turn in chat 2 would then send chat 1's
 * `previous_response_id`, continuing chat 1's provider-side thread inside
 * chat 2 — cross-conversation contamination, no queue and no flag
 * required, just navigating away from a streaming answer.
 *
 * This pins the fix: the effect is now gated on `sseState.chatId ===
 * chatId`, so it never fires for the wrong chat.
 *
 * Scaffold mirrors test_Chat_chatswitch_ephemeral_wipe.spec.tsx (same
 * two-chat fixture + navigate-without-remount harness).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
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
import { loadResponseId } from "@/lib/responseId";
import type { StreamState } from "@/hooks/useSSE";

if (
  typeof window !== "undefined" &&
  !(Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
) {
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

// Controllable SSE mock — `mockSSEState` fixed at each assignment, read
// fresh on every render. Reassigned mid-test + `rerender()`d to simulate a
// background stream's async completion landing while a different chat is
// on screen — same idiom test_Chat.spec.tsx's mtp_suspected suite uses.
const idleSSEState: StreamState = {
  status: "idle",
  chatId: null,
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
  followups: [],
  warnings: [],
  memorySaved: undefined,
  modeAdopt: undefined,
};

let mockSSEState: StreamState = { ...idleSSEState };
const resetStreamSpy = vi.fn();

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    state: mockSSEState,
    start: vi.fn(),
    stop: vi.fn(),
    reset: resetStreamSpy,
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

vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({ data: undefined }),
}));

// Two chats — Chat must stay mounted across a navigate between them.
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

const refetchMessagesSpy = vi.fn().mockResolvedValue({ data: { messages: [] } });

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: mockChats, isLoading: false, isError: false }),
  useMessages: () => ({ data: { messages: [] }, refetch: refetchMessagesSpy }),
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
  useChatPreset: () => ({ activePreset: "", preset: null, setPreset: vi.fn(), clearPreset: vi.fn(), adoptModelPreset: vi.fn(), adoptedByModel: false }),
  useHydrateChatPresets: () => undefined,
  useChatPresetStore: (sel: (s: { overrides: Record<number, string>; sources: Record<number, "user" | "model"> }) => unknown) =>
    sel({ overrides: {}, sources: {} }),
}));

vi.mock("@/hooks/useMemory", () => ({
  usePinInsight: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
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

vi.mock("@/components/chat/SubSessionPanel", () => ({
  SubSessionPanel: () => createElement("div", { "data-testid": "mock-subsessionpanel" }),
}));

vi.mock("@/components/Sidebar", () => ({
  Sidebar: () => createElement("div", { "data-testid": "mock-sidebar" }, "sidebar"),
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

// ─── Helpers ─────────────────────────────────────────────────────────────────

import Chat from "@/pages/Chat";

let capturedNavigate: NavigateFunction | null = null;
function NavCapture(): null {
  capturedNavigate = useNavigate();
  return null;
}

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
      createElement(NavCapture),
      createElement(
        Routes,
        null,
        createElement(Route, { path: "/chats/:chatId", element: createElement(Chat) }),
      ),
    ),
  );
}

function renderChat(initialPath: string) {
  return render(chatRouteTree(initialPath));
}

function navigateTo(path: string): void {
  act(() => {
    if (capturedNavigate === null) throw new Error("navigate not captured");
    void capturedNavigate(path);
  });
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — response_id / message-cache cross-chat scoping", () => {
  beforeEach(() => {
    __resetChatScopedMemoryForTests();
    capturedNavigate = null;
    mockSSEState = { ...idleSSEState };
    refetchMessagesSpy.mockClear();
    localStorage.clear();
  });

  it("does NOT give chat 2 chat 1's response_id when chat 1's stream completes in the background while chat 2 is on screen", () => {
    // Chat 1's stream is in flight.
    mockSSEState = { ...idleSSEState, chatId: 1, status: "streaming" };
    const { rerender } = renderChat("/chats/1");

    // User navigates away before it finishes.
    navigateTo("/chats/2");

    // Chat 1's stream completes in the background — sseState is a single
    // shared instance, still tagged chatId: 1, NOT reset by the navigate.
    mockSSEState = {
      ...idleSSEState,
      chatId: 1,
      status: "complete",
      responseId: "resp-from-chat-1",
    };
    rerender(chatRouteTree("/chats/2"));

    // The cross-chat leak: chat 2 must never receive chat 1's response_id.
    expect(loadResponseId(2)).toBeNull();
  });

  it("still stores the response_id correctly for the ORDINARY case — same chat throughout", () => {
    mockSSEState = { ...idleSSEState, chatId: 1, status: "streaming" };
    const { rerender } = renderChat("/chats/1");

    mockSSEState = {
      ...idleSSEState,
      chatId: 1,
      status: "complete",
      responseId: "resp-from-chat-1",
    };
    rerender(chatRouteTree("/chats/1"));

    expect(loadResponseId(1)).toBe("resp-from-chat-1");
  });
});
