/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat — REAL Composer + REAL Chat.tsx queue integration (regression guard
 * for the 08-15 live-dogfood j8 failure).
 *
 * Every other queue/chatId test in this suite mocks HALF of the real
 * mechanism: test_Composer_message_queue.spec.tsx renders Composer alone
 * with a hand-rolled `onSubmit` spy (never exercises Chat.tsx's real
 * `handleSubmit`/`startStream`/`sseState` wiring), while
 * test_Chat_*_chatswitch.spec.tsx files render <Chat> with Composer
 * REPLACED by a bare textarea stub (never exercises Composer's real queue
 * effect). Both suites passed while j8 — a real two-turn exchange against
 * a real model — hung for 30 minutes with the queued message never sent.
 * Neither suite could have caught that: the seam between "Composer thinks
 * it enqueued/drained correctly" and "Chat.tsx's real handleSubmit actually
 * got called with it" was never exercised end-to-end.
 *
 * This file renders BOTH real components together, with only `useSSE`
 * mocked (a controllable `state` + a spied `start`), so a submit — whether
 * the first turn, an enqueue, or a drained second turn — has to travel the
 * REAL path: Composer's real submit/queue logic → the real `onSubmit` prop
 * (Chat.tsx's real `handleSubmit`) → the real `startStream` call → the
 * mocked `useSSE().start` spy, which is the only point this test observes.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { __resetChatScopedMemoryForTests } from "@/hooks/useChatScopedState";
import type { StreamState, ChatStreamPayload } from "@/hooks/useSSE";

if (
  typeof window !== "undefined" &&
  !(Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
) {
  Element.prototype.scrollIntoView = function (): void { /* no-op */ };
}

// ─── Mocks — Chat.tsx's own dependencies (heavy hooks / chrome) ─────────────

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

vi.mock("@/hooks/useKeyboardShortcuts", () => ({
  useKeyboardShortcuts: () => undefined,
}));

// ─── The mock under test — useSSE ────────────────────────────────────────────
//
// `state` is read fresh every render; the test reassigns it + calls
// `rerender()` to simulate what the REAL start()/handleEvent would have
// done (status/chatId transitions). `start` is a spy — the ONE thing this
// test actually asserts on: did the real Composer→Chat.tsx path call it,
// with what chatId, and how many times.
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
const startSpy = vi.fn<(chatId: number, payload: ChatStreamPayload) => Promise<void>>()
  .mockResolvedValue(undefined);
const stopSpy = vi.fn();
const resetSpy = vi.fn();

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    state: mockSSEState,
    start: startSpy,
    stop: stopSpy,
    reset: resetSpy,
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
    loadedModels: [{ id: "model-x" }],
    error: null,
    isFetching: false,
    refresh: () => undefined,
  }),
}));

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
];

// Stable across renders — mirrors real react-query's memoized `refetch`.
// A fresh vi.fn() created INSIDE the useMessages() factory call would
// change identity every render, making any effect depending on it
// (e.g. the response-id effect) look "changed" every render and re-fire
// forever — a self-inflicted infinite loop, not a real bug.
const refetchMessagesSpy = vi.fn().mockResolvedValue({ data: { messages: [] } });
const emptyMessagesData = { messages: [] };

vi.mock("@/hooks/useChats", () => ({
  useChatsDirect: () => ({ data: mockChats, isLoading: false, isError: false }),
  useMessages: () => ({ data: emptyMessagesData, refetch: refetchMessagesSpy }),
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
  useChatPreset: () => ({
    activePreset: "",
    preset: null,
    setPreset: vi.fn(),
    clearPreset: vi.fn(),
    adoptModelPreset: vi.fn(),
    adoptedByModel: false,
  }),
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

// ─── Mocks — Composer's OWN dependencies (NOT mocking Composer itself) ──────

vi.mock("@/hooks/useSTT", () => ({
  useSTT: () => ({
    capability: { available: false, engine: null },
    state: { listening: false, error: null },
    start: vi.fn(),
    stop: vi.fn(),
  }),
  detectSTT: () => ({ available: false, engine: null }),
}));

vi.mock("@/components/MicButton", () => ({
  MicButton: () => null,
}));

vi.mock("@/components/InProjectChip", () => ({
  InProjectChip: () => null,
}));

vi.mock("@/components/RagModeBadge", () => ({
  RagModeBadge: () => null,
}));

vi.mock("@/components/SlashMenu", () => ({
  SlashMenu: () => null,
  parseSlashCommand: () => null,
  BUILTIN_COMMANDS: [],
  filterCommands: () => [],
}));

vi.mock("@/hooks/usePrompts", () => ({
  usePrompts: () => ({ data: [], isLoading: false, isError: false }),
}));

vi.mock("@/hooks/useIntegrationsList", () => ({
  useIntegrationsList: () => ({ data: [], isLoading: false, isError: false }),
  useUpdateIntegrationsList: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
  }),
  lmStudioConfigKeys: { resolved: () => ["lmstudio-config", "resolved"] },
}));

const mockModelsData = {
  models: [
    {
      id: "model-x",
      name: "Model X",
      loaded: true,
      loaded_instance_ids: ["model-x"],
      capabilities: {
        vision: false,
        trained_for_tool_use: false,
        reasoning: null,
        embedding: false,
      },
      max_context_length: 8192,
      size_bytes: 0,
      params_string: "",
    },
  ],
};

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: mockModelsData, isLoading: false, isError: false, refetch: vi.fn() }),
}));

// ─── Helpers ─────────────────────────────────────────────────────────────────

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
        createElement(Route, { path: "/chats/:chatId", element: createElement(Chat) }),
      ),
    ),
  );
}

function renderChat(initialPath: string) {
  return render(chatRouteTree(initialPath));
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Chat — real Composer + real Chat.tsx queue integration", () => {
  beforeEach(() => {
    __resetChatScopedMemoryForTests();
    mockSSEState = { ...idleSSEState };
    startSpy.mockClear();
    refetchMessagesSpy.mockClear();
  });

  it("mirrors j8: turn 1 streams, turn 2 submitted mid-stream ENQUEUES (does not resubmit), then drains and sends once turn 1 completes", () => {
    const { rerender } = renderChat("/chats/1");
    const textarea = screen.getByPlaceholderText(/Message/i) as HTMLTextAreaElement;

    // Turn 1: real submit → real handleSubmit → real startStream → the
    // mocked start() spy.
    fireEvent.change(textarea, { target: { value: "turn 1 text" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(startSpy).toHaveBeenCalledTimes(1);
    expect(startSpy.mock.calls[0]?.[0]).toBe(1);

    // Simulate what the REAL start() would have done synchronously:
    // status → streaming, chatId → 1.
    mockSSEState = { ...idleSSEState, status: "streaming", chatId: 1 };
    rerender(chatRouteTree("/chats/1"));

    // Turn 2, submitted while turn 1 is genuinely streaming (same chat).
    // This is the exact scenario the live dogfood found broken: if the
    // real `streaming` prop reads false here despite a live same-chat
    // stream, this submit goes straight through instead of enqueueing —
    // startSpy would jump to 2 immediately, which must NOT happen.
    fireEvent.change(textarea, { target: { value: "turn 2 text" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(startSpy).toHaveBeenCalledTimes(1); // still just turn 1 — turn 2 must be QUEUED
    expect(screen.getByTestId("composer-queue")).toBeTruthy();
    expect(screen.getByTestId("composer-queue-item").textContent).toContain("turn 2 text");

    // Turn 1 completes naturally.
    mockSSEState = { ...idleSSEState, status: "complete", chatId: 1, responseId: "r1" };
    rerender(chatRouteTree("/chats/1"));

    // The drain must fire: a SECOND real startStream call, for chat 1,
    // carrying turn 2's text.
    expect(startSpy).toHaveBeenCalledTimes(2);
    const secondCall = startSpy.mock.calls[1];
    expect(secondCall?.[0]).toBe(1);
    expect(secondCall?.[1].input).toEqual([{ type: "text", content: "turn 2 text" }]);

    // The queue is empty again.
    expect(screen.queryByTestId("composer-queue")).toBeNull();
  });
});
