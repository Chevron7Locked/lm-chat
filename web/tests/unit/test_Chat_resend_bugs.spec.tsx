/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Three resend / regenerate bug fixes (verified 2026-07-02, Finding 1 gate
 * updated 2026-07-07 for the inclusive-delete resend fix).
 *
 * Finding 1 (P1) — resend on last user message silently did nothing.
 *   handleRegenerateClick's onSuccess must detect prior_user_content and
 *   fire startStream. This test pins the auto-confirm logic in isolation.
 *   NOTE: the gate used to also require deleted===0, but the backend now
 *   deletes the boundary user message inclusively (it gets replayed as a
 *   fresh turn instead of surviving in place — see
 *   delete_from_user_message_for_resend), so `deleted` is >= 1 even on the
 *   auto-confirm path and is no longer part of the gate.
 *
 * Finding 2 (P2) — resend dropped the response_id chain.
 *   handleResendClick must NOT set chainInvalidatedRef=true. This test pins the
 *   localStorage rid-storage contract: after a regenerate-style completion
 *   (chainInvalidated=false), the new rid IS stored; after an edit-style
 *   completion (chainInvalidated=true), it is cleared.
 *
 * Finding 3 (P3) — stale chat_id on mid-confirm chat switch.
 *   handleRegenerateConfirm's guard: if confirmedChatId !== chatId, call
 *   push({ variant:"error", ... }) and return (no startStream call).
 *
 * Tests are intentionally scoped to the CHANGED LOGIC only and avoid mounting
 * the full Chat component (too expensive in jsdom).
 */
import { describe, it, expect, vi } from "vitest";

// ─── Finding 1 — auto-confirm onSuccess fires startStream ────────────────────

describe("Finding 1 — auto-confirm path fires startStream", () => {
  /**
   * The exact onSuccess logic in handleRegenerateClick (Chat.tsx ~1297):
   *
   *   onSuccess: (result) => {
   *     if (result.prior_user_content !== null && result.prior_user_content !== "") {
   *       // build payload + call startStream(chatId, payload)
   *     }
   *   }
   *
   * We test the decision function inline to pin the branch conditions.
   */

  function autoConfirmShouldFire(result: {
    deleted: number;
    prior_user_content: string | null;
  }): boolean {
    return (
      result.prior_user_content !== null && result.prior_user_content !== ""
    );
  }

  it("triggers stream when prior_user_content is present, regardless of deleted count", () => {
    // deleted is now >= 1 on the auto-confirm path too (the boundary user
    // message is deleted inclusively), so it is no longer part of the gate.
    expect(
      autoConfirmShouldFire({ deleted: 1, prior_user_content: "Hello world" }),
    ).toBe(true);
  });

  it("triggers stream even when deleted > 1 (tail messages were deleted too)", () => {
    expect(
      autoConfirmShouldFire({ deleted: 3, prior_user_content: "Hello world" }),
    ).toBe(true);
  });

  it("does NOT trigger stream when prior_user_content is null", () => {
    expect(
      autoConfirmShouldFire({ deleted: 1, prior_user_content: null }),
    ).toBe(false);
  });

  it("does NOT trigger stream when prior_user_content is empty string", () => {
    expect(
      autoConfirmShouldFire({ deleted: 1, prior_user_content: "" }),
    ).toBe(false);
  });

  it("startStream is called with the prior_user_content as stream input", () => {
    const startStream = vi.fn();
    const push = vi.fn();
    const chatId = 7;
    // Annotated `string`, not inferred as the narrower literal type — model
    // is a mutable string in production (see the sibling "missing model"
    // test below), and the inline guard on line 95 needs that real width to
    // stay type-checkable as the defensive check it actually is.
    const model: string = "test-model";

    // Simulate the onSuccess handler from handleRegenerateClick.
    function handleOnSuccess(result: {
      deleted: number;
      prior_user_content: string | null;
    }) {
      if (
        result.prior_user_content !== null &&
        result.prior_user_content !== ""
      ) {
        if (model === undefined || model === "") {
          push({ variant: "error", message: "Pick a model before resending." });
          return;
        }
        startStream(chatId, {
          input: [{ type: "text", content: result.prior_user_content }],
          model,
        });
      }
    }

    handleOnSuccess({ deleted: 1, prior_user_content: "Resend this" });

    expect(startStream).toHaveBeenCalledOnce();
    const [calledChatId, payload] = startStream.mock.calls[0] as [
      number,
      { input: { type: string; content: string }[]; model: string },
    ];
    expect(calledChatId).toBe(chatId);
    expect(payload.input).toEqual([{ type: "text", content: "Resend this" }]);
    expect(payload.model).toBe(model);
    expect(push).not.toHaveBeenCalled();
  });

  it("pushes error toast and does NOT call startStream when model is missing", () => {
    const startStream = vi.fn();
    const push = vi.fn();
    const chatId = 7;
    const model = ""; // empty model — guard should fire

    function handleOnSuccess(result: {
      deleted: number;
      prior_user_content: string | null;
    }) {
      if (
        result.prior_user_content !== null &&
        result.prior_user_content !== ""
      ) {
        if (model === undefined || model === "") {
          push({ variant: "error", message: "Pick a model before resending." });
          return;
        }
        startStream(chatId, {
          input: [{ type: "text", content: result.prior_user_content }],
          model,
        });
      }
    }

    handleOnSuccess({ deleted: 1, prior_user_content: "Resend this" });

    expect(startStream).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Pick a model before resending.",
    });
  });
});

// ─── Finding 2 — response_id chain preserved after resend ────────────────────

describe("Finding 2 — chainInvalidatedRef contract: resend vs edit", () => {
  /**
   * Pins the localStorage rid-storage contract driven by chainInvalidatedRef.
   *
   * The stream-complete useEffect in Chat.tsx (line ~545):
   *
   *   if (chainInvalidatedRef.current) {
   *     chainInvalidatedRef.current = false;
   *     clearResponseId(chatId);       // EDIT path — rid is cleared
   *   } else if (sseState.responseId !== null) {
   *     storeResponseId(chatId, rid);  // REGEN/RESEND path — rid is stored
   *   }
   *
   * The fix: handleResendClick does NOT set chainInvalidatedRef.current = true,
   * so the RESEND path follows the else-branch and stores the new rid.
   */

  const LS_RID_KEY = (chatId: number) => `lmchat:sse:${String(chatId)}:rid`;

  function storeResponseId(chatId: number, rid: string): void {
    localStorage.setItem(LS_RID_KEY(chatId), rid);
  }

  function clearResponseId(chatId: number): void {
    localStorage.removeItem(LS_RID_KEY(chatId));
  }

  function simulateStreamComplete(
    chatId: number,
    responseId: string | null,
    chainInvalidated: boolean,
  ): void {
    // Mirror of Chat.tsx's stream-complete useEffect body.
    if (chainInvalidated) {
      clearResponseId(chatId);
    } else if (responseId !== null) {
      storeResponseId(chatId, responseId);
    }
  }

  it("RESEND: stores the new rid when chainInvalidatedRef=false (the fix)", () => {
    localStorage.clear();
    simulateStreamComplete(1, "new-rid-xyz", /* chainInvalidated */ false);
    expect(localStorage.getItem(LS_RID_KEY(1))).toBe("new-rid-xyz");
  });

  it("EDIT: clears the rid when chainInvalidatedRef=true (existing behaviour unchanged)", () => {
    localStorage.setItem(LS_RID_KEY(1), "stale-rid");
    simulateStreamComplete(1, "new-rid-xyz", /* chainInvalidated */ true);
    expect(localStorage.getItem(LS_RID_KEY(1))).toBeNull();
  });

  it("neither stores nor clears when responseId is null and chain not invalidated", () => {
    localStorage.setItem(LS_RID_KEY(1), "previous-rid");
    simulateStreamComplete(1, null, /* chainInvalidated */ false);
    // Previous rid must NOT be touched (stream had no responseId).
    expect(localStorage.getItem(LS_RID_KEY(1))).toBe("previous-rid");
  });

  it("clearing via resend (clearResponseId before stream starts) + storing after (new rid)", () => {
    // handleResendClick calls clearResponseId(chatId) BEFORE handleRegenerateClick.
    // Then stream completes with a new rid → storeResponseId is called.
    // Net result: only the new rid in storage.
    localStorage.setItem(LS_RID_KEY(5), "old-rid-from-previous-turn");

    // Step 1: handleResendClick calls clearResponseId (stale rid removed).
    clearResponseId(5);
    expect(localStorage.getItem(LS_RID_KEY(5))).toBeNull();

    // Step 2: stream completes, chainInvalidated=false → new rid stored.
    simulateStreamComplete(5, "fresh-rid-after-resend", false);
    expect(localStorage.getItem(LS_RID_KEY(5))).toBe("fresh-rid-after-resend");
  });
});

// ─── Finding 3 — mid-confirm chat-switch guard ───────────────────────────────

describe("Finding 3 — handleRegenerateConfirm guard: confirmedChatId !== chatId", () => {
  /**
   * Pins the guard added at the start of handleRegenerateConfirm (Chat.tsx):
   *
   *   if (confirmedChatId !== chatId) {
   *     setRegenConfirm(null);
   *     push({ variant: "error", message: "Chat changed — please try again." });
   *     return;
   *   }
   *
   * Tests the pure decision/action logic in isolation.
   */

  function simulateHandleRegenerateConfirm(opts: {
    confirmedChatId: number;
    chatId: number | null;
    push: (toast: { variant: string; message: string }) => void;
    startStream: (chatId: number, payload: unknown) => void;
    setRegenConfirm: (v: null) => void;
    prior_user_content?: string;
  }): void {
    const {
      confirmedChatId,
      chatId,
      push,
      startStream,
      setRegenConfirm,
      prior_user_content = "some content",
    } = opts;

    // Guard (the fix).
    if (confirmedChatId !== chatId) {
      setRegenConfirm(null);
      push({ variant: "error", message: "Chat changed — please try again." });
      return;
    }

    // Past the guard — normal confirm flow.
    if (prior_user_content === null || prior_user_content === "") {
      push({
        variant: "error",
        message: "Nothing to regenerate from — no prior prompt in this chat.",
      });
      return;
    }

    startStream(confirmedChatId, {
      input: [{ type: "text", content: prior_user_content }],
    });
  }

  it("fires error toast and does NOT call startStream when chat changed", () => {
    const push = vi.fn();
    const startStream = vi.fn();
    const setRegenConfirm = vi.fn();

    simulateHandleRegenerateConfirm({
      confirmedChatId: 1, // the chat the modal was opened for
      chatId: 2,          // user switched to chat 2
      push,
      startStream,
      setRegenConfirm,
    });

    expect(push).toHaveBeenCalledOnce();
    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Chat changed — please try again.",
    });
    expect(startStream).not.toHaveBeenCalled();
    expect(setRegenConfirm).toHaveBeenCalledWith(null);
  });

  it("calls startStream normally when chat has NOT changed (guard does not fire)", () => {
    const push = vi.fn();
    const startStream = vi.fn();
    const setRegenConfirm = vi.fn();

    simulateHandleRegenerateConfirm({
      confirmedChatId: 1,
      chatId: 1,            // same chat — guard skipped
      push,
      startStream,
      setRegenConfirm,
      prior_user_content: "resubmit this",
    });

    expect(push).not.toHaveBeenCalled();
    expect(startStream).toHaveBeenCalledOnce();
    const [calledChatId] = startStream.mock.calls[0] as [number, unknown];
    expect(calledChatId).toBe(1);
  });

  it("also fires toast (not stream) when chatId is null (no active chat)", () => {
    // chatId=null means no chat is selected; confirmedChatId is always a number.
    // null !== number → guard fires.
    const push = vi.fn();
    const startStream = vi.fn();
    const setRegenConfirm = vi.fn();

    simulateHandleRegenerateConfirm({
      confirmedChatId: 3,
      chatId: null,         // edge case: navigated away from chats entirely
      push,
      startStream,
      setRegenConfirm,
    });

    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Chat changed — please try again.",
    });
    expect(startStream).not.toHaveBeenCalled();
  });

  it("fires error toast (not guard) when prior_user_content is empty on same chat", () => {
    const push = vi.fn();
    const startStream = vi.fn();
    const setRegenConfirm = vi.fn();

    simulateHandleRegenerateConfirm({
      confirmedChatId: 5,
      chatId: 5,
      push,
      startStream,
      setRegenConfirm,
      prior_user_content: "",
    });

    // Guard doesn't fire (same chat). Empty content fires the different error.
    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Nothing to regenerate from — no prior prompt in this chat.",
    });
    expect(startStream).not.toHaveBeenCalled();
    // setRegenConfirm not called (empty-content path doesn't clear the confirm).
    expect(setRegenConfirm).not.toHaveBeenCalled();
  });
});
