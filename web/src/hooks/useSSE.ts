/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSSE — fetch-based Server-Sent Events state machine for lm-chat streaming.
 *
 * WHY fetch, not EventSource:
 *   EventSource cannot send custom headers (CSRF) and doesn't support POST
 *   requests. Manual SSE parsing over a ReadableStream is the standard
 *   2025-2026 pattern for credentialed POST SSE (MDN WHATWG issue #2177).
 *
 * State machine transitions:
 *   idle ──[start()]──► streaming
 *   streaming ──[chat.start]──► streaming (sets messageId, responseId)
 *   streaming ──[message.delta]──► streaming (appends content)
 *   streaming ──[reasoning.delta]──► streaming (appends reasoning)
 *   streaming ──[tool_call.*]──► streaming (accumulates ToolCall)
 *   streaming ──[chat.end]──► complete
 *   streaming ──[error frame]──► error
 *   streaming ──[stop()]──► stopped (partial visible, refetch pending)
 *   stopped ──[reset()]──► idle
 *   stopped ──[start()]──► streaming (new turn)
 *   error ──[start()]──► streaming (retry)
 *   complete ──[start()]──► streaming (new turn)
 *
 * Multi-tab coordination:
 *   - msg_id for in-flight streams stored in localStorage under
 *     `lmchat:sse:<chat_id>:msg_id` (cross-tab visible).
 *   - BroadcastChannel('lmchat-streams') broadcasts stream lifecycle events
 *     so peer tabs on the same chat can show "streaming in another tab" UI.
 *   - Graceful degradation: if BroadcastChannel is unavailable, the storage
 *     event on window covers tab-to-tab notification.
 *
 * Response-ID reconciliation:
 *   On chat.start the server sends response_id. This is stored locally and
 *   passed as previous_response_id on the next message in the same chat
 *   (LM Studio native multi-turn).
 *
 * SSE wire format (per streaming.py / streaming_service.py):
 *   event: <type>\n
 *   data: {"type":"<type>","msg_id":<int>,...}\n\n
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { createRunGuard, foldToolCallStart, readSseStream } from "@/lib/sseStream";

// ─── Types ──────────────────────────────────────────────────────────────────

export interface ToolCall {
  id: string;
  name: string;
  arguments: string;
  status: "pending" | "success" | "failure";
  result?: string | undefined;
}

/**
 * Per-message stats (live during streaming, finalized at chat.end).
 * chat.end carries REAL LM Studio stats (`total_output_tokens` /
 * `tokens_per_second` from the native `result.stats` block, serialized by
 * streaming_service._format_sse_frame). When present they replace the FE
 * chunk-count approximation at finalize; the live values during streaming
 * remain the rolling FE estimate.
 */
export interface StreamStats {
  /** Tokens per second (rolling live estimate; finalized at chat.end). */
  tokensPerSecond: number | null;
  /** Time-to-first-token in seconds (set when first delta arrives). */
  ttftSeconds: number | null;
  /** Total content token deltas received (proxy for output token count). */
  outputTokens: number;
}

/**
 * Model load / prompt processing phase for ThinkingIndicator.
 * Null when idle/complete. Updated by model_load.* and prompt_processing.* events.
 */
export interface LoadPhase {
  phase: "model_load" | "prompt_processing" | "generating";
  /** Progress 0–1 for model_load.progress / prompt_processing.progress. */
  progress: number | null;
}

export interface StreamState {
  status: "idle" | "streaming" | "complete" | "error" | "stopped";
  messageId: number | null;
  responseId: string | null;
  /** Accumulating assistant content delta chunks (joined for display). */
  contentDeltas: string[];
  /** Accumulating reasoning/thinking delta chunks. */
  reasoningDeltas: string[];
  toolCalls: ToolCall[];
  // cumulative_tool_rounds/hint: only ever populated on an mtp_suspected
  // error frame (see the "error" case in handleEvent below, which reads
  // raw.error?.cumulative_tool_rounds / raw.error?.hint unconditionally on
  // every error). Optional here because every other error code leaves them
  // undefined. Neither is currently read by a consumer of THIS state shape
  // — StreamErrorBanner renders via humanizeApiError(), which is a static
  // code→copy lookup blind to both fields; the sub-session panel's own
  // (differently-typed) SubSessionSSEState.error.hint IS rendered, but that
  // is a separate state shape (useSubSessionSSE.ts), not this one. Fixing
  // the type is what was asked; wiring a consumer is separate work — see
  // the accompanying report.
  error: {
    code: string;
    message: string;
    // Explicit `| undefined` (not just `?:`) because the construction site
    // assigns `raw.error?.cumulative_tool_rounds`/`raw.error?.hint`
    // directly — a value that IS `undefined` when absent, not an omitted
    // key — and this project's tsconfig has exactOptionalPropertyTypes on,
    // which treats `prop?: T` and `prop?: T | undefined` differently.
    cumulative_tool_rounds?: number | undefined;
    hint?: string | undefined;
  } | null;
  /** Live stats for the current/last streaming turn. */
  stats: StreamStats;
  /** Current load/processing phase for ThinkingIndicator. */
  loadPhase: LoadPhase | null;
  /**
   * True when the upstream SSE reader exhausted while content deltas had
   * arrived but no `chat.end` or `error` terminal frame was ever
   * received. The Continue affordance reads this OR
   * `chat.end.stop_reason === "length"` — one chip, one state, two
   * sources. False on a clean `chat.end` and on a `stream_truncated`
   * error (which is its own status, not a flag).
   */
  truncated_without_terminal: boolean;
  /**
   * Why the stream terminated, from `chat.end.stop_reason`: "stop"
   * (natural end) | "length" (max_output_tokens truncation). Null until a
   * chat.end carrying a reason arrives (older backends omit the field).
   */
  stop_reason: string | null;
  /**
   * The unified Continue affordance:
   * `stop_reason === "length" || truncated_without_terminal`. Computed
   * here (not in Chat.tsx) so both chip sources share one state machine.
   */
  showContinue: boolean;
  /**
   * Accumulated non-terminal `warning` frames for the current/last stream
   * (e.g. the budget gate trimming integrations). Chat.tsx surfaces new
   * entries as toasts. Reset to [] on every start().
   */
  warnings: { code: string; message: string }[];
  /**
   * Out-of-band followup chips from the BE `followups` SSE event.
   * Arrives after `chat.end`; set to the array when received.
   * Reset to [] on every start(). Distinct from the legacy inline
   * `extractFollowups` path which is now a harmless no-op (the main
   * content no longer carries the HTML comment).
   */
  followups: string[];
  /**
   * Quiet auto-memory indicator from the BE `memory.saved` SSE event.
   * Arrives after `chat.end` (and after `followups`, when both fire) once
   * the detached auto-memory distillation task resolves within the BE's
   * bounded wait. `undefined` until received this turn; reset to
   * `undefined` on every start(). A slow/failed distillation still stores
   * the fact server-side — it just won't show this inline indicator for
   * that turn.
   */
  memorySaved: { count: number; msgId: number } | undefined;
  /**
   * C3 — out-of-band model-decided role adoption from the BE `mode_adopt`
   * SSE event. Arrives after `chat.end` (and after `followups`, when both
   * fire — `mode_adopt` is yielded before the `memory.saved` wait).
   * `presetId` is `null` when the classifier found no confident match this
   * turn or the feature is disabled server-side — Chat.tsx treats `null`
   * as a no-op. Reset to `undefined` on every start()/reset(); consumed by
   * an effect in Chat.tsx that applies it via `useChatPreset`'s
   * `adoptModelPreset`.
   */
  modeAdopt: { presetId: string | null; msgId: number } | undefined;
}

export interface ChatStreamPayload {
  model?: string | undefined;
  /** Current-turn content blocks — maps to CanonicalChatRequest.input on the backend. */
  input: { type: "text" | "image"; content?: string; data_url?: string }[];
  previous_response_id?: string | undefined;
  /** User-toggled MCP integrations for this turn (e.g. ``["mcp/context7"]``).
   *  Forwarded by both main-chat and sub-session paths; consumers must
   *  treat as opt-in (omit when empty). */
  integrations?: string[];
  [key: string]: unknown;
}

interface UseSSEReturn {
  state: StreamState;
  start: (chatId: number, payload: ChatStreamPayload) => Promise<void>;
  stop: () => void;
  /** Transition "stopped" → "idle", clearing the partial. */
  reset: () => void;
}

// ─── BroadcastChannel wrapper ───────────────────────────────────────────────

type StreamLifecycleMsg =
  | { type: "stream_started"; chat_id: number; msg_id: number }
  | { type: "chat_end"; chat_id: number }
  | { type: "aborted"; chat_id: number }
  | { type: "stream_handoff"; chat_id: number };

function openChannel(): BroadcastChannel | null {
  try {
    return new BroadcastChannel("lmchat-streams");
  } catch {
    return null;
  }
}

// ─── localStorage helpers ───────────────────────────────────────────────────

const LS_PREFIX = "lmchat:sse";

function lsKey(chatId: number): string {
  return `${LS_PREFIX}:${String(chatId)}:msg_id`;
}

function storeMsgId(chatId: number, msgId: number): void {
  try {
    localStorage.setItem(lsKey(chatId), String(msgId));
  } catch {
    // Ignore write failures.
  }
}

function clearMsgId(chatId: number): void {
  try {
    localStorage.removeItem(lsKey(chatId));
  } catch {
    // Ignore.
  }
}

// ─── SSE event dispatcher ───────────────────────────────────────────────────

/**
 * Every SSE event name the BE can put on the wire. One value-level artifact
 * so the type union AND the runtime contract test share a single source:
 *
 *   - the type `CanonicalEventType` is derived from this array, so
 *     `handleEvent`'s assertNever default makes the switch exhaustive
 *     over exactly these names;
 *   - the contract test (test_sse_event_names_contract.spec.ts) asserts
 *     this array matches web/src/types/sse-event-names.json, which the BE
 *     pytest (tests/contracts/test_sse_event_names_contract.py) asserts
 *     against the CanonicalEvent literal + synthetic frame formatters.
 *
 * Sources on the BE: `CanonicalEvent.type` Literal (lmstudio/types.py)
 * plus the synthetic `warning` frame (_format_warning_frame). May not be
 * hand-edited without updating sse-event-names.json — both contract tests
 * fail loudly on drift.
 */
export const CANONICAL_EVENT_TYPES = [
  "chat.start",
  "message.start",
  "message.delta",
  "message.end",
  "reasoning.start",
  "reasoning.delta",
  "reasoning.end",
  "tool_call.start",
  "tool_call.name",
  "tool_call.arguments",
  "tool_call.success",
  "tool_call.failure",
  "tool_call.repeat_warning",
  "tool_call.failure_streak_warning",
  "tool_call.name_warning",
  "chat.end",
  "error",
  // Non-terminal advisory frame from the BE budget gate
  // (_format_warning_frame), e.g. integrations trimmed for context. May
  // arrive BEFORE chat.start.
  "warning",
  // Model load / prompt processing (forwarded verbatim from LM Studio):
  "model_load.start",
  "model_load.progress",
  "model_load.end",
  "prompt_processing.start",
  "prompt_processing.progress",
  "prompt_processing.end",
  // Out-of-band followups — emitted AFTER chat.end once the separate
  // lightweight followups call completes. Never blocks the main answer.
  "followups",
  // Quiet auto-memory-saved indicator — emitted AFTER chat.end (and after
  // `followups`, when both fire) once the detached distillation task
  // resolves within the BE's bounded wait. Omitted entirely when nothing
  // new was stored or the wait timed out.
  "memory.saved",
  // C3 — model-decided role adoption. Emitted AFTER chat.end (and after
  // `followups`, when both fire — before the `memory.saved` wait) once
  // the separate out-of-band mode-classifier call (_infer_mode_oob)
  // completes.
  // `preset_id` is `null` when no persona clearly fit the next turn (the
  // common case — the classifier is deliberately biased toward no
  // change) or the feature is disabled server-side
  // (lm_chat_mode_adoption_enabled). Never blocks the main answer.
  "mode_adopt",
] as const;

/** Known SSE event types emitted by streaming_service.py. */
export type CanonicalEventType = (typeof CANONICAL_EVENT_TYPES)[number];

interface RawEvent {
  type: CanonicalEventType;
  msg_id?: number | undefined;
  response_id?: string | undefined;
  /** On chat.end — "stop" | "length". */
  stop_reason?: string | undefined;
  delta?: string | undefined;
  content?: string | undefined;
  /**
   * The main-chat wire nests the tool-call payload —
   * `data["tool_call"] = event.tool_call.model_dump()` per
   * streaming_service.py:_format_sse_frame. `arguments` is a JSON OBJECT
   * (dict) on the wire, NOT a string; handlers JSON.stringify it to fit
   * the FE `ToolCall.arguments: string` shape. There are no flat
   * `tool_call_id`/`name`/`arguments`/`result` keys on this path (the
   * sub-session path flattens, but that's useSubSessionSSE's wire).
   */
  tool_call?:
    | {
        id?: string;
        name?: string;
        arguments?: Record<string, unknown> | null;
        call_id?: string | null;
        result?: string | null;
      }
    | undefined;
  /** Real LM Studio stats on chat.end. */
  total_output_tokens?: number | undefined;
  tokens_per_second?: number | undefined;
  code?: string | undefined;
  message?: string | undefined;
  /** 0.0–1.0 progress for model_load.progress / prompt_processing.progress. */
  progress?: number | undefined;
  error?: {
    code?: string;
    message?: string;
    cumulative_tool_rounds?: number;
    hint?: string;
  };
  /** Nested payload of the non-terminal `warning` frame. */
  warning?: {
    code?: string;
    message?: string;
  };
  /** OOB followups chips — emitted after chat.end completes. */
  followups?: string[] | undefined;
  /** Quiet auto-memory-saved count — emitted after chat.end completes. */
  count?: number | undefined;
  /** C3 mode-adoption verdict — emitted after chat.end completes. */
  preset_id?: string | null | undefined;
}

/**
 * Timing refs — passed from the hook's start() closure.
 * Using a mutable object avoids prop-drilling through handleEvent.
 */
interface TimingRefs {
  streamStartMs: number;
  firstTokenMs: number | null;
  deltaCount: number;
}

/**
 * Bridge the wire tool_call.arguments DICT into the FE
 * `ToolCall.arguments: string` shape. Mirrors the BE persistence fold
 * (`_accumulate_tool_call`: success/failure only refresh arguments when the
 * dict is non-empty). Returns null for absent/empty dicts so callers keep
 * the previously accumulated value.
 */
function stringifyWireToolArgs(
  args: Record<string, unknown> | null | undefined,
): string | null {
  if (args == null || Object.keys(args).length === 0) return null;
  try {
    return JSON.stringify(args);
  } catch {
    return null;
  }
}

/**
 * Exhaustiveness guard for handleEvent's switch. Replaces the silent
 * `default: break;` that used to let a new event slip through: a NEW BE-side
 * event landing without an FE-side case now fails the typecheck (this call
 * no longer narrows to `never`) instead of being silently dropped on the
 * floor. Intentionally-ignored lifecycle events get an explicit allow-list
 * `break` above the default.
 */
function assertNever(x: never): never {
  throw new Error(
    `useSSE: unhandled SSE event type ${JSON.stringify(x)} — add a handleEvent ` +
      "case (or the explicit ignore allow-list) and update " +
      "web/src/types/sse-event-names.json.",
  );
}

function handleEvent(
  raw: RawEvent,
  chatId: number,
  setState: Dispatch<SetStateAction<StreamState>>,
  channel: BroadcastChannel | null,
  timing: TimingRefs,
): void {
  switch (raw.type) {
    case "chat.start":
      if (raw.msg_id !== undefined) storeMsgId(chatId, raw.msg_id);
      if (channel !== null && raw.msg_id !== undefined) {
        const msg: StreamLifecycleMsg = {
          type: "stream_started",
          chat_id: chatId,
          msg_id: raw.msg_id,
        };
        channel.postMessage(msg);
      }
      setState((s) => ({
        ...s,
        messageId: raw.msg_id ?? s.messageId,
        responseId: raw.response_id ?? s.responseId,
      }));
      break;

    case "message.delta": {
      // Track TTFT and running tok/s.
      const nowMs = performance.now();
      timing.firstTokenMs ??= nowMs;
      timing.deltaCount += 1;
      const elapsedS = (nowMs - timing.streamStartMs) / 1000;
      const tps = elapsedS > 0 ? timing.deltaCount / elapsedS : null;
      const ttft = (timing.firstTokenMs - timing.streamStartMs) / 1000;
      setState((s) => ({
        ...s,
        contentDeltas: [...s.contentDeltas, raw.delta ?? raw.content ?? ""],
        stats: {
          tokensPerSecond: tps,
          ttftSeconds: ttft,
          outputTokens: timing.deltaCount,
        },
      }));
      break;
    }

    case "reasoning.delta":
      setState((s) => ({
        ...s,
        reasoningDeltas: [...s.reasoningDeltas, raw.delta ?? raw.content ?? ""],
      }));
      break;

    // All tool_call.* handlers read the NESTED `raw.tool_call` payload
    // (CanonicalToolCall.model_dump() on the wire). The fold mirrors the BE
    // persistence accumulator (streaming_service._accumulate_tool_call) so
    // live cards and reload-from-DB cards render identically. This case used
    // to unconditionally APPEND, so a repeated `tool_call.start` for the
    // SAME id (decoder resend / reconnect) produced a duplicate card — a
    // real divergence from the BE fold it claims to mirror (which upserts by
    // id) and from useSubSessionSSE's `sub.tool_call.start` (which already
    // upserted). Now upsert-by-id via the shared `foldToolCallStart` — see
    // lib/sseStream.ts.
    case "tool_call.start": {
      const tc: ToolCall = {
        id: raw.tool_call?.id ?? `tc-${String(Date.now())}`,
        name: raw.tool_call?.name ?? "",
        arguments: "",
        status: "pending",
      };
      setState((s) => ({
        ...s,
        toolCalls: foldToolCallStart(s.toolCalls, tc),
      }));
      break;
    }

    case "tool_call.name":
      setState((s) => ({
        ...s,
        toolCalls: s.toolCalls.map((tc) =>
          tc.id === raw.tool_call?.id
            ? { ...tc, name: raw.tool_call.name ?? tc.name }
            : tc,
        ),
      }));
      break;

    case "tool_call.arguments":
      // The native decoder re-sends the COMPLETE arguments dict per event
      // (not a string delta) — REPLACE the stringified value, don't append.
      setState((s) => ({
        ...s,
        toolCalls: s.toolCalls.map((tc) =>
          tc.id === raw.tool_call?.id
            ? {
                ...tc,
                name: raw.tool_call.name ?? tc.name,
                arguments: JSON.stringify(raw.tool_call.arguments ?? {}),
              }
            : tc,
        ),
      }));
      break;

    case "tool_call.success":
      setState((s) => ({
        ...s,
        toolCalls: s.toolCalls.map((tc) =>
          tc.id === raw.tool_call?.id
            ? {
                ...tc,
                name: raw.tool_call.name ?? tc.name,
                arguments:
                  stringifyWireToolArgs(raw.tool_call.arguments) ??
                  tc.arguments,
                status: "success",
                result: raw.tool_call.result ?? undefined,
              }
            : tc,
        ),
      }));
      break;

    case "tool_call.failure":
      setState((s) => ({
        ...s,
        toolCalls: s.toolCalls.map((tc) =>
          tc.id === raw.tool_call?.id
            ? {
                ...tc,
                name: raw.tool_call.name ?? tc.name,
                arguments:
                  stringifyWireToolArgs(raw.tool_call.arguments) ??
                  tc.arguments,
                status: "failure",
                result: raw.tool_call.result ?? undefined,
              }
            : tc,
        ),
      }));
      break;

    case "chat.end": {
      clearMsgId(chatId);
      if (channel !== null) {
        const msg: StreamLifecycleMsg = { type: "chat_end", chat_id: chatId };
        channel.postMessage(msg);
      }
      // Finalize stats on chat.end; snap tok/s to authoritative value.
      const finalTtft =
        timing.firstTokenMs !== null
          ? (timing.firstTokenMs - timing.streamStartMs) / 1000
          : null;
      const finalElapsedS = (performance.now() - timing.streamStartMs) / 1000;
      const finalTps =
        finalElapsedS > 0 && timing.deltaCount > 0
          ? timing.deltaCount / finalElapsedS
          : null;
      setState((s) => ({
        ...s,
        status: "complete",
        loadPhase: null,
        // Capture WHY the stream ended. "length" (max_output_tokens
        // truncation) raises the Continue chip; the
        // truncated_without_terminal OR keeps the EOF-without-terminal
        // source alive (one chip, one state, two sources).
        stop_reason: raw.stop_reason ?? null,
        showContinue:
          raw.stop_reason === "length" || s.truncated_without_terminal,
        // LM Studio nests the chain anchor in `chat.end.result.response_id`,
        // not `chat.start` — the docstring above predates the live wire
        // probe. Without copying it here `sseState.responseId` stays null,
        // `storeResponseId` never fires, and every next turn arrives at
        // LM Studio with no `previous_response_id`. That's the bug behind
        // "same answer to the same question twice" / "model loses
        // context after turn 2".
        responseId: raw.response_id ?? s.responseId,
        // Prefer REAL LM Studio stats from the chat.end frame (decoded from
        // native `result.stats` and serialized by _format_sse_frame) over
        // the FE chunk-count approximation. Fall back to the local
        // computation when the surface omits them.
        stats: {
          tokensPerSecond: raw.tokens_per_second ?? finalTps,
          ttftSeconds: finalTtft,
          outputTokens: raw.total_output_tokens ?? timing.deltaCount,
        },
      }));
      break;
    }

    // Non-terminal advisory frame. The budget gate yields it BEFORE
    // chat.start (gate runs before the upstream stream opens), so this
    // handler must not assume any prior lifecycle event arrived. Appends to
    // `warnings`; status untouched — the stream proceeds normally.
    case "warning":
      setState((s) => ({
        ...s,
        warnings: [
          ...s.warnings,
          {
            code: raw.warning?.code ?? "warning",
            message: raw.warning?.message ?? "Stream warning",
          },
        ],
      }));
      break;

    case "error":
      clearMsgId(chatId);
      setState((s) => ({
        ...s,
        status: "error",
        error: {
          code: raw.error?.code ?? raw.code ?? "unknown",
          message: raw.error?.message ?? raw.message ?? "stream error",
          cumulative_tool_rounds: raw.error?.cumulative_tool_rounds,
          hint: raw.error?.hint,
        },
        loadPhase: null,
      }));
      break;

    // Model load / prompt processing phase events.
    case "model_load.start":
      setState((s) => ({
        ...s,
        loadPhase: { phase: "model_load", progress: null },
      }));
      break;

    case "model_load.progress":
      setState((s) => ({
        ...s,
        loadPhase: { phase: "model_load", progress: raw.progress ?? null },
      }));
      break;

    case "model_load.end":
      setState((s) => ({
        ...s,
        loadPhase: { phase: "generating", progress: null },
      }));
      break;

    case "prompt_processing.start":
      setState((s) => ({
        ...s,
        loadPhase: { phase: "prompt_processing", progress: null },
      }));
      break;

    case "prompt_processing.progress":
      setState((s) => ({
        ...s,
        loadPhase: {
          phase: "prompt_processing",
          progress: raw.progress ?? null,
        },
      }));
      break;

    case "prompt_processing.end":
      setState((s) => ({
        ...s,
        loadPhase: { phase: "generating", progress: null },
      }));
      break;

    case "message.start":
      setState((s) => ({
        ...s,
        loadPhase: { phase: "generating", progress: null },
      }));
      break;

    // Explicit ignore allow-list: lifecycle markers with no UI-visible
    // payload (message.end / reasoning.*) and the synthetic pipeline
    // advisories (tool_call.*_warning) that have no FE surface yet. Listed
    // by name — NOT folded into default — so the assertNever below stays
    // exhaustive over CanonicalEventType.
    // OOB followups frame — arrives after chat.end. Store the chips array
    // in state so Chat.tsx can render suggestion chips without re-parsing
    // the main content for an HTML comment (which is no longer emitted).
    case "followups":
      setState((s) => ({
        ...s,
        followups: Array.isArray(raw.followups) ? (raw.followups) : [],
      }));
      break;

    // Quiet auto-memory-saved indicator — arrives after chat.end (and after
    // followups, when both fire). Only ever sent with count >= 1 (the BE
    // omits the frame entirely when there's nothing to show), but guard
    // anyway rather than trust the wire blindly.
    case "memory.saved": {
      const count = raw.count ?? 0;
      const msgId = raw.msg_id;
      if (count > 0 && msgId !== undefined) {
        setState((s) => ({
          ...s,
          memorySaved: { count, msgId },
        }));
      }
      break;
    }

    // C3 mode-adoption frame — arrives after chat.end. Store the verdict
    // (possibly `preset_id: null`) so Chat.tsx's effect can apply it via
    // useChatPreset. Unlike memory.saved, this is stored even when
    // preset_id is null — Chat.tsx's effect is what decides null is a
    // no-op, mirroring how the followups case stores an empty array rather
    // than omitting state on the empty case.
    case "mode_adopt": {
      const msgId = raw.msg_id;
      if (msgId !== undefined) {
        setState((s) => ({
          ...s,
          modeAdopt: { presetId: raw.preset_id ?? null, msgId },
        }));
      }
      break;
    }

    case "message.end":
    case "reasoning.start":
    case "reasoning.end":
    case "tool_call.repeat_warning":
    case "tool_call.failure_streak_warning":
    case "tool_call.name_warning":
      break;

    default:
      // A new BE event without an FE case is a typecheck error here
      // (raw.type must narrow to `never`) and a loud runtime error instead
      // of a silently-dropped frame.
      assertNever(raw.type);
  }
}

// ─── Hook ───────────────────────────────────────────────────────────────────

const INITIAL_STATS: StreamStats = {
  tokensPerSecond: null,
  ttftSeconds: null,
  outputTokens: 0,
};

const INITIAL_STATE: StreamState = {
  status: "idle",
  messageId: null,
  responseId: null,
  contentDeltas: [],
  reasoningDeltas: [],
  toolCalls: [],
  error: null,
  stats: INITIAL_STATS,
  loadPhase: null,
  truncated_without_terminal: false,
  stop_reason: null,
  showContinue: false,
  warnings: [],
  followups: [],
  memorySaved: undefined,
  modeAdopt: undefined,
};

export function useSSE(): UseSSEReturn {
  const [state, setState] = useState<StreamState>(INITIAL_STATE);

  // Abort controller ref — allows stop() to abort mid-stream.
  const abortRef = useRef<AbortController | null>(null);

  // BroadcastChannel — opened once per hook instance.
  const channelRef = useRef<BroadcastChannel | null>(null);

  // Track current chat_id for multi-tab messages.
  const chatIdRef = useRef<number | null>(null);

  // Run-generation guard: discards setState calls from a stream superseded
  // by a newer start() or an explicit stop(). Only useSubSessionSSE had this
  // before extraction; generalized to all three SSE hooks (see
  // lib/sseStream.ts).
  const runGuardRef = useRef(createRunGuard());

  useEffect(() => {
    const ch = openChannel();
    channelRef.current = ch;

    if (ch !== null) {
      ch.onmessage = (ev: MessageEvent<StreamLifecycleMsg>) => {
        const msg = ev.data;
        // Only react to events about the chat this hook instance is tracking.
        if (chatIdRef.current === null || msg.chat_id !== chatIdRef.current)
          return;

        if (msg.type === "stream_started") {
          // Another tab started streaming this chat — show as streaming.
          setState((s) =>
            s.status !== "streaming"
              ? { ...s, status: "streaming", messageId: msg.msg_id }
              : s,
          );
        } else if (msg.type === "chat_end" || msg.type === "aborted") {
          setState((s) =>
            s.status === "streaming" ? { ...s, status: "complete" } : s,
          );
        }
      };
    }

    return () => {
      ch?.close();
    };
  }, []);

  const stop = useCallback((): void => {
    runGuardRef.current.invalidate();
    abortRef.current?.abort();
    abortRef.current = null;
    const chatId = chatIdRef.current;
    if (chatId !== null) {
      clearMsgId(chatId);
      const ch = channelRef.current;
      if (ch !== null) {
        const msg: StreamLifecycleMsg = { type: "aborted", chat_id: chatId };
        ch.postMessage(msg);
      }
    }
    // Transition to "stopped" rather than "idle" so the in-memory partial
    // (contentDeltas) stays visible with a "Stopped" chip. Chat.tsx's
    // useEffect watches for "stopped" and triggers the 600ms-delayed refetch
    // + flush-lag comparison.
    setState((s) =>
      s.contentDeltas.length > 0
        ? { ...s, status: "stopped" }
        : { ...s, status: "idle" }
    );
  }, []);

  const start = useCallback(
    async (chatId: number, payload: ChatStreamPayload): Promise<void> => {
      // Abort any in-flight stream before starting a new one.
      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;
      chatIdRef.current = chatId;

      // New generation — any async continuation from a PRIOR start() (e.g. a
      // late HTTP-error/fetch-error setState racing this call) becomes a
      // no-op from here on. The synchronous state reset directly below stays
      // UNGUARDED: it always applies, nothing could have superseded it yet.
      const myGen = runGuardRef.current.start();
      const guardedSetState: Dispatch<SetStateAction<StreamState>> = (
        updater,
      ) => {
        if (!runGuardRef.current.isCurrent(myGen)) return;
        setState(updater);
      };

      // Initialize timing for live stats.
      const timing: TimingRefs = {
        streamStartMs: performance.now(),
        firstTokenMs: null,
        deltaCount: 0,
      };

      setState({
        status: "streaming",
        messageId: null,
        responseId: null,
        contentDeltas: [],
        reasoningDeltas: [],
        toolCalls: [],
        error: null,
        stats: INITIAL_STATS,
        loadPhase: null,
        truncated_without_terminal: false,
        stop_reason: null,
        showContinue: false,
        warnings: [],
        followups: [],
        memorySaved: undefined,
        modeAdopt: undefined,
      });

      const ch = channelRef.current;

      try {
        const response = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: chatId, payload }),
          credentials: "same-origin",
          signal: ac.signal,
        });

        if (!response.ok) {
          const body = (await response.json().catch(() => ({}))) as {
            detail?: unknown;
          };
          const detail =
            typeof body.detail === "string"
              ? body.detail
              : typeof body.detail === "object" && body.detail !== null
                ? JSON.stringify(body.detail)
                : response.statusText;
          guardedSetState((s) => ({
            ...s,
            status: "error",
            error: { code: `http_${String(response.status)}`, message: detail },
          }));
          return;
        }

        if (response.body === null) {
          guardedSetState((s) => ({
            ...s,
            status: "error",
            error: { code: "no_body", message: "Response body was null" },
          }));
          return;
        }

        // `error` is terminal. `chat.end` marks the ANSWER complete but is
        // NOT the end of the stream: up to three optional out-of-band
        // frames follow it, always yielded in this order — `followups`
        // (chips), `mode_adopt` (C3 role adoption), then `memory.saved`
        // (the quiet auto-memory indicator, gated on the BE's own bounded
        // wait on the detached distillation task) — none of them delay
        // the visible answer (see streaming_service
        // `_generate_followups_oob` / `_infer_mode_oob` /
        // `_MEMORY_SAVED_FRAME_WAIT_SEC`). Keep reading past `chat.end`
        // until the LAST possible OOB frame arrives, or the server closes
        // the stream (readSseStream's `done`, e.g. all OOB frames
        // skipped/disabled this turn). `memory.saved` is always yielded
        // last of the three when it fires, so it — not `followups` or
        // `mode_adopt` — is the stop trigger; stopping on an earlier OOB
        // frame here would cancel the reader (and the underlying
        // connection) before the BE ever gets to yield the later ones,
        // silently losing them. A new message / unmount aborts any
        // lingering read via `abortRef`, so this never holds a connection
        // past its use.
        const { exhausted } = await readSseStream(response.body, (frame) => {
          let raw: RawEvent;
          try {
            raw = JSON.parse(frame.data ?? "") as RawEvent;
          } catch {
            // Malformed data line — skip; don't crash the stream.
            return "continue";
          }

          handleEvent(raw, chatId, guardedSetState, ch, timing);

          if (raw.type === "error" || raw.type === "memory.saved") {
            return "stop";
          }
          return "continue";
        });

        if (!exhausted) return;

        // Reader exhausted without a chat.end or error terminal. Two cases
        // to distinguish:
        //   (a) zero content deltas arrived → the BE silently dropped the
        //       stream (typical: a streaming_service.py stall leak, or LM
        //       Studio collapsing on context overflow). Surface as
        //       `status: "error"` with code `stream_truncated` so the user
        //       sees something actionable instead of a vanished indicator —
        //       which was the exact reported symptom.
        //   (b) content deltas arrived but no terminal → legitimate truncation
        //       (rare; cleanly-cut SSE mid-token). Keep `complete` but flag
        //       `truncated_without_terminal` for the Continue chip.
        guardedSetState((s) => {
          if (s.status !== "streaming") return s;
          if (s.contentDeltas.length === 0) {
            return {
              ...s,
              status: "error",
              error: {
                code: "stream_truncated",
                message:
                  "The model stopped sending before any reply arrived. " +
                  "Often this means the request exceeded the model's " +
                  "context window or the upstream connection dropped. " +
                  "Try again with fewer tools enabled or a model with a " +
                  "larger context window.",
              },
              loadPhase: null,
            };
          }
          return {
            ...s,
            status: "complete",
            loadPhase: null,
            truncated_without_terminal: true,
            // EOF-without-terminal is the second source for the unified
            // Continue affordance.
            showContinue: true,
          };
        });
      } catch (err: unknown) {
        if ((err as Error).name === "AbortError") {
          // User-initiated stop — state already set to idle by stop().
          return;
        }
        const msg =
          err instanceof Error ? err.message : "unknown streaming error";
        guardedSetState((s) => ({
          ...s,
          status: "error",
          error: { code: "fetch_error", message: msg },
        }));
      }
    },
    [],
  );

  const reset = useCallback((): void => {
    setState((s) => ({
      ...s,
      status: "idle",
      contentDeltas: [],
      reasoningDeltas: [],
      toolCalls: [],
      error: null,
      messageId: null,
      responseId: null,
      stats: INITIAL_STATS,
      loadPhase: null,
      truncated_without_terminal: false,
      stop_reason: null,
      showContinue: false,
      warnings: [],
      followups: [],
      memorySaved: undefined,
      modeAdopt: undefined,
    }));
  }, []);

  return { state, start, stop, reset };
}
