/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useABStream — A/B model compare streaming hook.
 *
 * Connects to POST /api/ab/stream (form-encoded) which returns SSE with
 * events from two models simultaneously.  Each SSE event carries a
 * `pane: "a" | "b"` discriminator.
 *
 * State machine:
 *   idle ──[start()]──► streaming
 *   streaming ──[ab.end (both panes)]──► complete
 *   streaming ──[ab.error]──► pane marked errored, other continues
 *   streaming ──[stop()]──► idle
 *   complete ──[start()]──► streaming (new compare)
 *   error ──[start()]──► streaming (retry)
 *
 * Per the brief: an error in one pane MUST NOT break the other.
 * Each pane has its own status field in ABStreamState.
 *
 * Gated on !isInitializing && user !== null.
 * Form-encoded payload.
 */
import { useCallback, useRef, useState } from "react";
import { useAuthStore } from "@/stores/authStore";
import { createRunGuard, readSseStream } from "@/lib/sseStream";

// ─── Types ──────────────────────────────────────────────────────────────────

type ABPaneStatus = "idle" | "streaming" | "complete" | "error";

interface ABPaneState {
  status: ABPaneStatus;
  contentDeltas: string[];
  reasoningDeltas: string[];
  responseId: string | null;
  error: { code: string; message: string } | null;
}

export interface ABStreamState {
  /** Overall session status. "complete" when BOTH panes are done. */
  status: "idle" | "streaming" | "complete" | "error";
  paneA: ABPaneState;
  paneB: ABPaneState;
}

interface StartABPayload {
  chatId: number;
  message: string;
  modelA: string;
  modelB: string;
}

interface UseABStreamReturn {
  state: ABStreamState;
  start: (payload: StartABPayload) => Promise<void>;
  stop: () => void;
}

// ─── Initial state ───────────────────────────────────────────────────────────

const INITIAL_PANE: ABPaneState = {
  status: "idle",
  contentDeltas: [],
  reasoningDeltas: [],
  responseId: null,
  error: null,
};

const INITIAL_STATE: ABStreamState = {
  status: "idle",
  paneA: INITIAL_PANE,
  paneB: INITIAL_PANE,
};

// ─── Hook ────────────────────────────────────────────────────────────────────

/**
 * useABStream — two-pane concurrent streaming state machine.
 *
 * Error in one pane does not break the other: each pane has independent
 * status tracking.  The session status is "complete" only when BOTH panes
 * reach a terminal state (ab.end or ab.error).
 *
 * Gated on !isInitializing && user !== null to prevent 401 spam.
 */
export function useABStream(): UseABStreamReturn {
  const [state, setState] = useState<ABStreamState>(INITIAL_STATE);
  const abortRef = useRef<AbortController | null>(null);
  const { isInitializing, user } = useAuthStore();

  // fe-components-state-9 — run-generation guard: discards setState calls
  // from a stream superseded by a newer start() or an explicit stop(). Only
  // useSubSessionSSE had this before extraction; generalized to all three
  // SSE hooks (see lib/sseStream.ts).
  const runGuardRef = useRef(createRunGuard());

  const stop = useCallback((): void => {
    runGuardRef.current.invalidate();
    abortRef.current?.abort();
    abortRef.current = null;
    setState((s) => ({ ...s, status: "idle" }));
  }, []);

  const start = useCallback(
    async ({
      chatId,
      message,
      modelA,
      modelB,
    }: StartABPayload): Promise<void> => {
      if (isInitializing || user === null) return;

      // Abort any in-flight stream.
      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;

      // New generation — any async continuation from a PRIOR start() becomes
      // a no-op from here on. The synchronous state reset directly below
      // stays UNGUARDED: it always applies, nothing could have superseded it.
      const myGen = runGuardRef.current.start();
      const guardedSetState = (
        updater: (prev: ABStreamState) => ABStreamState,
      ): void => {
        if (!runGuardRef.current.isCurrent(myGen)) return;
        setState(updater);
      };

      setState({
        status: "streaming",
        paneA: { ...INITIAL_PANE, status: "streaming" },
        paneB: { ...INITIAL_PANE, status: "streaming" },
      });

      // Track terminal state locally to detect session completion.
      let paneADone = false;
      let paneBDone = false;

      const params = new URLSearchParams({
        chat_id: String(chatId),
        message,
        model_a: modelA,
        model_b: modelB,
      });

      try {
        const response = await fetch("/api/ab/stream", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: params.toString(),
          credentials: "same-origin",
          signal: ac.signal,
        });

        if (!response.ok) {
          const body = (await response.json().catch(() => ({}))) as {
            detail?: unknown;
          };
          const detail =
            typeof body.detail === "string" ? body.detail : response.statusText;
          guardedSetState((s) => ({
            ...s,
            status: "error",
            paneA: {
              ...s.paneA,
              status: "error",
              error: {
                code: `http_${String(response.status)}`,
                message: detail,
              },
            },
            paneB: {
              ...s.paneB,
              status: "error",
              error: {
                code: `http_${String(response.status)}`,
                message: detail,
              },
            },
          }));
          return;
        }

        if (response.body === null) {
          guardedSetState((s) => ({
            ...s,
            status: "error",
            paneA: {
              ...s.paneA,
              status: "error",
              error: { code: "no_body", message: "Response body was null" },
            },
            paneB: {
              ...s.paneB,
              status: "error",
              error: { code: "no_body", message: "Response body was null" },
            },
          }));
          return;
        }

        const { exhausted } = await readSseStream(response.body, (frame) => {
          let raw: {
            type: string;
            pane?: string;
            delta?: string;
            reasoning?: string;
            response_id?: string;
            code?: string;
            message?: string;
          };
          try {
            raw = JSON.parse(frame.data ?? "") as typeof raw;
          } catch {
            return "continue";
          }

          const pane = raw.pane as "a" | "b" | undefined;
          const paneKey =
            pane === "a" ? "paneA" : pane === "b" ? "paneB" : null;
          if (paneKey === null) return "continue";

          // Detect terminal events and update done flags BEFORE setState
          // so ESLint control-flow analysis can verify them below.
          const isTerminal =
            raw.type === "ab.end" ||
            raw.type === "chat.end" ||
            raw.type === "ab.error" ||
            raw.type === "error";
          if (isTerminal) {
            if (pane === "a") paneADone = true;
            if (pane === "b") paneBDone = true;
          }

          guardedSetState((s) => {
            const currentPane = s[paneKey];
            let updatedPane: ABPaneState;

            if (raw.type === "message.delta") {
              updatedPane = {
                ...currentPane,
                contentDeltas: [
                  ...currentPane.contentDeltas,
                  raw.delta ?? "",
                ],
              };
            } else if (raw.type === "reasoning.delta") {
              updatedPane = {
                ...currentPane,
                reasoningDeltas: [
                  ...currentPane.reasoningDeltas,
                  raw.reasoning ?? "",
                ],
              };
            } else if (raw.type === "ab.end" || raw.type === "chat.end") {
              updatedPane = {
                ...currentPane,
                status: "complete",
                responseId: raw.response_id ?? currentPane.responseId,
              };
            } else if (raw.type === "ab.error" || raw.type === "error") {
              updatedPane = {
                ...currentPane,
                status: "error",
                error: {
                  code: raw.code ?? "upstream_error",
                  message: raw.message ?? "stream error",
                },
              };
            } else if (raw.type === "chat.start") {
              updatedPane = {
                ...currentPane,
                responseId: raw.response_id ?? currentPane.responseId,
              };
            } else {
              updatedPane = currentPane;
            }

            const bothDone = paneADone && paneBDone;
            return {
              ...s,
              status: bothDone ? "complete" : s.status,
              [paneKey]: updatedPane,
            };
          });

          return paneADone && paneBDone ? "stop" : "continue";
        });

        if (!exhausted) return;

        // Reader exhausted — mark both panes done if they weren't already.
        guardedSetState((s) => ({
          ...s,
          status: "complete",
          paneA:
            s.paneA.status === "streaming"
              ? { ...s.paneA, status: "complete" }
              : s.paneA,
          paneB:
            s.paneB.status === "streaming"
              ? { ...s.paneB, status: "complete" }
              : s.paneB,
        }));
      } catch (err: unknown) {
        if ((err as Error).name === "AbortError") return;
        const msg =
          err instanceof Error ? err.message : "unknown streaming error";
        guardedSetState((s) => ({
          ...s,
          status: "error",
          paneA: {
            ...s.paneA,
            status: "error",
            error: { code: "fetch_error", message: msg },
          },
          paneB: {
            ...s.paneB,
            status: "error",
            error: { code: "fetch_error", message: msg },
          },
        }));
      }
    },
    [isInitializing, user],
  );

  return { state, start, stop };
}
