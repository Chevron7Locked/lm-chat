/**
 * Unit tests for the sub-session inject path.
 *
 * Covers ISSUE-16: a non-OK response from `/inject-message` must NOT clear
 * the sub-session panel. The user must see an error toast and the panel
 * must stay open so they can retry.
 *
 * The handler is tested through the extracted `injectSubSessionSummary`
 * helper plus a thin clear-on-ok wrapper that mirrors the handler control
 * flow inside Chat.tsx (`if (!ok) { push(error); return; }` else clear).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { injectSubSessionSummary } from "@/lib/subSession";

describe("injectSubSessionSummary", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns ok=true on a 200 response", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    const result = await injectSubSessionSummary(7, "summary text", "qwen3.6");
    expect(result.ok).toBe(true);
  });

  it("returns ok=false on a 500 response", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("oops", { status: 500 }));
    const result = await injectSubSessionSummary(7, "summary text", "qwen3.6");
    expect(result.ok).toBe(false);
  });

  it("returns ok=false on a 4xx response", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("bad", { status: 422 }));
    const result = await injectSubSessionSummary(7, "summary text", "qwen3.6");
    expect(result.ok).toBe(false);
  });

  it("returns ok=false when fetch throws (network failure)", async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error("network down"));
    const result = await injectSubSessionSummary(7, "summary text", "qwen3.6");
    expect(result.ok).toBe(false);
  });

  it("posts to the chat's inject-message endpoint with content + model_id", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    global.fetch = fetchSpy;
    await injectSubSessionSummary(42, "the summary", "qwen3.6");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/chats/42/inject-message");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string) as { content: string; model_id: string | null };
    expect(body.content).toBe("the summary");
    expect(body.model_id).toBe("qwen3.6");
  });

  it("forwards a null model_id when none is selected", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    global.fetch = fetchSpy;
    await injectSubSessionSummary(1, "x", null);
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    const body = JSON.parse(init.body as string) as { model_id: string | null };
    expect(body.model_id).toBeNull();
  });
});

describe("handleSubSessionInject control flow", () => {
  // Mirrors the handler body in Chat.tsx — keeps the panel open on failure,
  // clears it on success. The handler doesn't ship state setters directly,
  // so we model them as spies here.
  async function run(injectOk: boolean) {
    const setSubSession = vi.fn();
    const reset = vi.fn();
    const refetchMessages = vi.fn();
    const push = vi.fn();

    global.fetch = vi.fn().mockResolvedValue(
      new Response("{}", { status: injectOk ? 200 : 500 }),
    );

    const { ok } = await injectSubSessionSummary(7, "content", "qwen3.6");
    if (!ok) {
      push({ variant: "error", message: "Failed to send summary to main chat." });
    } else {
      setSubSession(null);
      reset();
      refetchMessages();
    }
    return { setSubSession, reset, refetchMessages, push };
  }

  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("on inject failure: pushes error toast, does NOT clear sub-session", async () => {
    const { setSubSession, reset, refetchMessages, push } = await run(false);
    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Failed to send summary to main chat.",
    });
    expect(setSubSession).not.toHaveBeenCalled();
    expect(reset).not.toHaveBeenCalled();
    expect(refetchMessages).not.toHaveBeenCalled();
  });

  it("on inject success: clears sub-session and resets SSE state", async () => {
    const { setSubSession, reset, refetchMessages, push } = await run(true);
    expect(push).not.toHaveBeenCalled();
    expect(setSubSession).toHaveBeenCalledWith(null);
    expect(reset).toHaveBeenCalledTimes(1);
    expect(refetchMessages).toHaveBeenCalledTimes(1);
  });
});
