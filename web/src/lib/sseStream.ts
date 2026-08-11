/* SPDX-License-Identifier: Apache-2.0 */
/**
 * sseStream — shared fetch-based SSE reader loop, frame parser, and
 * run-generation guard for lm-chat's streaming hooks.
 *
 * Extracted 2026-07-17 to end a 3-way parser/reader fork: `parseSseBlock`
 * was duplicated verbatim between useSSE.ts and useABStream.ts, and
 * useSubSessionSSE.ts carried a THIRD hand-rolled line-by-line parser
 * (its old inline comments describe a bug where
 * a chunk boundary landing between an `event:` line and its `data:` line
 * misattributed the frame). All three hooks now share:
 *
 *   - `parseSseBlock`   — parse one "\n\n"-delimited SSE block into
 *                         {event, data}.
 *   - `readSseStream`   — decode (UTF-8, correct across multi-byte chunk
 *                         boundaries via TextDecoderStream) + buffer + split
 *                         on "\n\n" + dispatch loop over a fetch Response
 *                         body. Buffering until the full block has arrived
 *                         (rather than dispatching per-line) means the
 *                         event/data chunk-split bug useSubSessionSSE's old
 *                         parser had to work around by hoisting `eventType`
 *                         out of the read loop can no longer occur here:
 *                         nothing is parsed until the terminator arrives.
 *   - `createRunGuard`  — a monotonic generation counter so a superseded
 *                         stream's late setState calls (after abort() or a
 *                         fresh start()) become no-ops. Ported from
 *                         useSubSessionSSE's `streamSeqRef`/`guardedSetState`
 *                         pattern — previously the ONLY hook that had one —
 *                         to all three.
 *
 * Each hook keeps its OWN event-handling / state shape and its own wire
 * schema (main-chat nests `tool_call`; sub-session sends flat fields; A/B
 * carries a `pane` discriminator) — only the mechanical reader/parser/guard
 * is shared. This is a behavior-preserving refactor; see
 * tests/unit/test_sseStream.spec.ts for parser/reader unit coverage and the
 * per-hook specs for end-to-end characterization.
 */

// ─── SSE frame parser ───────────────────────────────────────────────────────

export interface SseFrame {
  event: string | null;
  data: string | null;
}

/** Parse one complete SSE block (text between "\n\n" separators). */
export function parseSseBlock(block: string): SseFrame {
  let event: string | null = null;
  let data: string | null = null;
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) {
      event = line.slice(7).trim();
    } else if (line.startsWith("data: ")) {
      data = line.slice(6);
    }
  }
  return { event, data };
}

// ─── Reader loop ────────────────────────────────────────────────────────────

/** Signal a frame handler returns to control the read loop. */
export type SseFrameSignal = "continue" | "stop";

export interface ReadSseStreamResult {
  /** True when the reader was exhausted (server closed the stream / done
   *  resolved). False when a handler returned "stop" and the loop cancelled
   *  the reader before returning. */
  exhausted: boolean;
}

/**
 * Drive a fetch Response body as an SSE stream: decode, buffer, split on
 * "\n\n" frame terminators, and invoke `onFrame` once per complete,
 * non-blank block with a non-null `data` line (a block with no `data:` line
 * at all — e.g. a comment-only block — is skipped before `onFrame` is
 * called, same as all three hooks did pre-extraction).
 *
 * Returns once the reader is exhausted OR `onFrame` returns "stop" (in which
 * case the reader is cancelled before returning — a caller that wants to
 * stop reading past a terminal frame, e.g. an `error` event, signals this
 * instead of managing the reader itself).
 */
export async function readSseStream(
  // NOTE: `Uint8Array<ArrayBuffer>` (not the bare `Uint8Array` alias, which
  // defaults to the wider `Uint8Array<ArrayBufferLike>`) — matches
  // `Body.body`'s exact type in lib.dom.d.ts. TextDecoderStream's `writable`
  // is `WritableStream<BufferSource>`; pipeThrough only typechecks against
  // this narrower element type, not the ArrayBufferLike-generic default.
  body: ReadableStream<Uint8Array<ArrayBuffer>>,
  onFrame: (frame: SseFrame) => SseFrameSignal,
): Promise<ReadSseStreamResult> {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) return { exhausted: true };
    buffer += value;

    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (block.trim() === "") continue;

      const frame = parseSseBlock(block);
      if (frame.data === null) continue;

      const signal = onFrame(frame);
      if (signal === "stop") {
        await reader.cancel().catch(() => {
          /* no-op */
        });
        return { exhausted: false };
      }
    }
  }
}

// ─── Run-generation guard ───────────────────────────────────────────────────

export interface RunGuard {
  /** Start a new generation; returns its id. Call once per start()/stream(). */
  start: () => number;
  /** Invalidate the current generation without starting a new one — call on
   *  stop()/abort()/unmount so a still-in-flight run's async continuations
   *  can no longer write state. */
  invalidate: () => void;
  /** True if `id` is still the active (most recent) generation. */
  isCurrent: (id: number) => boolean;
}

/**
 * createRunGuard — a monotonic generation counter. `start()` bumps to a new
 * generation and returns its id; `isCurrent(id)` is true only for the MOST
 * RECENT generation. Wrap a hook's async setState calls (everything after an
 * `await`, e.g. inside the fetch/read loop) so a stream superseded by a
 * newer start() call, or explicitly invalidated by stop()/abort(), can no
 * longer mutate state — the exact stale-callback race useSubSessionSSE's
 * `streamSeqRef` guarded against before this extraction generalized it to
 * useSSE and useABStream too (neither had this guard previously).
 *
 * The SYNCHRONOUS state reset at the top of start()/stream() (establishing
 * the new run's baseline state) should stay UNGUARDED — it always applies
 * unconditionally since nothing could have superseded it yet.
 */
export function createRunGuard(): RunGuard {
  let generation = 0;
  return {
    start: () => ++generation,
    invalidate: () => {
      generation++;
    },
    isCurrent: (id: number) => id === generation,
  };
}

// ─── Tool-call fold ──────────────────────────────────────────────────────────

/**
 * foldToolCallStart — upsert-by-id fold for a `tool_call.start` /
 * `sub.tool_call.start` frame.
 *
 * fe-components-state-9: useSSE's `tool_call.start` case used to APPEND a
 * new entry unconditionally — a repeated start frame for the SAME id (e.g.
 * a decoder resend or a reconnect) produced a DUPLICATE card. useSubSessionSSE's
 * `sub.tool_call.start` branch already upserted by id (replace the existing
 * entry in place; append only when the id is new). The backend's own
 * persistence fold — `_accumulate_tool_call` in
 * src/lmchat/services/streaming_service.py — does the same: on
 * `tool_call.start`, it only appends `if entry is None`; a second start for
 * an id already in its list is a no-op. Its docstring states it "Mirrors
 * the FE's live accumulator in useSSE.ts" — meaning useSSE's blind-append
 * was a regression from its own documented mirror, not a deliberate
 * design choice. Both hooks now share this single upsert-by-id fold.
 *
 * Structurally generic (`{ id: string }`) rather than importing either
 * hook's `ToolCall`/`SubSessionToolCall` type, so this stays a leaf
 * dependency of both hooks instead of coupling them to each other.
 */
export function foldToolCallStart<T extends { id: string }>(
  toolCalls: T[],
  next: T,
): T[] {
  const idx = toolCalls.findIndex((tc) => tc.id === next.id);
  return idx >= 0
    ? toolCalls.map((tc, i) => (i === idx ? next : tc))
    : [...toolCalls, next];
}
