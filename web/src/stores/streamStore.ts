/* SPDX-License-Identifier: Apache-2.0 */
/**
 * streamStore — chat-keyed Zustand store for lm-chat's main SSE stream.
 *
 * Ported from useSSE.ts (2026-08-15) to close a cross-chat data-loss bug:
 * useSSE() used to be a single hook instance (Chat.tsx:299) backing EVERY
 * chat with one `useState<StreamState>`, one `AbortController` and one
 * run-generation guard. `start()`'s first line unconditionally aborted
 * whatever was already in flight — so opening chat B while chat A was
 * still generating silently killed A's request (disconnect freezes
 * generation server-side; the work is lost). Every consumer instead relied
 * on a `chatId` TAG on the shared state plus a `sseState.chatId === chatId`
 * convention at each call site — a tag is not partitioned state, and one
 * consumer (useStoppedStreamReconciliation) had already been found to have
 * forgotten the check.
 *
 * This store makes chat-scoping structural instead of conventional:
 *
 *   - `streams: Record<chatId, StreamState>` — each chat gets its own slot.
 *     A component reading via a selector keyed on ITS chatId re-renders
 *     only for its own chat and can never observe another chat's frames.
 *   - AbortControllers live in a module-level `Map<chatId, AbortController>`
 *     beside the store, not in Zustand state — same reasoning as the old
 *     `abortRef`: not render-relevant. `start(B, …)` only ever aborts
 *     `abortControllers.get(B)`, never A's — the write-side fix for the
 *     cross-chat-abort bug above.
 *   - Run-generation guards (see lib/sseStream.ts `createRunGuard`) are
 *     likewise a module-level `Map<chatId, RunGuard>`, so a stream
 *     superseded by a NEW start() for the SAME chat still has its late
 *     setState calls discarded, exactly as before — but a start() for a
 *     DIFFERENT chat cannot invalidate another chat's guard.
 *
 * This mirrors the existing chat-keyed pattern in titleGenerationStore.ts /
 * chatSettingsStore.ts / useChatPresetStore (useChatPreset.ts) — a
 * `Record<number, T>` Zustand store read via a per-chat selector — rather
 * than introducing a new idiom.
 *
 * `useSSE(chatId)` (see hooks/useSSE.ts) is a thin selector wrapper around
 * this store: it reads `streams[chatId]` and forwards `start`/`stop`/
 * `reset` to the actions below. The wire parsing (`handleEvent`, the SSE
 * event union, the fetch/read loop) is ported verbatim from useSSE.ts —
 * see that file's original module doc for the wire format / state-machine
 * background this file doesn't repeat.
 *
 * Cross-tab BroadcastChannel: this store owns the single page-lifetime
 * channel (`getStreamChannel`) used to POST stream-lifecycle messages
 * (`chat.start` → stream_started, `chat.end`/stop() → chat_end/aborted).
 * The LISTENING side (subscribing to peer-tab messages and folding them
 * into a chat's slot via `receiveCrossTabMessage`) is wired from useSSE's
 * per-mount effect, filtered to the chat it's currently viewing — same
 * scoping useSSE.ts's `chatIdRef`-filtered listener had, just re-homed
 * onto an explicit `chatId` argument instead of an imperative ref (there
 * is no local `useState` left in the hook for a listener to write into).
 * Listening and posting deliberately share ONE BroadcastChannel instance
 * (not two) to preserve the browser's non-self-delivery guarantee a
 * channel doesn't receive its own posted messages, exactly as the
 * original single-`channelRef` hook relied on. A genuinely multi-chat-
 * aware cross-tab listener (reacting to peer messages about a chat that
 * ISN'T the one currently on screen) is out of scope here — see the
 * accompanying report.
 */
import { create } from "zustand";
import { createRunGuard, foldToolCallStart, readSseStream } from "@/lib/sseStream";
import type { RunGuard, SseFrame } from "@/lib/sseStream";
import type {
  ChatStreamPayload,
  StreamState,
  StreamStats,
  ToolCall,
  CanonicalEventType,
} from "@/hooks/useSSE";

// ─── BroadcastChannel wrapper (ported verbatim from useSSE.ts) ────────────

/** Cross-tab stream-lifecycle message shape — posted by this store's
 *  actions, consumed by useSSE's per-mount listener effect. */
export type StreamLifecycleMsg =
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

let sharedChannel: BroadcastChannel | null | undefined;

/**
 * The single page-lifetime BroadcastChannel used for cross-tab stream
 * coordination. Lazily created on first use, never closed — a module
 * singleton with the same lifetime as this store, unlike the old
 * `channelRef` which opened/closed with the (single) hook's mount cycle.
 * Returns null when BroadcastChannel is unavailable (same graceful
 * degradation `openChannel`'s try/catch always had).
 */
export function getStreamChannel(): BroadcastChannel | null {
  sharedChannel ??= openChannel();
  return sharedChannel;
}

// ─── localStorage helpers (ported verbatim from useSSE.ts) ────────────────

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

// ─── SSE event dispatcher (ported verbatim from useSSE.ts) ────────────────

interface RawEvent {
  type: CanonicalEventType;
  msg_id?: number | undefined;
  response_id?: string | undefined;
  stop_reason?: string | undefined;
  delta?: string | undefined;
  content?: string | undefined;
  tool_call?:
    | {
        id?: string;
        name?: string;
        arguments?: Record<string, unknown> | null;
        call_id?: string | null;
        result?: string | null;
      }
    | undefined;
  total_output_tokens?: number | undefined;
  tokens_per_second?: number | undefined;
  code?: string | undefined;
  message?: string | undefined;
  progress?: number | undefined;
  error?: {
    code?: string;
    message?: string;
    cumulative_tool_rounds?: number;
    hint?: string;
  };
  warning?: {
    code?: string;
    message?: string;
  };
  followups?: string[] | undefined;
  count?: number | undefined;
  preset_id?: string | null | undefined;
}

/** Timing refs — mutable object avoids prop-drilling through handleEvent. */
interface TimingRefs {
  streamStartMs: number;
  firstTokenMs: number | null;
  deltaCount: number;
}

/**
 * Bridge the wire tool_call.arguments DICT into the FE
 * `ToolCall.arguments: string` shape. Returns null for absent/empty dicts
 * so callers keep the previously accumulated value.
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

/** Exhaustiveness guard for handleEvent's switch — see useSSE.ts's
 *  `assertNever` for the full rationale (unchanged here). */
function assertNever(x: never): never {
  throw new Error(
    `streamStore: unhandled SSE event type ${JSON.stringify(x)} — add a ` +
      "handleEvent case (or the explicit ignore allow-list) and update " +
      "web/src/types/sse-event-names.json.",
  );
}

/** Mutate a chat's slot via an updater — the store-keyed replacement for
 *  the hook's `setState((s) => ({...s, ...}))` calls. */
type StreamUpdate = (updater: (s: StreamState) => StreamState) => void;

function handleEvent(
  raw: RawEvent,
  chatId: number,
  update: StreamUpdate,
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
      update((s) => ({
        ...s,
        messageId: raw.msg_id ?? s.messageId,
        responseId: raw.response_id ?? s.responseId,
      }));
      break;

    case "message.delta": {
      const nowMs = performance.now();
      timing.firstTokenMs ??= nowMs;
      timing.deltaCount += 1;
      const elapsedS = (nowMs - timing.streamStartMs) / 1000;
      const tps = elapsedS > 0 ? timing.deltaCount / elapsedS : null;
      const ttft = (timing.firstTokenMs - timing.streamStartMs) / 1000;
      update((s) => ({
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
      update((s) => ({
        ...s,
        reasoningDeltas: [...s.reasoningDeltas, raw.delta ?? raw.content ?? ""],
      }));
      break;

    case "tool_call.start": {
      const tc: ToolCall = {
        id: raw.tool_call?.id ?? `tc-${String(Date.now())}`,
        name: raw.tool_call?.name ?? "",
        arguments: "",
        status: "pending",
      };
      update((s) => ({
        ...s,
        toolCalls: foldToolCallStart(s.toolCalls, tc),
      }));
      break;
    }

    case "tool_call.name":
      update((s) => ({
        ...s,
        toolCalls: s.toolCalls.map((tc) =>
          tc.id === raw.tool_call?.id
            ? { ...tc, name: raw.tool_call.name ?? tc.name }
            : tc,
        ),
      }));
      break;

    case "tool_call.arguments":
      update((s) => ({
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
      update((s) => ({
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
      update((s) => ({
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
      const finalTtft =
        timing.firstTokenMs !== null
          ? (timing.firstTokenMs - timing.streamStartMs) / 1000
          : null;
      const finalElapsedS = (performance.now() - timing.streamStartMs) / 1000;
      const finalTps =
        finalElapsedS > 0 && timing.deltaCount > 0
          ? timing.deltaCount / finalElapsedS
          : null;
      update((s) => ({
        ...s,
        status: "complete",
        loadPhase: null,
        stop_reason: raw.stop_reason ?? null,
        showContinue:
          raw.stop_reason === "length" || s.truncated_without_terminal,
        responseId: raw.response_id ?? s.responseId,
        stats: {
          tokensPerSecond: raw.tokens_per_second ?? finalTps,
          ttftSeconds: finalTtft,
          outputTokens: raw.total_output_tokens ?? timing.deltaCount,
        },
      }));
      break;
    }

    case "warning":
      update((s) => ({
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
      update((s) => ({
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

    case "model_load.start":
      update((s) => ({
        ...s,
        loadPhase: { phase: "model_load", progress: null },
      }));
      break;

    case "model_load.progress":
      update((s) => ({
        ...s,
        loadPhase: { phase: "model_load", progress: raw.progress ?? null },
      }));
      break;

    case "model_load.end":
      update((s) => ({
        ...s,
        loadPhase: { phase: "generating", progress: null },
      }));
      break;

    case "prompt_processing.start":
      update((s) => ({
        ...s,
        loadPhase: { phase: "prompt_processing", progress: null },
      }));
      break;

    case "prompt_processing.progress":
      update((s) => ({
        ...s,
        loadPhase: {
          phase: "prompt_processing",
          progress: raw.progress ?? null,
        },
      }));
      break;

    case "prompt_processing.end":
      update((s) => ({
        ...s,
        loadPhase: { phase: "generating", progress: null },
      }));
      break;

    case "message.start":
      update((s) => ({
        ...s,
        loadPhase: { phase: "generating", progress: null },
      }));
      break;

    case "followups":
      update((s) => ({
        ...s,
        followups: Array.isArray(raw.followups) ? raw.followups : [],
      }));
      break;

    case "memory.saved": {
      const count = raw.count ?? 0;
      const msgId = raw.msg_id;
      if (count > 0 && msgId !== undefined) {
        update((s) => ({
          ...s,
          memorySaved: { count, msgId },
        }));
      }
      break;
    }

    case "mode_adopt": {
      const msgId = raw.msg_id;
      if (msgId !== undefined) {
        update((s) => ({
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
      assertNever(raw.type);
  }
}

// ─── Store ──────────────────────────────────────────────────────────────

const INITIAL_STATS: StreamStats = {
  tokensPerSecond: null,
  ttftSeconds: null,
  outputTokens: 0,
};

/** A chat that has never had a stream slot allocated reads as this —
 *  exported so useSSE(chatId)'s selector can fall back to it. */
export const INITIAL_STREAM_STATE: StreamState = {
  status: "idle",
  chatId: null,
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

// AbortControllers and run-generation guards are keyed per-chat but kept
// OUTSIDE Zustand state — same reasoning `abortRef` had in useSSE.ts: not
// render-relevant, and mutating a Map in place must never trigger a
// selector re-render. Module-level (not per-store-instance) since
// `useStreamStore` itself is a page-lifetime singleton — there is only
// ever one of these maps, matching there being only one store.
const abortControllers = new Map<number, AbortController>();
const runGuards = new Map<number, RunGuard>();

function getRunGuard(chatId: number): RunGuard {
  let guard = runGuards.get(chatId);
  if (guard === undefined) {
    guard = createRunGuard();
    runGuards.set(chatId, guard);
  }
  return guard;
}

/**
 * Test-only: wipe this module's page-lifetime singletons between tests.
 *
 * Without this, `sharedChannel` in particular goes stale across test
 * files that stub `global.BroadcastChannel` per-test (`getStreamChannel`'s
 * `??=` cache means the FIRST test to call it wins for the rest of the
 * run, so a later test's fresh `StubChannel` class never gets
 * instantiated). `abortControllers`/`runGuards` don't currently cause
 * cross-test failures (each test's own chatId numbers rarely collide
 * across files), but resetting them alongside keeps this one function the
 * complete undo of everything `start()` accumulates. Mirrors the
 * `__resetChatScopedMemoryForTests` pattern in useChatScopedState.ts.
 */
export function __resetStreamStoreForTests(): void {
  sharedChannel = undefined;
  abortControllers.clear();
  runGuards.clear();
  useStreamStore.setState({ streams: {} });
}

export interface StreamStoreState {
  streams: Record<number, StreamState>;
  start: (chatId: number, payload: ChatStreamPayload) => Promise<void>;
  stop: (chatId: number) => void;
  reset: (chatId: number) => void;
  /** Fold an incoming cross-tab BroadcastChannel message into `msg.chat_id`'s
   *  slot. Called from useSSE's per-mount listener effect — see this file's
   *  module doc for why the SUBSCRIPTION stays hook-side while the FOLD
   *  lives here. */
  receiveCrossTabMessage: (msg: StreamLifecycleMsg) => void;
}

export const useStreamStore = create<StreamStoreState>((set) => {
  const updateStream = (
    chatId: number,
    updater: (s: StreamState) => StreamState,
  ): void => {
    set((state) => ({
      streams: {
        ...state.streams,
        [chatId]: updater(state.streams[chatId] ?? INITIAL_STREAM_STATE),
      },
    }));
  };

  return {
    streams: {},

    start: async (chatId, payload) => {
      // Abort any in-flight stream for THIS chat only — starting chat B
      // can no longer abort chat A's AbortController (finding #1 fix).
      abortControllers.get(chatId)?.abort();
      const ac = new AbortController();
      abortControllers.set(chatId, ac);

      // New generation for THIS chat — a late setState from a PRIOR
      // start() call on the SAME chatId becomes a no-op from here on. A
      // start() for a DIFFERENT chatId uses its own guard and cannot
      // invalidate this one.
      const guard = getRunGuard(chatId);
      const myGen = guard.start();
      const guardedUpdate: StreamUpdate = (updater) => {
        if (!guard.isCurrent(myGen)) return;
        updateStream(chatId, updater);
      };

      const timing: TimingRefs = {
        streamStartMs: performance.now(),
        firstTokenMs: null,
        deltaCount: 0,
      };

      // Synchronous baseline reset — UNGUARDED: it always applies, nothing
      // could have superseded it yet.
      updateStream(chatId, () => ({
        status: "streaming",
        chatId,
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
      }));

      const ch = getStreamChannel();

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
          guardedUpdate((s) => ({
            ...s,
            status: "error",
            error: { code: `http_${String(response.status)}`, message: detail },
          }));
          return;
        }

        if (response.body === null) {
          guardedUpdate((s) => ({
            ...s,
            status: "error",
            error: { code: "no_body", message: "Response body was null" },
          }));
          return;
        }

        // See useSSE.ts's original doc for why the loop keeps reading PAST
        // chat.end (followups / mode_adopt / memory.saved OOB frames).
        const { exhausted } = await readSseStream(response.body, (frame: SseFrame) => {
          let raw: RawEvent;
          try {
            raw = JSON.parse(frame.data ?? "") as RawEvent;
          } catch {
            return "continue";
          }

          handleEvent(raw, chatId, guardedUpdate, ch, timing);

          if (raw.type === "error" || raw.type === "memory.saved") {
            return "stop";
          }
          return "continue";
        });

        if (!exhausted) return;

        guardedUpdate((s) => {
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
            showContinue: true,
          };
        });
      } catch (err: unknown) {
        if ((err as Error).name === "AbortError") {
          // User-initiated stop — state already set to idle/stopped by stop().
          return;
        }
        const msg =
          err instanceof Error ? err.message : "unknown streaming error";
        guardedUpdate((s) => ({
          ...s,
          status: "error",
          error: { code: "fetch_error", message: msg },
        }));
      }
    },

    stop: (chatId) => {
      getRunGuard(chatId).invalidate();
      abortControllers.get(chatId)?.abort();
      abortControllers.delete(chatId);
      clearMsgId(chatId);
      const ch = getStreamChannel();
      if (ch !== null) {
        const msg: StreamLifecycleMsg = { type: "aborted", chat_id: chatId };
        ch.postMessage(msg);
      }
      // Transition to "stopped" rather than "idle" so the in-memory partial
      // (contentDeltas) stays visible with a "Stopped" chip — see
      // useStoppedStreamReconciliation.ts.
      updateStream(chatId, (s) =>
        s.contentDeltas.length > 0
          ? { ...s, status: "stopped" }
          : { ...s, status: "idle" },
      );
    },

    reset: (chatId) => {
      updateStream(chatId, (s) => ({
        ...s,
        status: "idle",
        chatId: null,
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
    },

    receiveCrossTabMessage: (msg) => {
      if (msg.type === "stream_started") {
        // Another tab started streaming this chat — show as streaming.
        // chatId is set here too so this cross-tab path keeps
        // state.chatId consistent with the locally-started path.
        updateStream(msg.chat_id, (s) =>
          s.status !== "streaming"
            ? { ...s, status: "streaming", chatId: msg.chat_id, messageId: msg.msg_id }
            : s,
        );
      } else if (msg.type === "chat_end" || msg.type === "aborted") {
        updateStream(msg.chat_id, (s) =>
          s.status === "streaming" ? { ...s, status: "complete" } : s,
        );
      }
    },
  };
});
