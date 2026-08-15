/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useReasoningOverridePersistence — hydrate/persist characterization tests.
 *
 * The reasoning-persist cluster (K-001, extracted from pages/Chat.tsx in FE
 * decomposition cut #5) had ZERO existing coverage before this file. These
 * tests pin CURRENT behavior, read directly off the hook's source:
 *
 *   (a) hydrateFromChats fires exactly once for the hook's lifetime — the
 *       guard is a single `hydratedRef`, never reset per-chat, so switching
 *       chats or getting a new chatsData reference does NOT re-hydrate.
 *   (b) updateChat.mutate fires whenever chatOverrides[chatId] changes to a
 *       new, defined value (and does NOT re-fire when the value is
 *       unchanged — the prevReasoningRef guard).
 *   (c) updateChat.mutate does NOT fire in the same render/effect pass that
 *       first calls hydrateFromChats: the persist effect only ever reads
 *       chatOverrides as captured by ITS OWN render's closure, so
 *       hydrateFromChats's store mutation cannot retroactively affect the
 *       persist effect already scheduled for that same commit.
 *
 * The hook reads useChatSettingsStore() internally (not injectable), so the
 * store module is mocked and chatOverrides is driven via a plain mutable
 * variable flipped between renders — same pattern test_Chat.spec.tsx already
 * uses for mocking useSSE's mutable state.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useReasoningOverridePersistence } from "@/hooks/useReasoningOverridePersistence";
import type { ReasoningOverridePersistMutation } from "@/hooks/useReasoningOverridePersistence";
import type { ChatSummary } from "@/hooks/useChats";

// ─── Mock the Zustand store the hook reads internally ────────────────────────

const hydrateFromChats = vi.fn();
let mockChatOverrides: Record<number, string> = {};

vi.mock("@/stores/chatSettingsStore", () => ({
  useChatSettingsStore: () => ({
    hydrateFromChats,
    chatOverrides: mockChatOverrides,
  }),
}));

// ─── Helpers ──────────────────────────────────────────────────────────────────

function makeChat(id: number, reasoningEffort?: string): ChatSummary {
  return {
    id,
    title: `Chat ${String(id)}`,
    folder: null,
    pinned: false,
    updated_at: "2026-07-16T00:00:00Z",
    model_id: null,
    display_order: 0,
    tags: [],
    archived_at: null,
    // `settings` is optional — omit the key entirely rather than assign it
    // an explicit `undefined` (exactOptionalPropertyTypes treats those
    // differently; ChatSummary.settings?: never declares `| undefined`).
    ...(reasoningEffort === undefined
      ? {}
      : { settings: { reasoning_effort: reasoningEffort } }),
  };
}

function makeUpdateChat(mutate: (body: unknown) => void = vi.fn()): ReasoningOverridePersistMutation {
  return { mutate } as unknown as ReasoningOverridePersistMutation;
}

interface HookProps {
  chatId: number | null;
  chatsData: ChatSummary[] | undefined;
}

describe("useReasoningOverridePersistence", () => {
  beforeEach(() => {
    hydrateFromChats.mockClear();
    mockChatOverrides = {};
  });

  it("(a) hydrates from chatsData exactly once, even across chatId/chatsData changes", () => {
    const chatsData1 = [makeChat(1, "high")];
    const updateChat = makeUpdateChat();

    const { rerender } = renderHook(
      (props: HookProps) => { useReasoningOverridePersistence({ ...props, updateChat }); },
      { initialProps: { chatId: 1, chatsData: chatsData1 } },
    );

    expect(hydrateFromChats).toHaveBeenCalledTimes(1);
    expect(hydrateFromChats).toHaveBeenCalledWith(chatsData1);

    // Switch chats AND supply a brand-new chatsData reference — hydratedRef
    // never resets per-chat, so hydrateFromChats must NOT fire again.
    const chatsData2 = [makeChat(1, "high"), makeChat(2, "medium")];
    rerender({ chatId: 2, chatsData: chatsData2 });
    expect(hydrateFromChats).toHaveBeenCalledTimes(1);

    // Yet another new chatsData reference — still exactly once, total.
    rerender({ chatId: 2, chatsData: [...chatsData2] });
    expect(hydrateFromChats).toHaveBeenCalledTimes(1);
  });

  it("(b) PATCHes via updateChat.mutate when the override changes after hydration", () => {
    const mutate = vi.fn();
    const updateChat = makeUpdateChat(mutate);

    const { rerender } = renderHook(
      (props: HookProps) => { useReasoningOverridePersistence({ ...props, updateChat }); },
      { initialProps: { chatId: 1, chatsData: undefined } },
    );

    expect(mutate).not.toHaveBeenCalled();

    // The override becomes available for chat 1 (e.g. after the store
    // settles post-hydration, or a user toggle elsewhere) — a fresh render
    // now sees it.
    mockChatOverrides = { 1: "high" };
    rerender({ chatId: 1, chatsData: undefined });
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate).toHaveBeenCalledWith({ reasoning_effort: "high" });

    // Re-render with the SAME override value — must NOT re-fire (the hook
    // only PATCHes when the value actually changes).
    rerender({ chatId: 1, chatsData: undefined });
    expect(mutate).toHaveBeenCalledTimes(1);

    // Change the override to a new value — fires again with the new value.
    mockChatOverrides = { 1: "medium" };
    rerender({ chatId: 1, chatsData: undefined });
    expect(mutate).toHaveBeenCalledTimes(2);
    expect(mutate).toHaveBeenLastCalledWith({ reasoning_effort: "medium" });
  });

  it("(c) does NOT PATCH in the initial render/effect pass where hydration also first runs", () => {
    const mutate = vi.fn();
    const updateChat = makeUpdateChat(mutate);

    // chatsData already carries a persisted reasoning_effort for this chat,
    // but chatOverrides (the store slice the persist effect reads) is still
    // {} for THIS render — hydrateFromChats is mocked, so it does not
    // synchronously populate chatOverrides within the same closure, exactly
    // mirroring the real store (a set() call only affects the NEXT render,
    // never effect closures already captured for the current one).
    const chatsData = [makeChat(1, "high")];

    renderHook(() => {
      useReasoningOverridePersistence({ chatId: 1, chatsData, updateChat });
    });

    expect(hydrateFromChats).toHaveBeenCalledTimes(1);
    expect(mutate).not.toHaveBeenCalled();
  });
});
