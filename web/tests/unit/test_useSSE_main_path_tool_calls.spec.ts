/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Cross-pinned codec test — main-chat live tool_call wire (Finding #1,
 * audit ba1d324).
 *
 * Pins the FE parser to the EXACT frame shape the BE emits in
 * `src/lmchat/services/streaming_service.py:_format_sse_frame`:
 *
 *     event: <type>\n
 *     data: {"type":"<type>","msg_id":<int>,"tool_call":{...}}\n\n
 *
 * where `tool_call` is `CanonicalToolCall.model_dump()`:
 *
 *     {"id": str, "name": str, "arguments": dict,
 *      "call_id": str|null, "result": str|null}
 *
 * The mirror pin on the BE side is
 * `tests/services/test_streaming_service.py::
 *  test_format_sse_frame_tool_call_nested_shape`, which asserts the
 * producer emits exactly this nested key set. If either side drifts
 * (e.g. back to the flat `tool_call_id`/`name`/`arguments` keys that
 * caused the live-vs-reload regression), one of the two tests breaks.
 *
 * History: useSSE.ts read FLAT fields (`raw.tool_call_id`, `raw.name`,
 * `raw.arguments`, `raw.result`) that never existed on this wire — live
 * ToolCallCards got a synthesized id, stayed unnamed/pending, and never
 * attached results, while reload-from-DB rendered correctly.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSSE } from "@/hooks/useSSE";

// ─── BE-shape frame builders ─────────────────────────────────────────────────

/** CanonicalToolCall.model_dump() — ALL keys present (pydantic dumps every field). */
interface WireToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  call_id: string | null;
  result: string | null;
}

/**
 * Byte-identical reproduction of `_format_sse_frame` for tool_call.* events:
 * sparse data dict of {type, msg_id, tool_call}.
 */
function beToolCallFrame(
  type: string,
  msgId: number,
  toolCall: WireToolCall,
): string {
  const data = { type, msg_id: msgId, tool_call: toolCall };
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** `_format_sse_frame` for payload-free lifecycle frames (chat.start/chat.end). */
function beLifecycleFrame(
  type: string,
  msgId: number,
  extra: Record<string, unknown> = {},
): string {
  const data = { type, msg_id: msgId, ...extra };
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

function sseResponse(frames: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(frames));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

class StubChannel {
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  postMessage(_msg: unknown) {
    /* no-op */
  }
  close() {
    /* no-op */
  }
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("useSSE — main-path tool_call wire codec (BE nested shape)", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // @ts-expect-error -- stub BroadcastChannel globally
    global.BroadcastChannel = StubChannel;
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // @ts-expect-error -- restore
    delete global.BroadcastChannel;
  });

  it("parses a complete start → name → arguments → success sequence", async () => {
    // Sequence mirrors the native decoder's per-event CanonicalToolCall
    // shapes (lmstudio/native.py): start synthesizes id with empty
    // name/arguments; name carries the tool name; arguments carries the
    // COMPLETE dict; success carries arguments + result.
    const frames =
      beLifecycleFrame("chat.start", 6, { response_id: "rid-6" }) +
      beToolCallFrame("tool_call.start", 6, {
        id: "uuid-abc",
        name: "",
        arguments: {},
        call_id: null,
        result: null,
      }) +
      beToolCallFrame("tool_call.name", 6, {
        id: "uuid-abc",
        name: "search_web",
        arguments: {},
        call_id: null,
        result: null,
      }) +
      beToolCallFrame("tool_call.arguments", 6, {
        id: "uuid-abc",
        name: "search_web",
        arguments: { q: "lm studio", limit: 3 },
        call_id: null,
        result: null,
      }) +
      beToolCallFrame("tool_call.success", 6, {
        id: "uuid-abc",
        name: "search_web",
        arguments: { q: "lm studio", limit: 3 },
        call_id: null,
        result: "3 results found",
      }) +
      beLifecycleFrame("chat.end", 6);

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.status).toBe("complete");
    expect(result.current.state.toolCalls).toHaveLength(1);
    expect(result.current.state.toolCalls[0]).toEqual({
      // The BE id flows through — NOT a synthesized `tc-<timestamp>` fallback.
      id: "uuid-abc",
      name: "search_web",
      // Wire dict → FE string bridge (ToolCall.arguments: string).
      arguments: JSON.stringify({ q: "lm studio", limit: 3 }),
      status: "success",
      result: "3 results found",
    });
  });

  it("parses a failure terminator (result stays absent; status flips)", async () => {
    const frames =
      beLifecycleFrame("chat.start", 7, { response_id: "rid-7" }) +
      beToolCallFrame("tool_call.start", 7, {
        id: "uuid-def",
        name: "",
        arguments: {},
        call_id: null,
        result: null,
      }) +
      beToolCallFrame("tool_call.name", 7, {
        id: "uuid-def",
        name: "fetch_url",
        arguments: {},
        call_id: null,
        result: null,
      }) +
      beToolCallFrame("tool_call.failure", 7, {
        id: "uuid-def",
        name: "fetch_url",
        arguments: { url: "https://x" },
        call_id: null,
        result: null,
      }) +
      beLifecycleFrame("chat.end", 7);

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.toolCalls).toHaveLength(1);
    const tc = result.current.state.toolCalls[0];
    expect(tc?.id).toBe("uuid-def");
    expect(tc?.name).toBe("fetch_url");
    expect(tc?.status).toBe("failure");
    expect(tc?.arguments).toBe(JSON.stringify({ url: "https://x" }));
    expect(tc?.result).toBeUndefined();
  });

  it("two interleaved tool calls reconcile by nested id", async () => {
    const tcA = (over: Partial<WireToolCall>): WireToolCall => ({
      id: "uuid-A",
      name: "tool_a",
      arguments: {},
      call_id: null,
      result: null,
      ...over,
    });
    const tcB = (over: Partial<WireToolCall>): WireToolCall => ({
      id: "uuid-B",
      name: "tool_b",
      arguments: {},
      call_id: null,
      result: null,
      ...over,
    });

    const frames =
      beLifecycleFrame("chat.start", 8, { response_id: "rid-8" }) +
      beToolCallFrame("tool_call.start", 8, tcA({ name: "" })) +
      beToolCallFrame("tool_call.start", 8, tcB({ name: "" })) +
      beToolCallFrame("tool_call.name", 8, tcA({})) +
      beToolCallFrame("tool_call.name", 8, tcB({})) +
      beToolCallFrame("tool_call.success", 8, tcA({ result: "A done" })) +
      beToolCallFrame("tool_call.success", 8, tcB({ result: "B done" })) +
      beLifecycleFrame("chat.end", 8);

    global.fetch = vi.fn().mockResolvedValue(sseResponse(frames));
    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.start(42, { input: [] });
    });

    expect(result.current.state.toolCalls).toHaveLength(2);
    const byId = new Map(result.current.state.toolCalls.map((t) => [t.id, t]));
    expect(byId.get("uuid-A")).toMatchObject({
      name: "tool_a",
      status: "success",
      result: "A done",
    });
    expect(byId.get("uuid-B")).toMatchObject({
      name: "tool_b",
      status: "success",
      result: "B done",
    });
  });
});
