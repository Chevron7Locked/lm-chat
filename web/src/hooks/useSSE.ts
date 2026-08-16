/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSSE — chat-scoped selector wrapper around the streamStore.
 *
 * Historically this hook owned a fetch-based SSE state machine directly
 * (one `useState<StreamState>`, one `AbortController`, one run-generation
 * guard — see git history for the pre-2026-08-15 implementation). That
 * design meant exactly ONE hook instance backed EVERY chat (Chat.tsx
 * mounts `useSSE` once): starting a stream in chat B would unconditionally
 * abort whatever chat A still had in flight, and every consumer had to
 * defensively compare a `chatId` TAG on the shared state against its own
 * chatId to avoid attributing a background chat's frames to whatever chat
 * happened to be on screen.
 *
 * The state machine (fetch/SSE-parsing loop, per-event fold, wire types)
 * now lives in `@/stores/streamStore` as a chat-keyed Zustand store
 * (`streams: Record<chatId, StreamState>`), with per-chat AbortControllers
 * and run-generation guards kept beside it. This hook is now a THIN
 * selector: `useSSE(chatId)` reads `streams[chatId]` (or the idle default
 * when that chat has never streamed) and forwards `start`/`stop`/`reset`
 * to the store's actions. `start` still takes an explicit `chatId`
 * argument — callers use it to target a chat OTHER than the one currently
 * on screen (e.g. draining a queued message into its origin chat while a
 * different chat is being viewed); `stop`/`reset` act on the chat this
 * hook instance was called with.
 *
 * See `@/stores/streamStore`'s module doc for the wire format / event
 * fold / cross-tab BroadcastChannel design this file doesn't repeat.
 */
import { useCallback, useEffect } from "react";
import {
  useStreamStore,
  INITIAL_STREAM_STATE,
  getStreamChannel,
} from "@/stores/streamStore";
import type { StreamLifecycleMsg } from "@/stores/streamStore";

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
  /**
   * The chat this slot belongs to — the `chatId` argument the CURRENT (or
   * most recent) `start(chatId, …)` call was made with, or `null` before
   * any stream has ever started for this slot / after `reset()`.
   *
   * `state` now comes from `streamStore`'s chat-keyed `streams` record
   * (see that module's doc) — a slot for chat 5 only ever holds chat 5's
   * frames, so this field is normally redundant with the slot's own key.
   * It is kept for two reasons: (1) backward compatibility with consumers
   * written against the pre-2026-08-15 single-shared-instance design that
   * still defensively compare it against their own chatId
   * (useMtpSuspectedDedupe, useAutotitleEffect,
   * useStoppedStreamReconciliation, deriveMessageList — untouched by this
   * refactor, their checks are now provably redundant but remain valid
   * defense-in-depth); and (2) the cross-tab BroadcastChannel path, where
   * a peer tab's `stream_started` message sets it explicitly rather than
   * inferring it from which slot got written.
   */
  chatId: number | null;
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
   * in streamStore (not in Chat.tsx) so both chip sources share one state
   * machine.
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

/**
 * Every SSE event name the BE can put on the wire. One value-level artifact
 * so the type union AND the runtime contract test share a single source:
 *
 *   - the type `CanonicalEventType` is derived from this array, so
 *     streamStore's `handleEvent` assertNever default makes the switch
 *     exhaustive over exactly these names;
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

// ─── Hook ───────────────────────────────────────────────────────────────────

/**
 * Chat-scoped selector wrapper around `useStreamStore` — see this file's
 * module doc. `chatId` selects which slot of the store's `streams` record
 * `state` reads; `stop()`/`reset()` act on that same chatId; `start()`
 * still takes an explicit chatId argument (may differ from the chatId this
 * hook was called with — e.g. draining a queued message into its origin
 * chat from a different chat's view).
 */
export function useSSE(chatId: number | null): UseSSEReturn {
  const state = useStreamStore((s) =>
    chatId !== null ? (s.streams[chatId] ?? INITIAL_STREAM_STATE) : INITIAL_STREAM_STATE,
  );
  const start = useStreamStore((s) => s.start);
  const storeStop = useStreamStore((s) => s.stop);
  const storeReset = useStreamStore((s) => s.reset);

  const stop = useCallback((): void => {
    if (chatId !== null) storeStop(chatId);
  }, [chatId, storeStop]);

  const reset = useCallback((): void => {
    if (chatId !== null) storeReset(chatId);
  }, [chatId, storeReset]);

  // Cross-tab BroadcastChannel LISTENER. Posting (on chat.start/chat.end/
  // stop()) lives in streamStore.ts, which also owns the single
  // page-lifetime channel instance both sides share — see that module's
  // doc for why listening and posting deliberately use ONE
  // BroadcastChannel object (preserves the browser's non-self-delivery
  // guarantee the original single-`channelRef` hook relied on).
  //
  // Filtered to `chatId` — the chat THIS hook instance is currently
  // viewing — the same scoping the pre-refactor `chatIdRef`-filtered
  // listener had, just sourced from an explicit argument instead of an
  // imperative ref (there's no local `useState` left to filter for).
  // A peer-tab message about a chat that ISN'T on screen is not applied
  // to any slot by this hook; making the listener react to ALL chat_ids
  // regardless of what's on screen is a further step, out of scope here.
  useEffect(() => {
    const ch = getStreamChannel();
    if (ch === null || chatId === null) return;
    ch.onmessage = (ev: MessageEvent<StreamLifecycleMsg>) => {
      const msg = ev.data;
      if (msg.chat_id !== chatId) return;
      useStreamStore.getState().receiveCrossTabMessage(msg);
    };
    return () => {
      ch.onmessage = null;
    };
  }, [chatId]);

  return { state, start, stop, reset };
}
