/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat — chat-switch ephemeral-state wipe characterization
 * (FE-STATE work-stream, STEP 0, 2026-07-17).
 *
 * Pins THREE state-bandaid behaviors ahead of their STEP 1-3 removal:
 *  1. followupSuggestions clear on chatId change (Chat.tsx ~623-642).
 *  2. An open sub-session is cancelled on chatId change (useSubSession.ts
 *     ~151-169, the prevChatIdRef wipe effect).
 *  3. A lingering "stopped" stream's zombie state is wiped on chatId change
 *     (useStoppedStreamReconciliation.ts ~91-100).
 *
 * Scaffold mirrors test_Chat_selectedModel_chatswitch.spec.tsx (same
 * two-chat fixture + navigate-without-remount harness — Chat must stay
 * mounted across the switch, which is exactly the condition under which
 * un-keyed state bleeds across chats). Two differences from that scaffold:
 *  - useSSE is mocked with a per-test-settable `mockSSEState` (plus a
 *    `reset` spy) instead of a fixed idle state, so the stopped-stream test
 *    can observe the reconciliation hook's reset call.
 *  - Composer is stubbed with an extra button that calls `onPresetActivate`
 *    directly — the fastest way to drive Chat's REAL useSubSession hook
 *    into a non-null subSession without going through slash-command text
 *    parsing (that path is characterized separately, at the Composer
 *    level, for the inline-form '/research <query>' race fix).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
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

// Controllable SSE mock. Unlike the selectedModel-chatswitch fixture (fixed
// idle state), this test needs to (a) seed OOB followups and (b) put the
// stream in "stopped" status — so `mockSSEState` is settable per-test
// (assigned before render; no mid-render transitions needed, since a
// mount-time value plus a chatId-only navigate is enough to isolate the
// switch-triggered reset from the mount-time one).
// Real StreamState import above — not a hand-rolled shadow. A local mirror
// of this shape (the previous `MockSSEState`) was missing five fields the
// real type has gained over time, including `modeAdopt` (C3 role-adoption,
// shipped 08-14) — invisible until now because a vi.mock factory's return
// value isn't checked against the real hook's type.
const idleSSEState: StreamState = {
  status: "idle",
  // Matches the /chats/1 target every test in this file renders first —
  // see StreamState.chatId.
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
    refresh: async () => undefined,
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

vi.mock("@/hooks/useChatPreset", () => ({
  useChatPreset: () => ({ activePreset: "", preset: null, setPreset: vi.fn(), clearPreset: vi.fn() }),
  useHydrateChatPresets: () => undefined,
  useChatPresetStore: (sel: (s: { overrides: Record<number, string>; sources: Record<number, "user" | "model"> }) => unknown) =>
    sel({ overrides: {}, sources: {} }),
}));

vi.mock("@/hooks/useMemory", () => ({
  usePinInsight: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}));

// Composer stub: a bare textarea plus one extra button that calls
// onPresetActivate("research") directly. That drives Chat's real
// useSubSession hook the same way the rail-picker / two-step slash-command
// path does, without needing the inline-form text-parsing machinery.
vi.mock("@/components/Composer", () => ({
  Composer: ({
    onPresetActivate,
  }: {
    onPresetActivate?: ((presetId: string) => void) | undefined;
  }) =>
    createElement(
      "div",
      { "data-testid": "mock-composer" },
      createElement("textarea", { "aria-label": "Message" }),
      createElement(
        "button",
        {
          type: "button",
          "data-testid": "start-research-subsession",
          onClick: () => {
            onPresetActivate?.("research");
          },
        },
        "start research",
      ),
    ),
}));

vi.mock("@/components/ChatMessage", () => ({
  ChatMessage: () => createElement("div", { "data-testid": "mock-chatmessage" }),
}));

vi.mock("@/components/ABCompareView", () => ({
  ABCompareView: () => createElement("div", { "data-testid": "mock-abcompare" }),
}));

// Sub-session panel mount marker only — its internals (ChatMessage list,
// finalize/inject buttons) aren't under test here.
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

// Stores ----------------------------------------------------------------------

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
    capturedNavigate(path);
  });
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — chat-switch ephemeral-state wipe (FE-STATE characterization)", () => {
  beforeEach(() => {
    __resetChatScopedMemoryForTests();
    capturedNavigate = null;
    mockSSEState = { ...idleSSEState };
    vi.clearAllMocks();
  });

  it("clears followupSuggestions on chat switch", () => {
    mockSSEState = { ...idleSSEState, followups: ["chip one", "chip two"] };
    renderChat("/chats/1");

    const chips = screen.getAllByTestId("followup-chip");
    expect(chips.map((el) => el.textContent)).toEqual(["chip one", "chip two"]);

    navigateTo("/chats/2");
    expect(screen.queryAllByTestId("followup-chip")).toHaveLength(0);
  });

  it("cancels an open sub-session on chat switch", () => {
    renderChat("/chats/1");
    expect(screen.queryByTestId("mock-subsessionpanel")).toBeNull();

    fireEvent.click(screen.getByTestId("start-research-subsession"));
    expect(screen.queryByTestId("mock-subsessionpanel")).not.toBeNull();

    navigateTo("/chats/2");
    expect(screen.queryByTestId("mock-subsessionpanel")).toBeNull();
  });

  it("wipes a lingering stopped-stream zombie state on chat switch", () => {
    mockSSEState = { ...idleSSEState, status: "stopped" };
    renderChat("/chats/1");

    // Mount-time artifact: the chatId-tracking ref (prevChatIdForStopRef)
    // starts at null, so the reconciliation effect also fires once on the
    // very first mount (null !== 1). Clear it here so the assertion below
    // isolates the SWITCH-triggered call specifically.
    resetStreamSpy.mockClear();

    navigateTo("/chats/2");
    expect(resetStreamSpy).toHaveBeenCalledTimes(1);
  });
});
