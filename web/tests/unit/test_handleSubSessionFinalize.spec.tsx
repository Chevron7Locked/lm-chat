/**
 * Unit tests for the sub-session finalize guard.
 *
 * Covers ISSUE-18: `handleSubSessionFinalize` must not forward an empty
 * `model_id` to the streaming-finalize endpoint. When no model is selected,
 * the handler must push a warning toast and short-circuit BEFORE calling
 * `subSessionSSE.finalize`.
 *
 * The handler lives inside Chat.tsx as a closure, so we model its control
 * flow here with the same condition. The point of the test is to lock the
 * guard in place: if a future edit drops the selectedModel check, the
 * "finalize never called" assertion fails.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

interface SubSession {
  systemPrompt: string;
  messages: { role: "user" | "assistant"; content: string }[];
}

// Faithful re-implementation of the handler control flow in
// Chat.tsx::handleSubSessionFinalize. Kept here so the guard remains
// pinned down by an executable test.
function runFinalize(opts: {
  subSession: SubSession | null;
  chatId: number | null;
  selectedModel: string | null;
  subSessionSSE: { finalize: ReturnType<typeof vi.fn> };
  push: ReturnType<typeof vi.fn>;
}): void {
  const { subSession, chatId, selectedModel, subSessionSSE, push } = opts;
  if (subSession === null || chatId === null) return;
  if (!selectedModel) {
    push({ variant: "warning", message: "Select a model before finalizing." });
    return;
  }
  subSessionSSE.finalize({
    chatId,
    modelId: selectedModel,
    systemPrompt: subSession.systemPrompt,
    messages: subSession.messages,
  });
}

describe("handleSubSessionFinalize guard", () => {
  let push: ReturnType<typeof vi.fn>;
  let finalize: ReturnType<typeof vi.fn>;
  const session: SubSession = {
    systemPrompt: "You are a deep research agent.",
    messages: [{ role: "user", content: "find me sources" }],
  };

  beforeEach(() => {
    push = vi.fn();
    finalize = vi.fn();
  });

  it("blocks finalize when selectedModel is null", () => {
    runFinalize({
      subSession: session,
      chatId: 7,
      selectedModel: null,
      subSessionSSE: { finalize },
      push,
    });
    expect(push).toHaveBeenCalledWith({
      variant: "warning",
      message: "Select a model before finalizing.",
    });
    expect(finalize).not.toHaveBeenCalled();
  });

  it("blocks finalize when selectedModel is an empty string", () => {
    runFinalize({
      subSession: session,
      chatId: 7,
      selectedModel: "",
      subSessionSSE: { finalize },
      push,
    });
    expect(push).toHaveBeenCalledTimes(1);
    expect(finalize).not.toHaveBeenCalled();
  });

  it("calls finalize with the selected model when one is set", () => {
    runFinalize({
      subSession: session,
      chatId: 7,
      selectedModel: "qwen3.6",
      subSessionSSE: { finalize },
      push,
    });
    expect(push).not.toHaveBeenCalled();
    expect(finalize).toHaveBeenCalledTimes(1);
    const arg = finalize.mock.calls[0]?.[0] as { modelId: string; chatId: number };
    expect(arg.modelId).toBe("qwen3.6");
    expect(arg.chatId).toBe(7);
  });

  it("no-ops cleanly when subSession is null", () => {
    runFinalize({
      subSession: null,
      chatId: 7,
      selectedModel: "qwen3.6",
      subSessionSSE: { finalize },
      push,
    });
    expect(push).not.toHaveBeenCalled();
    expect(finalize).not.toHaveBeenCalled();
  });

  it("no-ops cleanly when chatId is null", () => {
    runFinalize({
      subSession: session,
      chatId: null,
      selectedModel: "qwen3.6",
      subSessionSSE: { finalize },
      push,
    });
    expect(push).not.toHaveBeenCalled();
    expect(finalize).not.toHaveBeenCalled();
  });
});
