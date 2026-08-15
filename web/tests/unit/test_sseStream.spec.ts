/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit + property tests for the shared SSE reader/parser/guard
 * (web/src/lib/sseStream.ts) extracted in fe-components-state-9.
 *
 * These tests exercise the REAL production module (not a hand-copied
 * mirror) — the property coverage that used to live only against a
 * standalone re-implementation inside
 * test_useSubSessionSSE_parser_property.spec.ts now also runs directly
 * against `readSseStream`/`parseSseBlock`, since useSubSessionSSE now
 * delegates to them.
 */
import { describe, it, expect } from "vitest";
import * as fc from "fast-check";
import { parseSseBlock, readSseStream, createRunGuard } from "@/lib/sseStream";
import type { SseFrame } from "@/lib/sseStream";

const encoder = new TextEncoder();

/** Build a ReadableStream that yields the given string chunks in order, one
 *  read() per chunk, then closes. */
function chunkedStream(chunks: string[]): ReadableStream<Uint8Array<ArrayBuffer>> {
  let idx = 0;
  return new ReadableStream<Uint8Array<ArrayBuffer>>({
    pull(controller) {
      const chunk = chunks[idx++];
      if (chunk !== undefined) {
        controller.enqueue(encoder.encode(chunk));
      } else {
        controller.close();
      }
    },
  });
}

// ─── parseSseBlock ───────────────────────────────────────────────────────────

describe("parseSseBlock", () => {
  it("parses an event: + data: block", () => {
    expect(parseSseBlock("event: chat.start\ndata: {\"a\":1}")).toEqual({
      event: "chat.start",
      data: '{"a":1}',
    });
  });

  it("returns event: null when no event: line is present", () => {
    expect(parseSseBlock("data: {\"a\":1}")).toEqual({
      event: null,
      data: '{"a":1}',
    });
  });

  it("returns data: null when no data: line is present", () => {
    expect(parseSseBlock("event: ping")).toEqual({ event: "ping", data: null });
  });

  it("does NOT trim the data payload (only event: is trimmed)", () => {
    // Matches useSSE/useABStream's original parseSseBlock — data is used
    // raw; callers that need trimming (e.g. useSubSessionSSE's `!raw.trim()`
    // empty-check) do it themselves.
    expect(parseSseBlock("data:   {\"a\":1}  ")).toEqual({
      event: null,
      data: '  {"a":1}  ',
    });
  });

  it("last event:/data: line in a block wins when duplicated", () => {
    expect(
      parseSseBlock("event: first\nevent: second\ndata: one\ndata: two"),
    ).toEqual({ event: "second", data: "two" });
  });
});

// ─── readSseStream ───────────────────────────────────────────────────────────

describe("readSseStream", () => {
  it("dispatches one frame per complete block, in order", async () => {
    const stream = chunkedStream([
      "event: a\ndata: 1\n\nevent: b\ndata: 2\n\n",
    ]);
    const seen: SseFrame[] = [];
    const result = await readSseStream(stream, (frame) => {
      seen.push(frame);
      return "continue";
    });
    expect(result.exhausted).toBe(true);
    expect(seen).toEqual([
      { event: "a", data: "1" },
      { event: "b", data: "2" },
    ]);
  });

  it("buffers across chunk boundaries — event: in one chunk, data: in the next", async () => {
    // The exact chunk-split class useSubSessionSSE's old inline parser had a
    // documented bug around (its "Fix #1"). Buffering until the "\n\n"
    // terminator means this can never misattribute or drop a frame.
    const stream = chunkedStream([
      "event: sub.delta\n",
      "data: {\"delta\":\"world\"}\n\n",
    ]);
    const seen: SseFrame[] = [];
    const result = await readSseStream(stream, (frame) => {
      seen.push(frame);
      return "continue";
    });
    expect(result.exhausted).toBe(true);
    expect(seen).toEqual([{ event: "sub.delta", data: '{"delta":"world"}' }]);
  });

  it("buffers across chunk boundaries even mid-terminator (the \\n\\n itself split)", async () => {
    const stream = chunkedStream(["event: a\ndata: 1\n", "\nevent: b\ndata: 2\n\n"]);
    const seen: SseFrame[] = [];
    await readSseStream(stream, (frame) => {
      seen.push(frame);
      return "continue";
    });
    expect(seen).toEqual([
      { event: "a", data: "1" },
      { event: "b", data: "2" },
    ]);
  });

  it("skips blank blocks (consecutive terminators) without dispatching", async () => {
    const stream = chunkedStream(["event: a\ndata: 1\n\n\n\nevent: b\ndata: 2\n\n"]);
    const seen: SseFrame[] = [];
    await readSseStream(stream, (frame) => {
      seen.push(frame);
      return "continue";
    });
    expect(seen).toEqual([
      { event: "a", data: "1" },
      { event: "b", data: "2" },
    ]);
  });

  it("skips a block with no data: line at all", async () => {
    const stream = chunkedStream(["event: ping\n\nevent: b\ndata: 2\n\n"]);
    const seen: SseFrame[] = [];
    await readSseStream(stream, (frame) => {
      seen.push(frame);
      return "continue";
    });
    expect(seen).toEqual([{ event: "b", data: "2" }]);
  });

  it('stops early and cancels the reader when onFrame returns "stop"', async () => {
    const stream = chunkedStream([
      "event: a\ndata: 1\n\nevent: b\ndata: 2\n\nevent: c\ndata: 3\n\n",
    ]);
    const seen: SseFrame[] = [];
    const result = await readSseStream(stream, (frame) => {
      seen.push(frame);
      return frame.event === "b" ? "stop" : "continue";
    });
    expect(result.exhausted).toBe(false);
    // "c" was never dispatched — the loop stopped as soon as "b" signalled.
    expect(seen).toEqual([
      { event: "a", data: "1" },
      { event: "b", data: "2" },
    ]);
  });

  it("a trailing partial block with no terminator is never dispatched", async () => {
    const stream = chunkedStream(["event: a\ndata: 1\n\nevent: b\ndata: 2"]);
    const seen: SseFrame[] = [];
    const result = await readSseStream(stream, (frame) => {
      seen.push(frame);
      return "continue";
    });
    expect(result.exhausted).toBe(true);
    expect(seen).toEqual([{ event: "a", data: "1" }]);
  });
});

// ─── readSseStream — property coverage ──────────────────────────────────────

describe("readSseStream — property: never crashes, never drops/duplicates well-formed frames", () => {
  it("never throws on arbitrary byte chunk sequences", () => {
    const arb_chunk = fc.uint8Array({ minLength: 0, maxLength: 200 });
    const arb_chunks = fc.array(arb_chunk, { minLength: 0, maxLength: 20 });

    return fc.assert(
      fc.asyncProperty(arb_chunks, async (chunks) => {
        const stream = new ReadableStream<Uint8Array<ArrayBuffer>>({
          start(controller) {
            for (const c of chunks) controller.enqueue(new Uint8Array(c));
            controller.close();
          },
        });
        await expect(
          readSseStream(stream, () => "continue"),
        ).resolves.toMatchObject({
          exhausted: true,
        });
      }),
      { numRuns: 300, seed: 0xca7b_eef0 },
    );
  });

  it("delivers every sub.delta-style frame exactly once regardless of split position", () => {
    const arbDeltas = fc.array(
      fc.string({ minLength: 1, maxLength: 30, unit: "grapheme-ascii" }),
      { minLength: 1, maxLength: 8 },
    );
    const arbPositions = fc.array(fc.integer({ min: 1, max: 999 }), {
      minLength: 0,
      maxLength: 12,
    });

    return fc.assert(
      fc.asyncProperty(arbDeltas, arbPositions, async (deltas, rawPositions) => {
        const payload = deltas
          .map((d) => `event: sub.delta\ndata: ${JSON.stringify({ delta: d })}\n\n`)
          .join("");
        const bytes = encoder.encode(payload);

        const positions = [
          ...new Set(
            rawPositions
              .map((p) => Math.round((p / 1000) * bytes.length))
              .filter((p) => p >= 1 && p < bytes.length),
          ),
        ].sort((a, b) => a - b);

        const chunks: Uint8Array<ArrayBuffer>[] = [];
        let prev = 0;
        for (const pos of positions) {
          if (pos > prev) chunks.push(bytes.slice(prev, pos));
          prev = pos;
        }
        if (prev < bytes.length) chunks.push(bytes.slice(prev));
        if (chunks.length === 0) chunks.push(bytes);

        const stream = new ReadableStream<Uint8Array<ArrayBuffer>>({
          start(controller) {
            for (const c of chunks) controller.enqueue(c);
            controller.close();
          },
        });

        const seen: SseFrame[] = [];
        await readSseStream(stream, (frame) => {
          seen.push(frame);
          return "continue";
        });

        const delivered = seen
          .filter((f) => f.event === "sub.delta")
          .map((f) => (JSON.parse(f.data ?? "{}") as { delta?: string }).delta ?? "");
        expect(delivered).toEqual(deltas);
      }),
      { numRuns: 300, seed: 0xf01d_b4be },
    );
  });
});

// ─── createRunGuard ──────────────────────────────────────────────────────────

describe("createRunGuard", () => {
  it("start() returns monotonically increasing ids; only the latest is current", () => {
    const guard = createRunGuard();
    const g1 = guard.start();
    const g2 = guard.start();
    expect(g2).toBeGreaterThan(g1);
    expect(guard.isCurrent(g1)).toBe(false);
    expect(guard.isCurrent(g2)).toBe(true);
  });

  it("invalidate() bumps the generation without starting a new one — the previously-current id becomes stale", () => {
    const guard = createRunGuard();
    const g1 = guard.start();
    expect(guard.isCurrent(g1)).toBe(true);
    guard.invalidate();
    expect(guard.isCurrent(g1)).toBe(false);
  });

  it("start() never returns 0 — callers can rely on a truthy generation id", () => {
    const guard = createRunGuard();
    expect(guard.start()).not.toBe(0);
  });
});
