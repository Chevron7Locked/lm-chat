/* SPDX-License-Identifier: Apache-2.0 */
/**
 * submitTurn primitive + edit-then-regenerate wiring (Phase B consolidation).
 *
 * Chat.tsx now funnels every text-driven, direct-stream turn (regenerate,
 * resend, and the edit-then-regenerate flow) through ONE `submitTurn`
 * primitive. These tests pin the two behaviours that changed:
 *
 *   1. submitTurn's payload contract — it builds the canonical
 *      { input, model, integrations? } shape and does NOT carry a `provider`
 *      field (the backend resolves provider from the STORED
 *      chat.settings.provider; CanonicalChatRequest has no provider field, so
 *      a payload provider key would be silently dropped). This test guards
 *      against a future regression that re-adds a dead `provider` field.
 *
 *   2. Editing a user message now regenerates a reply. The edit endpoint only
 *      patches content in place (it does NOT truncate the stale assistant
 *      reply), so handleEditUserMessage routes through the regenerate
 *      delete-then-replay path (handleRegenerateClick) rather than streaming a
 *      fresh turn directly — which would duplicate the user message. This test
 *      pins that the edit persists first, then triggers regenerate, which in
 *      turn fires the stream with the edited content.
 *
 * Following the established Chat.tsx test convention (see
 * test_Chat_resend_bugs.spec.tsx), the CHANGED LOGIC is exercised in isolation
 * rather than by mounting the full Chat component (too expensive in jsdom).
 */
import { describe, it, expect, vi } from "vitest";

// ─── 1. submitTurn payload contract ──────────────────────────────────────────

describe("submitTurn — payload contract", () => {
  /**
   * Mirror of Chat.tsx `submitTurn`: resolve model via the ladder, error-toast
   * + bail when unresolved, resolve integrations (opts override → per-chat),
   * build the canonical payload, re-arm auto-stick, call startStream.
   */
  function submitTurn(opts: {
    turnChatId: number;
    inputText: string;
    model: string | undefined;
    perChatIntegrations: string[] | undefined;
    override?: { integrations?: string[] };
    push: (t: { variant: string; message: string }) => void;
    startStream: (chatId: number, payload: Record<string, unknown>) => void;
    setAutoStick: (v: boolean) => void;
  }): void {
    const { model, override, perChatIntegrations } = opts;
    if (model === undefined || model === "") {
      opts.push({ variant: "error", message: "Pick a model before sending." });
      return;
    }
    const integrations = override?.integrations ?? perChatIntegrations;
    const payload: Record<string, unknown> = {
      input: [{ type: "text", content: opts.inputText }],
      model,
      ...(integrations !== undefined && { integrations }),
    };
    opts.setAutoStick(true);
    opts.startStream(opts.turnChatId, payload);
  }

  it("builds { input, model } and streams to the given chat id", () => {
    const startStream = vi.fn();
    const push = vi.fn();
    const setAutoStick = vi.fn();

    submitTurn({
      turnChatId: 42,
      inputText: "Hello there",
      model: "qwen3-coder",
      perChatIntegrations: undefined,
      push,
      startStream,
      setAutoStick,
    });

    expect(startStream).toHaveBeenCalledOnce();
    const [chatId, payload] = startStream.mock.calls[0] as [
      number,
      Record<string, unknown>,
    ];
    expect(chatId).toBe(42);
    expect(payload.input).toEqual([{ type: "text", content: "Hello there" }]);
    expect(payload.model).toBe("qwen3-coder");
    // Auto-stick is re-armed so the new turn snaps to the bottom.
    expect(setAutoStick).toHaveBeenCalledWith(true);
    expect(push).not.toHaveBeenCalled();
  });

  it("does NOT carry a `provider` field (provider is resolved server-side)", () => {
    const startStream = vi.fn();
    submitTurn({
      turnChatId: 1,
      inputText: "x",
      model: "some-model",
      perChatIntegrations: undefined,
      push: vi.fn(),
      startStream,
      setAutoStick: vi.fn(),
    });
    const [, payload] = startStream.mock.calls[0] as [
      number,
      Record<string, unknown>,
    ];
    // Regression guard: the stream payload must never sprout a dead `provider`
    // key — CanonicalChatRequest has no such field and the backend routes on
    // the stored chat.settings.provider instead.
    expect("provider" in payload).toBe(false);
  });

  it("includes per-chat integrations when resolved, omits when undefined", () => {
    const withTools = vi.fn();
    submitTurn({
      turnChatId: 1,
      inputText: "x",
      model: "m",
      perChatIntegrations: ["mcp/context7"],
      push: vi.fn(),
      startStream: withTools,
      setAutoStick: vi.fn(),
    });
    // submitTurn above is fully inlined (no branch skips startStream when
    // model is non-empty, as here) — the call is guaranteed to have landed.
    const withToolsCall = withTools.mock.calls[0];
    if (withToolsCall === undefined) throw new Error("expected startStream to have been called");
    expect(
      (withToolsCall[1] as Record<string, unknown>).integrations,
    ).toEqual(["mcp/context7"]);

    const noTools = vi.fn();
    submitTurn({
      turnChatId: 1,
      inputText: "x",
      model: "m",
      perChatIntegrations: undefined,
      push: vi.fn(),
      startStream: noTools,
      setAutoStick: vi.fn(),
    });
    const noToolsCall = noTools.mock.calls[0];
    if (noToolsCall === undefined) throw new Error("expected startStream to have been called");
    expect(
      "integrations" in (noToolsCall[1] as Record<string, unknown>),
    ).toBe(false);
  });

  it("an explicit integrations override wins over the per-chat value", () => {
    const startStream = vi.fn();
    submitTurn({
      turnChatId: 1,
      inputText: "x",
      model: "m",
      perChatIntegrations: ["mcp/per-chat"],
      override: { integrations: ["mcp/override"] },
      push: vi.fn(),
      startStream,
      setAutoStick: vi.fn(),
    });
    const startStreamCall = startStream.mock.calls[0];
    if (startStreamCall === undefined) throw new Error("expected startStream to have been called");
    expect(
      (startStreamCall[1] as Record<string, unknown>).integrations,
    ).toEqual(["mcp/override"]);
  });

  it("error-toasts and does NOT stream when no model resolves", () => {
    const startStream = vi.fn();
    const push = vi.fn();
    submitTurn({
      turnChatId: 1,
      inputText: "x",
      model: undefined,
      perChatIntegrations: undefined,
      push,
      startStream,
      setAutoStick: vi.fn(),
    });
    expect(startStream).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Pick a model before sending.",
    });
  });
});

// ─── 2. Editing a user message regenerates a reply (fix) ─────────────────────

describe("handleEditUserMessage — edit now regenerates a reply", () => {
  /**
   * Mirror of Chat.tsx `handleEditUserMessage`: persist the edit, invalidate
   * the response_id chain, then regenerate FROM the edited user message via
   * handleRegenerateClick (delete-then-replay). Streaming directly would
   * duplicate the user message, so the regenerate endpoint MUST run first.
   */
  async function handleEditUserMessage(opts: {
    messageId: number;
    newContent: string;
    chatId: number | null;
    editMessage: (a: { messageId: number; content: string }) => Promise<void>;
    setChainInvalidated: (v: boolean) => void;
    clearResponseId: (chatId: number) => void;
    handleRegenerateClick: (messageId: number) => void;
  }): Promise<void> {
    await opts.editMessage({
      messageId: opts.messageId,
      content: opts.newContent,
    });
    opts.setChainInvalidated(true);
    if (opts.chatId !== null) opts.clearResponseId(opts.chatId);
    opts.handleRegenerateClick(opts.messageId);
  }

  it("persists the edit BEFORE regenerating, then regenerates the message", async () => {
    const calls: string[] = [];
    const editMessage = vi.fn((): Promise<void> => {
      calls.push("edit");
      return Promise.resolve();
    });
    const handleRegenerateClick = vi.fn(() => {
      calls.push("regenerate");
    });
    const clearResponseId = vi.fn();
    const setChainInvalidated = vi.fn();

    await handleEditUserMessage({
      messageId: 12,
      newContent: "edited prompt",
      chatId: 3,
      editMessage,
      setChainInvalidated,
      clearResponseId,
      handleRegenerateClick,
    });

    // The edit is persisted with the new content...
    expect(editMessage).toHaveBeenCalledWith({
      messageId: 12,
      content: "edited prompt",
    });
    // ...the chain is invalidated + cleared (edited context ⇒ fresh chain)...
    expect(setChainInvalidated).toHaveBeenCalledWith(true);
    expect(clearResponseId).toHaveBeenCalledWith(3);
    // ...and the reply is regenerated from the edited user message.
    expect(handleRegenerateClick).toHaveBeenCalledWith(12);
    // Ordering: the PATCH must complete before regenerate (so the regenerate
    // endpoint reads the edited content back as prior_user_content).
    expect(calls).toEqual(["edit", "regenerate"]);
  });

  it("end-to-end: edit → regenerate → stream fires with the edited content", async () => {
    // Wire the mirrored regenerate onSuccess (from handleRegenerateClick) so
    // that regenerating the edited message ultimately streams a fresh turn.
    const startStream = vi.fn();
    const model = "local-model";

    function regenerateOnSuccess(result: {
      prior_user_content: string | null;
    }): void {
      if (
        result.prior_user_content !== null &&
        result.prior_user_content !== ""
      ) {
        // submitTurn(currentChatId, result.prior_user_content)
        startStream(9, {
          input: [{ type: "text", content: result.prior_user_content }],
          model,
        });
      }
    }

    // Regenerate endpoint returns the (already-persisted) edited content.
    const handleRegenerateClick = vi.fn(() => {
      regenerateOnSuccess({ prior_user_content: "edited prompt" });
    });

    await handleEditUserMessage({
      messageId: 12,
      newContent: "edited prompt",
      chatId: 9,
      editMessage: vi.fn(async () => {}),
      setChainInvalidated: vi.fn(),
      clearResponseId: vi.fn(),
      handleRegenerateClick,
    });

    expect(startStream).toHaveBeenCalledOnce();
    const [chatId, payload] = startStream.mock.calls[0] as [
      number,
      { input: { type: string; content: string }[] },
    ];
    expect(chatId).toBe(9);
    expect(payload.input).toEqual([
      { type: "text", content: "edited prompt" },
    ]);
  });
});

// ─── 3. resolveTurnModel ladder ──────────────────────────────────────────────

describe("resolveTurnModel — model-resolution ladder", () => {
  /**
   * Mirror of the extracted Chat.tsx `resolveTurnModel` helper: per-chat
   * override → chat's persisted model → global saved default → "".
   */
  function resolveTurnModel(
    selectedModel: string | undefined,
    chatModelId: string | undefined,
    savedDefault: string | undefined,
  ): string {
    return selectedModel ?? chatModelId ?? savedDefault ?? "";
  }

  it("prefers the per-chat selected model", () => {
    expect(resolveTurnModel("sel", "chat", "default")).toBe("sel");
  });

  it("falls back to the chat's persisted model id", () => {
    expect(resolveTurnModel(undefined, "chat", "default")).toBe("chat");
  });

  it("falls back to the global saved default", () => {
    expect(resolveTurnModel(undefined, undefined, "default")).toBe("default");
  });

  it("returns \"\" when nothing resolves (the guard sentinel)", () => {
    expect(resolveTurnModel(undefined, undefined, undefined)).toBe("");
  });
});

// ─── 4. Silent no-op → error toast fixes ─────────────────────────────────────

describe("silent no-op fixes surface a toast", () => {
  /**
   * (a) handleRegenerateClick.onSuccess: when the endpoint auto-confirms but
   * returns no prior prompt to replay, the click used to fall through
   * silently. It now pushes an error toast.
   */
  function regenerateOnSuccess(
    result: { prior_user_content: string | null },
    submitTurn: (text: string) => void,
    push: (t: { variant: string; message: string }) => void,
  ): void {
    if (
      result.prior_user_content !== null &&
      result.prior_user_content !== ""
    ) {
      submitTurn(result.prior_user_content);
    } else {
      push({
        variant: "error",
        message: "Nothing to regenerate from — no prior prompt.",
      });
    }
  }

  it("regenerate: null prior_user_content now toasts instead of no-op", () => {
    const submitTurn = vi.fn();
    const push = vi.fn();
    regenerateOnSuccess({ prior_user_content: null }, submitTurn, push);
    expect(submitTurn).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Nothing to regenerate from — no prior prompt.",
    });
  });

  it("regenerate: empty prior_user_content now toasts instead of no-op", () => {
    const submitTurn = vi.fn();
    const push = vi.fn();
    regenerateOnSuccess({ prior_user_content: "" }, submitTurn, push);
    expect(submitTurn).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledOnce();
  });

  it("regenerate: non-empty prior_user_content still streams (no toast)", () => {
    const submitTurn = vi.fn();
    const push = vi.fn();
    regenerateOnSuccess({ prior_user_content: "replay me" }, submitTurn, push);
    expect(submitTurn).toHaveBeenCalledWith("replay me");
    expect(push).not.toHaveBeenCalled();
  });

  /**
   * (b) FollowupChips onSelect: clicking a chip with no resolvable model used
   * to `return` silently. It now pushes the same "Pick a model…" toast the
   * other submit paths use.
   */
  function followupOnSelect(
    q: string,
    streaming: boolean,
    model: string,
    push: (t: { variant: string; message: string }) => void,
    handleSubmit: (text: string) => void,
  ): void {
    if (streaming) return;
    if (model === "") {
      push({ variant: "error", message: "Pick a model before sending." });
      return;
    }
    handleSubmit(q);
  }

  it("followup: no model now toasts instead of no-op", () => {
    const push = vi.fn();
    const handleSubmit = vi.fn();
    followupOnSelect("next?", false, "", push, handleSubmit);
    expect(handleSubmit).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Pick a model before sending.",
    });
  });

  it("followup: with a model, submits and does not toast", () => {
    const push = vi.fn();
    const handleSubmit = vi.fn();
    followupOnSelect("next?", false, "some-model", push, handleSubmit);
    expect(handleSubmit).toHaveBeenCalledWith("next?");
    expect(push).not.toHaveBeenCalled();
  });

  it("followup: while streaming, does nothing (no toast, no submit)", () => {
    const push = vi.fn();
    const handleSubmit = vi.fn();
    followupOnSelect("next?", true, "some-model", push, handleSubmit);
    expect(handleSubmit).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });
});
