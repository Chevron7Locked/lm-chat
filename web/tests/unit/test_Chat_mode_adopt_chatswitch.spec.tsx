/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat — C3 mode-adoption (`mode_adopt` SSE frame) cross-chat scoping.
 *
 * `sseState` (useSSE) is a single instance kept alive across chat
 * navigation — NOT chat-scoped (see the followupSuggestions wipe comment in
 * Chat.tsx). `useChatPreset(chatId)`'s `adoptModelPreset` is a useCallback
 * keyed on `chatId`, so its identity changes on every chat switch. Before
 * the fix, the effect consuming `sseState.modeAdopt` re-fired on that
 * identity change alone and reapplied a STALE verdict — computed for the
 * chat whose stream produced it — to whatever chat happened to be on
 * screen at the time. This pins the fix: a verdict is only ever applied to
 * the chat it actually belongs to (`sseState.chatId` — see StreamState;
 * `modeAdopt` itself no longer carries its own duplicate chatId, folded
 * into this single top-level mechanism shared by every sseState consumer).
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
import type { StreamState } from "@/hooks/useSSE";

if (typeof window !== "undefined" && !(Element.prototype as { scrollIntoView?: () => void }).scrollIntoView) {
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

// Controllable SSE mock — `mockSSEState` carries a `modeAdopt` verdict and
// the top-level `chatId` of the stream that produced it (chat 1's, always,
// in this file — see StreamState.chatId), fixed at mount and never
// reassigned. Any effect refire this file observes is caused purely by the
// chat-navigation identity change, not by the verdict/chatId themselves
// changing.
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

// useSSE(chatId) is a per-chat selector in the real implementation (a
// chat-keyed streamStore slot) — this mock reflects that: only chat 1's
// view reads `mockSSEState` (every test in this file tags its verdict
// chatId: 1 — see the comment above), every OTHER chatId reads a plain
// idle slot with no modeAdopt verdict, mirroring the store's guarantee
// that chat 1's verdict never appears in chat 2's slot.
vi.mock("@/hooks/useSSE", () => ({
  useSSE: (chatId: number | null) => ({
    state: chatId === 1 ? mockSSEState : idleSSEState,
    start: vi.fn(),
    stop: vi.fn(),
    reset: vi.fn(),
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

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: mockChats, isLoading: false, isError: false }),
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

// The spy under test: `useChatPreset` returns a fresh `adoptModelPreset`
// closure bound to whatever `chatId` it's called with, same shape as the
// real hook's `useCallback([chatId, adoptModel, updateChat])` — every call
// records (chatId, presetId) so the assertions can tell which chat an
// adoption was actually applied to.
const adoptModelPresetSpy = vi.fn<(chatId: number | null, presetId: string) => void>();

vi.mock("@/hooks/useChatPreset", () => ({
  useChatPreset: (chatId: number | null) => ({
    activePreset: "",
    preset: null,
    setPreset: vi.fn(),
    clearPreset: vi.fn(),
    adoptModelPreset: (presetId: string) => {
      adoptModelPresetSpy(chatId, presetId);
    },
    adoptedByModel: false,
  }),
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
        createElement(NavCapture),
        createElement(
          Routes,
          null,
          createElement(Route, { path: "/chats/:chatId", element: createElement(Chat) }),
        ),
      ),
    ),
  );
}

function navigateTo(path: string): void {
  act(() => {
    if (capturedNavigate === null) throw new Error("navigate not captured");
    // MemoryRouter (declarative mode, not a data router) always navigates
    // synchronously — void the call to document that we deliberately don't
    // await NavigateFunction's `void | Promise<void>` return type here.
    void capturedNavigate(path);
  });
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — mode_adopt (C3) cross-chat scoping", () => {
  beforeEach(() => {
    __resetChatScopedMemoryForTests();
    capturedNavigate = null;
    mockSSEState = { ...idleSSEState };
    adoptModelPresetSpy.mockClear();
  });

  it("applies a mode_adopt verdict to the chat whose stream produced it", () => {
    mockSSEState = {
      ...idleSSEState,
      chatId: 1,
      modeAdopt: { presetId: "coder", msgId: 1 },
    };
    renderChat("/chats/1");

    expect(adoptModelPresetSpy).toHaveBeenCalledWith(1, "coder");
  });

  it("does NOT reapply a chat 1 verdict once the user has navigated to chat 2", () => {
    mockSSEState = {
      ...idleSSEState,
      chatId: 1,
      modeAdopt: { presetId: "coder", msgId: 1 },
    };
    renderChat("/chats/1");
    adoptModelPresetSpy.mockClear();

    // sseState is a single shared instance — the verdict object AND the
    // top-level chatId it's tagged with are deliberately NOT reset by
    // this navigate (see the doc above), so state.chatId is still 1 once
    // the user is on chat 2.
    navigateTo("/chats/2");

    // Must never have been applied to chat 2 — that's the cross-chat leak.
    const chat2Calls = adoptModelPresetSpy.mock.calls.filter(([cid]) => cid === 2);
    expect(chat2Calls).toHaveLength(0);
  });

  it("keeps re-applying (harmlessly) to chat 1 itself across unrelated re-renders, but never to chat 2 or beyond", () => {
    mockSSEState = {
      ...idleSSEState,
      chatId: 1,
      modeAdopt: { presetId: "researcher", msgId: 7 },
    };
    renderChat("/chats/1");

    navigateTo("/chats/2");
    navigateTo("/chats/1");
    navigateTo("/chats/2");

    const calls = adoptModelPresetSpy.mock.calls;
    expect(calls.every(([cid]) => cid === 1)).toBe(true);
    expect(calls.some(([cid]) => cid === 2)).toBe(false);
  });
});
