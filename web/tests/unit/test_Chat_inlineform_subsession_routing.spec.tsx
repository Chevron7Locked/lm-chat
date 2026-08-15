/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat — inline-form '/research <query>' sub-session routing
 * characterization (FE-STATE work-stream, STEP 0, 2026-07-17).
 *
 * Pins the CURRENT observable contract of the Composer inline-form path:
 * typing "/research quantum chromodynamics" and hitting Enter in one
 * keystroke both (a) opens a sub-session for the "research" preset and
 * (b) routes that same submit INTO the sub-session's stream — not the main
 * chat stream. That routing is what the ref-mirror bandaid in
 * useSubSession.ts (subSessionRef, synced via effect, read ref-first in
 * maybeRouteSubmit) currently exists to guarantee, defeating the stale
 * closure the Composer's `setTimeout(0)` deferral would otherwise read.
 *
 * Deliberately black-box: renders the REAL Composer + REAL useSubSession
 * through the REAL Chat.tsx page (only network-adjacent hooks and heavy
 * leaf components are mocked), and asserts on the OUTCOME (which stream
 * received the message), not on any ref/timing implementation detail. That
 * makes this test valid unchanged whether the routing is defended by the
 * ref/setTimeout pair (current) or by passing the freshly-started
 * sub-session explicitly (STEP 1's fix) — same test, either mechanism.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// TS's DOM lib declares scrollIntoView as always present on Element.prototype
// (it isn't, in jsdom). The `in` check reads real runtime presence; going
// through a boolean (rather than the `in` expression directly in the `if`)
// avoids narrowing Element.prototype to `never` in the assignment below.
const hasScrollIntoView = "scrollIntoView" in Element.prototype;
if (typeof window !== "undefined" && !hasScrollIntoView) {
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

// Composer-only hooks — not needed by the chatswitch-style fixtures (which
// stub Composer entirely) but required here since Composer renders for real.
vi.mock("@/hooks/useSTT", () => ({
  useSTT: () => ({
    capability: { available: false, engine: null },
    state: { listening: false, error: null },
    start: vi.fn(),
    stop: vi.fn(),
  }),
}));

vi.mock("@/hooks/usePrompts", () => ({
  usePrompts: () => ({ data: [], isLoading: false, isError: false }),
}));

vi.mock("@/hooks/useIntegrationsList", () => ({
  useIntegrationsList: () => ({ data: [], isLoading: false, isError: false }),
  useUpdateIntegrationsList: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
}));

// Main-chat stream — spied so the test can assert it was NOT used (the
// submit must route into the sub-session, not fall through to here).
const mainStartSpy = vi.fn();
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
      followups: [],
      warnings: [],
    },
    start: mainStartSpy,
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

// Sub-session stream — spied so the test can assert the routed submit
// landed HERE.
const subStreamSpy = vi.fn();
vi.mock("@/hooks/useSubSessionSSE", () => ({
  useSubSessionSSE: () => ({
    state: { status: "idle", content: "", error: null },
    stream: subStreamSpy,
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

const mockChats = [
  {
    id: 1,
    title: "Chat A",
    model_id: "model-x",
    pinned: false,
    incognito: false,
    updated_at: "2026-07-17T00:00:00Z",
    settings: {},
  },
];

vi.mock("@/hooks/useChats", () => ({
  useChats: () => ({ data: { chats: mockChats, total: mockChats.length }, isLoading: false, isError: false }),
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

vi.mock("@/components/ChatMessage", () => ({
  ChatMessage: () => createElement("div", { "data-testid": "mock-chatmessage" }),
}));

vi.mock("@/components/ABCompareView", () => ({
  ABCompareView: () => createElement("div", { "data-testid": "mock-abcompare" }),
}));

// Sub-session panel mount marker only — proves a sub-session opened without
// pulling in its ChatMessage-list rendering.
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

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — inline-form '/research <query>' sub-session routing (FE-STATE characterization)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("routes the inline-form submit into the sub-session stream, not the main chat stream", async () => {
    renderChat("/chats/1");

    const textarea = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(textarea, {
      target: { value: "/research quantum chromodynamics" },
    });
    fireEvent.keyDown(textarea, { key: "Enter" });

    // Current implementation defers the inline-form onSubmit via
    // setTimeout(0); STEP 1 makes it synchronous. waitFor tolerates either.
    await waitFor(() => {
      expect(subStreamSpy).toHaveBeenCalledTimes(1);
    });

    const call = subStreamSpy.mock.calls[0]?.[0] as {
      chatId: number;
      messages: { role: string; content: string }[];
    };
    expect(call.chatId).toBe(1);
    expect(call.messages).toEqual([
      { role: "user", content: "quantum chromodynamics" },
    ]);

    // Did NOT fall through to the regular chat/main-stream path.
    expect(mainStartSpy).not.toHaveBeenCalled();

    // Sub-session panel is showing (mode switched, not just a side-effect).
    expect(screen.queryByTestId("mock-subsessionpanel")).not.toBeNull();
  });
});
