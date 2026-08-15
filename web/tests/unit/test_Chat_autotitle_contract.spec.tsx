/* SPDX-License-Identifier: Apache-2.0 */
/**
 * A-autotitle-verify — Chat autotitle swallow + no-retry contract.
 *
 * Tests the extracted useAutotitleEffect hook directly.  This is the
 * correct layer: the swallow and no-retry guards live in the hook
 * (web/src/hooks/useAutotitleEffect.ts), which is what Chat.tsx now
 * delegates to.  Testing the hook directly avoids the full Chat.tsx
 * mount surface while precisely pinning the contracts.
 *
 * AC25 — rejection swallow: when mutateAsync rejects, no
 *         unhandledrejection fires and no React error boundary trips.
 * AC26 — no in-session retry: after a failed attempt, a second
 *         SSE-complete for the same chatId does NOT call mutateAsync again.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useRef } from "react";
import { useAutotitleEffect } from "@/hooks/useAutotitleEffect";
import type {
  AutotitleSSEState,
  AutotitleChat,
  AutotitleMessagesData,
  AutotitleMutation,
  AutotitleStoreCallbacks,
} from "@/hooks/useAutotitleEffect";

// ─── Helpers ──────────────────────────────────────────────────────────────────

function makeArgs(overrides: {
  status?: AutotitleSSEState["status"];
  chatId?: number | null;
  currentChat?: AutotitleChat | undefined;
  messagesData?: AutotitleMessagesData | undefined;
  mutateAsync?: AutotitleMutation["mutateAsync"];
  beginGenerating?: AutotitleStoreCallbacks["beginGenerating"];
  endGenerating?: AutotitleStoreCallbacks["endGenerating"];
  titleAttemptedSet?: Set<number>;
}) {
  const {
    status = "complete",
    chatId = 7,
    currentChat = { title: "New Chat" },
    messagesData = { messages: [{ role: "user" }, { role: "assistant", content: "Sure." }] },
    mutateAsync = vi.fn().mockResolvedValue({ title: "Cool" }),
    beginGenerating = vi.fn(),
    endGenerating = vi.fn(),
    titleAttemptedSet = new Set<number>(),
  } = overrides;

  return {
    chatId,
    // chatId mirrors the destructured `chatId` above by default so the
    // ordinary (same-chat) case matches out of the box — see
    // AutotitleSSEState.chatId / StreamState.chatId.
    sseState: { status, chatId } as AutotitleSSEState,
    currentChat,
    messagesData,
    mutation: { mutateAsync } as AutotitleMutation,
    store: { beginGenerating, endGenerating } as AutotitleStoreCallbacks,
    // We can't call useRef inside a plain function, so we pass the Set
    // separately and the hook wrapper below creates the ref from it.
    _titleAttemptedSet: titleAttemptedSet,
  };
}

/**
 * Wrapper hook that creates the ref and delegates to useAutotitleEffect.
 * This lets us test the hook without mounting Chat.tsx.
 */
function useTestHarness(args: ReturnType<typeof makeArgs>) {
  const titleAttemptedRef = useRef<Set<number>>(args._titleAttemptedSet);
  useAutotitleEffect({
    chatId: args.chatId,
    sseState: args.sseState,
    currentChat: args.currentChat,
    messagesData: args.messagesData,
    mutation: args.mutation,
    store: args.store,
    titleAttemptedRef,
  });
  return { titleAttemptedRef };
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("useAutotitleEffect — AC25: rejection is swallowed", () => {
  let unhandledRejections: PromiseRejectionEvent[] = [];
  let handler: (e: PromiseRejectionEvent) => void;

  beforeEach(() => {
    unhandledRejections = [];
    handler = (e: PromiseRejectionEvent) => {
      unhandledRejections.push(e);
    };
    window.addEventListener("unhandledrejection", handler);
  });

  afterEach(() => {
    window.removeEventListener("unhandledrejection", handler);
  });

  it("does not fire unhandledrejection when mutateAsync rejects", async () => {
    const mutateAsync = vi.fn().mockRejectedValue(
      Object.assign(new Error("upstream 502"), { status: 502 })
    );

    const args = makeArgs({ mutateAsync });
    renderHook(() => useTestHarness(args));

    // Allow microtasks + effect to flush
    await act(async () => {
      await new Promise<void>((r) => setTimeout(r, 50));
    });

    expect(unhandledRejections).toHaveLength(0);
  });

  it("does not throw into a React error boundary when mutateAsync rejects", async () => {
    // If an error escapes the hook into React's render/commit cycle it would
    // propagate to the ErrorBoundary.  The easiest proxy: renderHook itself
    // does not throw (no `.rejects` needed — we just assert the render
    // completes without throwing synchronously or via act()).
    const mutateAsync = vi.fn().mockRejectedValue(new Error("network gone"));
    const args = makeArgs({ mutateAsync });

    // This must not throw — renderHook() itself throwing (synchronously or
    // via act()) would already fail this test before reaching the
    // assertion below; there is nothing left to check for that half of the
    // intent (the modern @testing-library/react renderHook() return value
    // has no `.error` property — that was the old @testing-library/
    // react-hooks API; `expect(result.error).toBeUndefined()` used to live
    // here but always passed vacuously, TS2339 on `.error` not existing).
    renderHook(() => useTestHarness(args));
    await act(async () => {
      await new Promise<void>((r) => setTimeout(r, 50));
    });

    expect(unhandledRejections).toHaveLength(0);
  });
});

describe("useAutotitleEffect — first-turn trigger (< 1 gate, audit ba1d324)", () => {
  it("fires after the FIRST assistant message (assistantCount === 1, not 2)", async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ title: "Cool" });
    const beginGenerating = vi.fn();
    const endGenerating = vi.fn();

    // Exactly ONE assistant message persisted — the pre-adoption inline copy
    // in Chat.tsx (assistantCount < 2 gate) would NOT have fired here.
    const args = makeArgs({
      mutateAsync,
      beginGenerating,
      endGenerating,
      messagesData: { messages: [{ role: "user" }, { role: "assistant", content: "Sure." }] },
    });
    renderHook(() => useTestHarness(args));

    await act(async () => {
      await new Promise<void>((r) => setTimeout(r, 50));
    });

    expect(mutateAsync).toHaveBeenCalledTimes(1);
    expect(mutateAsync).toHaveBeenCalledWith(7);
    expect(beginGenerating).toHaveBeenCalledWith(7);
    expect(endGenerating).toHaveBeenCalledWith(7);
  });

  it("does NOT fire with zero assistant messages", async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ title: "Cool" });

    const args = makeArgs({
      mutateAsync,
      messagesData: { messages: [{ role: "user" }] },
    });
    renderHook(() => useTestHarness(args));

    await act(async () => {
      await new Promise<void>((r) => setTimeout(r, 50));
    });

    expect(mutateAsync).not.toHaveBeenCalled();
  });
});

describe("useAutotitleEffect — AC26: no in-session retry after failure", () => {
  it("does NOT call mutateAsync a second time when sseState.complete fires again for the same chatId", async () => {
    const mutateAsync = vi.fn().mockRejectedValue(new Error("502"));

    const args = makeArgs({ mutateAsync });

    // First render — SSE complete triggers the first (failed) attempt.
    const { rerender } = renderHook(() => useTestHarness(args));
    await act(async () => {
      await new Promise<void>((r) => setTimeout(r, 50));
    });

    expect(mutateAsync).toHaveBeenCalledTimes(1);

    // Simulate a second "complete" transition for the same chatId by
    // toggling sseState through idle → complete.
    const idleArgs = makeArgs({
      mutateAsync,
      status: "idle",
      titleAttemptedSet: args._titleAttemptedSet, // same Set → same ref content
    });
    rerender(); // first rerender with same args (no-op effect since same status)

    // Now rerender with complete again using the same shared titleAttemptedSet.
    const completeArgs2 = makeArgs({
      mutateAsync,
      status: "complete",
      titleAttemptedSet: args._titleAttemptedSet,
    });

    // We need to switch the args the hook sees. Re-create the harness with the
    // shared Set so the ref carries over the "already attempted" state.
    const { rerender: rerender2 } = renderHook(
      (a: ReturnType<typeof makeArgs>) => useTestHarness(a),
      { initialProps: completeArgs2 }
    );

    await act(async () => {
      await new Promise<void>((r) => setTimeout(r, 50));
    });

    // The titleAttemptedSet was already populated by the first render, so
    // the second complete must NOT call mutateAsync again.
    // Total across both renderHook instances = still 1.
    expect(mutateAsync).toHaveBeenCalledTimes(1);

    void rerender2;
    void idleArgs;
  });
});
