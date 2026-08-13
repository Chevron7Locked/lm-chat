/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSubSessionSSE — streaming hook for slash-command sub-sessions.
 *
 * Sends a POST to /api/chats/{chatId}/sub-session/stream (or /finalize) and
 * streams the response.  The backend calls LM Studio with ONLY the preset
 * system_prompt + sub-session messages — main chat history is excluded.
 *
 * SSE events emitted by the backend:
 *   sub.delta                — { delta: string }
 *   sub.complete             — { final_content: string, truncated?: boolean,
 *                               truncation_reason?: string, truncation_hint?: string }
 *   sub.error                — { code: string, message: string, hint?: string,
 *                               tally?: Record<string,number>, accumulated_chars?: number,
 *                               truncated?: boolean }
 *   sub.reasoning.start      — {}
 *   sub.reasoning.delta      — { delta: string }
 *   sub.reasoning.end        — {}
 *   sub.tool_call.start      — { id, name?, arguments? }
 *   sub.tool_call.name       — { id, name }
 *   sub.tool_call.arguments  — { id, arguments }
 *   sub.tool_call.success    — { id, result? }
 *   sub.tool_call.failure    — { id, error? }
 *
 * Unrecognised sub.* events are logged at debug level and skipped.
 */

import { useCallback, useRef, useState } from "react";
import { createRunGuard, foldToolCallStart, readSseStream } from "@/lib/sseStream";

type SubSessionStatus = "idle" | "streaming" | "complete" | "error";

interface SubSessionToolCall {
  id: string;
  name: string;
  arguments: string;
  status: "pending" | "success" | "failure";
  result?: string;
}

/** Structured error payload matching the canonical sub.error BE envelope. */
interface SubSessionError {
  code: string;
  message: string;
  hint?: string;
  truncated?: boolean;
  tally?: Record<string, number>;
}

export interface SubSessionSSEState {
  status: SubSessionStatus;
  content: string;
  reasoning_content: string | null;
  error: SubSessionError | null;
  /** Tool calls observed during this stream, in arrival order. */
  toolCalls: SubSessionToolCall[];
  /** True when the BE signalled the stream was truncated. */
  truncated?: boolean;
  /** Machine reason for truncation (e.g. "token_limit"). */
  truncation_reason?: string;
  /** Human-readable hint for the user about the truncation. */
  truncation_hint?: string;
}

export interface UseSubSessionSSE {
  state: SubSessionSSEState;
  stream: (params: SubSessionStreamParams) => void;
  finalize: (params: SubSessionStreamParams) => void;
  abort: () => void;
  reset: () => void;
}

export interface SubSessionMessage {
  role: "user" | "assistant";
  content: string;
  /**
   * Server-assigned `sub_session_messages.id` — set only on messages
   * hydrated from a restored transcript (`GET
   * .../sub-sessions/{sub_session_id}`, P3 restore-on-load). Messages typed
   * during a live session are undefined here: the SSE stream doesn't echo
   * row ids back to the FE (that's P4's continuation-param territory).
   * Lets SubSessionPanel key rehydrated turns on a stable id instead of
   * array index.
   */
  id?: number;
}

export interface SubSessionStreamParams {
  chatId: number;
  modelId: string;
  /**
   * Provider slug for the sub-session. When set to a cloud slug (e.g.
   * "openrouter") the backend routes to that provider instead of LM Studio.
   * Omit or set to "lmstudio" for the existing LM Studio path.
   * Corresponds to the optional `provider` form field accepted by
   * POST /api/chats/{id}/sub-session/stream (236cc54).
   */
  provider?: string;
  /**
   * Slash-command preset id (e.g. "research", "coder") — forwarded as the
   * `preset_id` form field so the BE can persist the real discriminator on
   * `sub_sessions.preset_id` and derive the row's title, instead of the
   * `_SUB_SESSION_PRESET_ID_UNSPECIFIED` placeholder. Omit only for call
   * sites that genuinely have no preset context.
   */
  presetId?: string;
  systemPrompt: string;
  messages: SubSessionMessage[];
  /** Optional integration ids forwarded to LM Studio (e.g.
   *  ``["mcp/context7", "mcp/deepwiki"]``). Without these, the
   *  sub-session has no tools and the model is told so via the prompt. */
  integrations?: string[];
  /**
   * P4 reopen + continue: the `sub_sessions.id` to APPEND this turn onto,
   * forwarded as the `sub_session_id` form field. Omit (the default) for
   * the create-new path — every existing call site is unaffected.
   */
  subSessionId?: number;
  onComplete?: (finalContent: string) => void;
}

/**
 * Convert a snake_case error code from the backend into a readable sentence.
 * Known codes get friendly copy; unknown codes are title-cased from the slug.
 */
function formatSubErrorCode(code: string): string {
  const known: Record<string, string> = {
    tool_format_generation_error:
      "Tool format error — the model produced a malformed tool call.",
    upstream_error: "Upstream error from LM Studio.",
    stream_error: "Stream error — the connection was interrupted.",
    no_model_selected: "No model selected — pick a model in the top bar.",
    stream_truncated: "Stream ended early — partial content may be shown.",
    stream_ended_unexpectedly: "Stream ended without a completion signal.",
    upstream_connection_lost: "Connection to LM Studio was lost.",
    no_final_content: "No response content was produced.",
    decode_error: "Stream decode error — response could not be parsed.",
  };
  const match = known[code];
  if (match !== undefined) return match;
  // Generic: replace underscores/hyphens with spaces, capitalize first letter.
  return code.replace(/[_-]/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

const IDLE: SubSessionSSEState = {
  status: "idle",
  content: "",
  reasoning_content: null,
  error: null,
  toolCalls: [],
};

/** Synthesise a SubSessionError from a non-SSE failure string (HTTP errors, etc.). */
function _makeStringError(message: string): SubSessionError {
  return { code: "client_error", message };
}

export function useSubSessionSSE(): UseSubSessionSSE {
  const [state, setState] = useState<SubSessionSSEState>(IDLE);
  const abortRef = useRef<AbortController | null>(null);
  // fe-components-state-9 — run-generation guard (lib/sseStream.ts):
  // discards stale setState callbacks from a previous stream call that was
  // aborted while its async loop was still running. Was a local
  // `streamSeqRef`/`guardedSetState` pair here — the ONE hook that had this
  // guard before extraction — now shared with useSSE and useABStream.
  const runGuardRef = useRef(createRunGuard());

  const _stream = useCallback(
    (params: SubSessionStreamParams, endpoint: "stream" | "finalize"): void => {
      // Correction 8: abort + guard-bump BEFORE the early-return guard so an
      // in-flight stream is always cancelled when a new _stream() call arrives,
      // even if the new call fails validation. Without this, the prior stream's
      // stale setState callbacks continue landing after the "No model selected"
      // error and overwrite it.
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const myGen = runGuardRef.current.start();

      if (!params.modelId || params.modelId.trim() === "") {
        console.warn("[sub-session] refusing to stream — empty model_id");
        setState({
          status: "error",
          content: "",
          reasoning_content: null,
          error: {
            code: "no_model_selected",
            message:
              "No model selected — pick a model in the top bar to use research mode.",
          },
          toolCalls: [],
        });
        return;
      }

      // Capture this stream's generation. Any setState callback that fires
      // after a subsequent _stream() call checks against this capture and
      // no-ops when stale.
      // (myGen and ctrl are already captured above, before the early-return guard.)
      const guardedSetState = (
        updater: (prev: SubSessionSSEState) => SubSessionSSEState,
      ): void => {
        if (!runGuardRef.current.isCurrent(myGen)) return;
        setState(updater);
      };

      setState({
        status: "streaming",
        content: "",
        reasoning_content: null,
        error: null,
        toolCalls: [],
      });

      const form = new FormData();
      form.append("model_id", params.modelId);
      // Send provider when it is set and differs from the LM Studio default.
      // The backend treats "lmstudio" and absent as equivalent; omitting it
      // preserves backward compatibility with existing call sites that don't
      // pass a provider.
      if (params.provider !== undefined && params.provider !== "" && params.provider !== "lmstudio") {
        form.append("provider", params.provider);
      }
      if (params.presetId !== undefined && params.presetId !== "") {
        form.append("preset_id", params.presetId);
      }
      form.append("system_prompt", params.systemPrompt);
      form.append("messages_json", JSON.stringify(params.messages));
      // Send integrations whenever the caller supplies the field — including
      // an explicit empty array, which signals "all tools off" to the BE.
      // Omitting the field entirely (undefined) lets the BE apply admin
      // defaults; sending [] is the intentional "user disabled everything" signal.
      if (params.integrations !== undefined) {
        form.append("integrations", JSON.stringify(params.integrations));
      }
      // P4: continue an existing sub-session (append) instead of creating
      // a new one — see SubSessionStreamParams.subSessionId.
      if (params.subSessionId !== undefined) {
        form.append("sub_session_id", String(params.subSessionId));
      }

      void (async () => {
        try {
          const res = await fetch(
            `/api/chats/${String(params.chatId)}/sub-session/${endpoint}`,
            {
              method: "POST",
              body: form,
              signal: ctrl.signal,
              credentials: "same-origin",
            },
          );
          if (!res.ok || res.body === null) {
            let detail = `HTTP ${String(res.status)}`;
            try {
              const body = (await res.clone().json()) as {
                detail?: { message?: string } | string | null;
              };
              if (
                typeof body.detail === "object" &&
                body.detail !== null &&
                "message" in body.detail
              ) {
                detail = `${detail} — ${body.detail.message ?? ""}`.trim();
              } else if (typeof body.detail === "string") {
                detail = `${detail} — ${body.detail}`;
              }
            } catch {
              // Body wasn't JSON; keep the bare status code.
            }
            console.error("[sub-session] stream failed:", detail);
            guardedSetState((prev) => ({
              ...prev,
              status: "error",
              content: "",
              error: _makeStringError(detail),
            }));
            return;
          }

          let accumulated = "";
          // #3: Track whether we saw a terminating event so we can detect
          // EOF-without-terminator and emit a real error rather than silently
          // completing. A mutable object (not two bare `let` booleans) —
          // both flags are flipped from inside the `onFrame` closure below;
          // TS's control-flow narrowing treats a closure-captured `let`
          // primitive as staying at its initializer's literal type at the
          // read site outside the closure, which made
          // `@typescript-eslint/no-unnecessary-condition` (wrongly) flag
          // `!sawComplete`/`!sawError` as always-truthy. An object property
          // isn't narrowed the same way.
          const terminal = { complete: false, error: false };

          // fe-components-state-9 — the reader loop, chunk buffering, and
          // frame boundary detection now live in the shared
          // `readSseStream` (lib/sseStream.ts). It buffers until a full
          // "\n\n"-terminated block has arrived before parsing, which
          // supersedes this hook's old hand-rolled fix — hoisting
          // `eventType` outside a per-line loop so a chunk boundary landing
          // between an `event:` line and its `data:` line didn't
          // misattribute the frame. Buffering until the terminator means
          // that class of bug can't occur here: nothing is parsed until the
          // whole block is in hand.
          const { exhausted } = await readSseStream(res.body, (frame) => {
            const eventType = frame.event ?? "";
            const raw = (frame.data ?? "").trim();
            if (!raw) return "continue";

            try {
              // #9: type payload as Record<string, unknown> and guard fields.
              const obj = JSON.parse(raw) as Record<string, unknown>;

              if (eventType === "sub.delta") {
                    const delta =
                      typeof obj.delta === "string" ? obj.delta : "";
                    if (delta) {
                      accumulated += delta;
                      const snap = accumulated;
                      guardedSetState((prev) => ({ ...prev, content: snap }));
                    }
                  } else if (eventType === "sub.reasoning.start") {
                    guardedSetState((prev) => ({
                      ...prev,
                      reasoning_content: "",
                    }));
                  } else if (eventType === "sub.reasoning.delta") {
                    const delta =
                      typeof obj.delta === "string" ? obj.delta : "";
                    if (delta) {
                      guardedSetState((prev) => ({
                        ...prev,
                        reasoning_content:
                          (prev.reasoning_content ?? "") + delta,
                      }));
                    }
                  } else if (eventType === "sub.reasoning.end") {
                    // no-op — reasoning_content is already accumulated
                  } else if (eventType === "sub.tool_call.start") {
                    const id =
                      typeof obj.id === "string"
                        ? obj.id
                        : typeof obj.id === "number" ||
                            typeof obj.id === "boolean"
                          ? String(obj.id)
                          : "";
                    const name = typeof obj.name === "string" ? obj.name : "";
                    const args =
                      typeof obj.arguments === "string" ? obj.arguments : "";
                    const next: SubSessionToolCall = {
                      id,
                      name,
                      arguments: args,
                      status: "pending",
                    };
                    // fe-components-state-9: was a local upsert-by-id inline
                    // here (the one hook that already had it right) — now
                    // shared with useSSE via `foldToolCallStart`.
                    guardedSetState((prev) => ({
                      ...prev,
                      toolCalls: foldToolCallStart(prev.toolCalls, next),
                    }));
                  } else if (eventType === "sub.tool_call.name") {
                    const id =
                      typeof obj.id === "string"
                        ? obj.id
                        : typeof obj.id === "number" ||
                            typeof obj.id === "boolean"
                          ? String(obj.id)
                          : "";
                    const name = typeof obj.name === "string" ? obj.name : "";
                    guardedSetState((prev) => ({
                      ...prev,
                      toolCalls: prev.toolCalls.map((tc) =>
                        tc.id === id ? { ...tc, name } : tc,
                      ),
                    }));
                  } else if (eventType === "sub.tool_call.arguments") {
                    const id =
                      typeof obj.id === "string"
                        ? obj.id
                        : typeof obj.id === "number" ||
                            typeof obj.id === "boolean"
                          ? String(obj.id)
                          : "";
                    const args =
                      typeof obj.arguments === "string" ? obj.arguments : "";
                    guardedSetState((prev) => ({
                      ...prev,
                      toolCalls: prev.toolCalls.map((tc) =>
                        tc.id === id
                          ? { ...tc, arguments: tc.arguments + args }
                          : tc,
                      ),
                    }));
                  } else if (eventType === "sub.tool_call.success") {
                    const id =
                      typeof obj.id === "string"
                        ? obj.id
                        : typeof obj.id === "number" ||
                            typeof obj.id === "boolean"
                          ? String(obj.id)
                          : "";
                    const resultStr =
                      typeof obj.result === "string" ? obj.result : undefined;
                    guardedSetState((prev) => ({
                      ...prev,
                      toolCalls: prev.toolCalls.map((tc) => {
                        if (tc.id !== id) return tc;
                        const next: SubSessionToolCall = {
                          ...tc,
                          status: "success",
                        };
                        if (resultStr !== undefined) next.result = resultStr;
                        return next;
                      }),
                    }));
                  } else if (eventType === "sub.tool_call.failure") {
                    const id =
                      typeof obj.id === "string"
                        ? obj.id
                        : typeof obj.id === "number" ||
                            typeof obj.id === "boolean"
                          ? String(obj.id)
                          : "";
                    // Correction 2: BE emits error as a dict {code, tool, message, output}.
                    // Accept both dict and string shapes so the panel always gets real detail.
                    let errMsg: string;
                    if (typeof obj.error === "string") {
                      errMsg = obj.error;
                    } else if (
                      typeof obj.error === "object" &&
                      obj.error !== null
                    ) {
                      const e = obj.error as Record<string, unknown>;
                      const rawCode =
                        typeof e.code === "string" && e.code !== ""
                          ? e.code
                          : "unknown_tool_failure";
                      const codeStr = formatSubErrorCode(rawCode);
                      const msgStr =
                        typeof e.message === "string" ? e.message : "";
                      const outStr =
                        typeof e.output === "string"
                          ? e.output
                          : e.output != null
                            ? JSON.stringify(e.output)
                            : "";
                      errMsg = [codeStr, msgStr, outStr]
                        .filter(Boolean)
                        .join(" — ");
                      if (!errMsg) errMsg = "tool failed";
                    } else {
                      errMsg = "tool failed";
                    }
                    guardedSetState((prev) => ({
                      ...prev,
                      toolCalls: prev.toolCalls.map((tc) =>
                        tc.id === id
                          ? { ...tc, status: "failure", result: errMsg }
                          : tc,
                      ),
                    }));
                  } else if (eventType === "sub.complete") {
                    // #5: Expose truncation metadata from sub.complete.
                    const finalContent =
                      typeof obj.final_content === "string"
                        ? obj.final_content
                        : accumulated;
                    const truncated = obj.truncated === true;
                    const truncation_reason =
                      typeof obj.truncation_reason === "string"
                        ? obj.truncation_reason
                        : undefined;
                    const truncation_hint =
                      typeof obj.truncation_hint === "string"
                        ? obj.truncation_hint
                        : undefined;
                    terminal.complete = true;
                    guardedSetState((prev) => {
                      const next: SubSessionSSEState = {
                        ...prev,
                        status: "complete",
                        content: finalContent,
                        error: null,
                      };
                      if (truncated) {
                        next.truncated = true;
                        if (truncation_reason !== undefined)
                          next.truncation_reason = truncation_reason;
                        if (truncation_hint !== undefined)
                          next.truncation_hint = truncation_hint;
                      }
                      return next;
                    });
                    params.onComplete?.(finalContent);
                    return "stop";
                  } else if (eventType === "sub.error") {
                    // BE emits { code, message, hint?, tally?, accumulated_chars?, truncated? }.
                    // Correction 3: preserve ALL canonical fields on the error state object
                    // so Chat.tsx toast can render code + message + hint.
                    const rawCode =
                      typeof obj.code === "string" ? obj.code : "";
                    const code = rawCode !== "" ? rawCode : "unknown_error";
                    if (rawCode === "")
                      console.error(
                        "[sub-session] sub.error missing canonical code",
                        obj,
                      );
                    const message =
                      typeof obj.message === "string"
                        ? obj.message
                        : rawCode !== ""
                          ? formatSubErrorCode(code)
                          : "Research session ended unexpectedly — try again.";
                    const hint =
                      typeof obj.hint === "string" ? obj.hint : undefined;
                    const truncated = obj.truncated === true;
                    const tally =
                      typeof obj.tally === "object" && obj.tally !== null
                        ? (obj.tally as Record<string, number>)
                        : undefined;
                    const structuredError: SubSessionError = { code, message };
                    if (hint !== undefined) structuredError.hint = hint;
                    if (truncated) structuredError.truncated = truncated;
                    if (tally !== undefined) structuredError.tally = tally;
                    terminal.error = true;
                    guardedSetState((prev) => ({
                      ...prev,
                      status: "error",
                      content: accumulated,
                      error: structuredError,
                    }));
                    return "stop";
                  } else if (eventType.startsWith("sub.")) {
                    // Unrecognised sub.* event from a newer BE — log and skip.
                    console.debug(
                      "[sub-session] unrecognised event:",
                      eventType,
                      obj,
                    );
                  }
              } catch (err) {
                // #9: surface parse failures in DevTools instead of swallowing silently.
                console.error(
                  "[sub-session] SSE parse error for event",
                  eventType,
                  ":",
                  err,
                );
              }
              return "continue";
            });

          // #3: EOF without sub.complete or sub.error — emit a real error
          // instead of silently completing. Do NOT call onComplete.
          // (`exhausted` is implied by `!terminal.complete && !terminal.error`
          // — both terminal branches signal "stop" before setting their
          // flag — but checking it explicitly matches useSSE/useABStream's
          // style and guards against future drift.)
          if (exhausted && !terminal.complete && !terminal.error) {
            guardedSetState((prev) => ({
              ...prev,
              status: "error",
              content: accumulated,
              error: {
                code: "stream_truncated",
                message: formatSubErrorCode("stream_truncated"),
              },
              truncated: true,
              truncation_reason: "stream_truncated",
            }));
          }
        } catch (err) {
          if ((err as Error).name !== "AbortError") {
            console.error("[sub-session] stream fetch error:", err);
            const errMsg =
              err instanceof Error ? err.message : JSON.stringify(err);
            guardedSetState((prev) => ({
              ...prev,
              status: "error",
              content: prev.content,
              error: _makeStringError(errMsg),
            }));
          }
        }
      })();
    },
    [],
  );

  const stream = useCallback(
    (params: SubSessionStreamParams) => {
      _stream(params, "stream");
    },
    [_stream],
  );

  const finalize = useCallback(
    (params: SubSessionStreamParams) => {
      _stream(params, "finalize");
    },
    [_stream],
  );

  // #18: abort and reset share one cleanup path. Both abort the controller
  // and reset state to IDLE. The run guard is invalidated so any in-flight
  // setState callbacks from the previous stream become no-ops.
  const _cleanup = useCallback((): void => {
    abortRef.current?.abort();
    runGuardRef.current.invalidate();
    setState(IDLE);
  }, []);

  const abort = useCallback((): void => {
    _cleanup();
  }, [_cleanup]);
  const reset = useCallback((): void => {
    _cleanup();
  }, [_cleanup]);

  return { state, stream, finalize, abort, reset };
}
