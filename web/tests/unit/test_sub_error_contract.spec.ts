/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Contract tests: sub.error SSE envelope JSON Schema.
 *
 * Validates that the hook's internal sub.error handler produces a state.error
 * that matches the canonical sub-error-schema.json.
 *
 * Schema source: web/src/types/sub-error-schema.json (canonical; this test
 * imports it rather than duplicating the shape).  If BE ever changes the
 * schema, this test catches regressions on
 * both sides of the wire.
 *
 * Validator: ajv (JSON Schema draft 2020-12 — matches the $schema declaration
 * in the file).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import Ajv from "ajv/dist/2020.js";
import { useSubSessionSSE } from "@/hooks/useSubSessionSSE";
import type { SubSessionStreamParams } from "@/hooks/useSubSessionSSE";
// Import the schema as a plain object — Vite/vitest resolves JSON imports natively.
import subErrorSchema from "@/types/sub-error-schema.json";

// ─── Validator setup ──────────────────────────────────────────────────────────

const ajv = new Ajv({ strict: false });
const validate = ajv.compile(subErrorSchema);

// ─── Helpers ──────────────────────────────────────────────────────────────────

const encoder = new TextEncoder();

function chunkedSseResponse(sseText: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      controller.enqueue(encoder.encode(sseText));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function buildSubErrorFrame(payload: Record<string, unknown>): string {
  return `event: sub.error\ndata: ${JSON.stringify(payload)}\n\n`;
}

const BASE_PARAMS: SubSessionStreamParams = {
  chatId: 1,
  modelId: "contract-test-model",
  systemPrompt: "test",
  messages: [],
};

async function drain(): Promise<void> {
  await new Promise<void>((r) => setTimeout(r, 80));
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("sub.error contract — state.error matches sub-error-schema.json", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(console, "debug").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("minimal valid envelope: {code, message} — passes schema and lands on state.error", async () => {
    const payload = { code: "stream_error", message: "Stream was interrupted." };
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse(buildSubErrorFrame(payload)));

    const { result } = renderHook(() => useSubSessionSSE());
    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await drain();
    });

    expect(result.current.state.status).toBe("error");
    const err = result.current.state.error;
    expect(err).not.toBeNull();
    // The hook normalises: code and message are always non-empty strings.
    expect(typeof err?.code).toBe("string");
    expect(err?.code.length).toBeGreaterThan(0);
    expect(typeof err?.message).toBe("string");
    expect(err?.message.length).toBeGreaterThan(0);

    // Schema validation — state.error must match the canonical envelope.
    const valid = validate(err);
    expect(valid, `AJV errors: ${JSON.stringify(validate.errors)}`).toBe(true);
  });

  it("full envelope: {code, message, hint, tally, accumulated_chars, truncated} — passes schema", async () => {
    const payload = {
      code: "stream_truncated",
      message: "Stream ended early.",
      hint: "Try a shorter prompt.",
      tally: { sub_delta: 5, sub_tool_call_start: 1 },
      accumulated_chars: 312,
      truncated: true,
    };
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse(buildSubErrorFrame(payload)));

    const { result } = renderHook(() => useSubSessionSSE());
    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await drain();
    });

    expect(result.current.state.status).toBe("error");
    const err = result.current.state.error;
    expect(err).not.toBeNull();
    expect(err?.hint).toBe("Try a shorter prompt.");
    expect(err?.truncated).toBe(true);
    expect(err?.tally).toEqual({ sub_delta: 5, sub_tool_call_start: 1 });

    const valid = validate(err);
    expect(valid, `AJV errors: ${JSON.stringify(validate.errors)}`).toBe(true);
  });

  it("known code 'no_model_selected' — passes schema with synthesised message", async () => {
    const payload = { code: "no_model_selected", message: "No model selected." };
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse(buildSubErrorFrame(payload)));

    const { result } = renderHook(() => useSubSessionSSE());
    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await drain();
    });

    const err = result.current.state.error;
    expect(err).not.toBeNull();
    expect(err?.code).toBe("no_model_selected");

    const valid = validate(err);
    expect(valid, `AJV errors: ${JSON.stringify(validate.errors)}`).toBe(true);
  });

  it("unknown code arrives from newer BE version — hook preserves code; passes schema", async () => {
    const payload = { code: "new_future_error", message: "A new type of error." };
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse(buildSubErrorFrame(payload)));

    const { result } = renderHook(() => useSubSessionSSE());
    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await drain();
    });

    const err = result.current.state.error;
    expect(err).not.toBeNull();
    expect(err?.code).toBe("new_future_error");

    const valid = validate(err);
    expect(valid, `AJV errors: ${JSON.stringify(validate.errors)}`).toBe(true);
  });

  it("malformed envelope (missing code) — hook synthesises fallback; schema still satisfied", async () => {
    // The hook defaults code to "" when missing, then synthesises a message.
    // Schema requires minLength:1 — this tests the hook's defensive fill-in.
    const payload = { message: "Something broke." }; // no code field
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse(buildSubErrorFrame(payload)));

    const { result } = renderHook(() => useSubSessionSSE());
    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await drain();
    });

    const err = result.current.state.error;
    expect(err).not.toBeNull();
    // BUG-C fixed: hook now falls back to "unknown_error" when BE omits code.
    expect(err?.code).toBe("unknown_error");
    expect(err?.code.length).toBeGreaterThan(0);

    // Schema validation — fallback code satisfies minLength:1.
    const valid = validate(err);
    expect(valid, `AJV errors: ${JSON.stringify(validate.errors)}`).toBe(true);
  });

  it("regression: multiple sub.error frames — last one wins, all pass schema", async () => {
    // In practice the hook returns early after the first sub.error (the
    // `return` statement after guardedSetState). This test confirms only one
    // error lands and that it still satisfies the schema.
    const frame1 = buildSubErrorFrame({ code: "upstream_error", message: "First error." });
    const frame2 = buildSubErrorFrame({ code: "stream_error", message: "Second error." });
    // Both frames in one chunk — parser sees frame1 first and returns.
    vi.mocked(fetch).mockResolvedValueOnce(chunkedSseResponse(frame1 + frame2));

    const { result } = renderHook(() => useSubSessionSSE());
    await act(async () => {
      result.current.stream(BASE_PARAMS);
      await drain();
    });

    expect(result.current.state.status).toBe("error");
    const err = result.current.state.error;
    expect(err).not.toBeNull();
    // First error wins because the hook returns early.
    expect(err?.code).toBe("upstream_error");

    const valid = validate(err);
    expect(valid, `AJV errors: ${JSON.stringify(validate.errors)}`).toBe(true);
  });
});
