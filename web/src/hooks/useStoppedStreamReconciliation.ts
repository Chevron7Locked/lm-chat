/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useStoppedStreamReconciliation — stop-button refetch + flush-lag
 * comparison, plus the chat-switch zombie-state reset.
 *
 * Extracted from pages/Chat.tsx — a self-contained, side-effect-only
 * cluster with zero cross-references elsewhere in the component.
 */
import { useEffect, useRef } from "react";
import type { QueryClient, UseQueryResult } from "@tanstack/react-query";
import type { StreamState } from "@/hooks/useSSE";
import type { MessageListResponse } from "@/hooks/useChats";
import { chatKeys } from "@/hooks/useChats";
import type { ApiError } from "@/lib/api";
import { resolveStoppedPartial } from "@/lib/stoppedPartial";

// Backoff schedule (ms) for the post-stop reconciliation poll — there is no
// open SSE channel after stop (the connection was aborted), so a BE
// "flush-complete" frame is not an option; we poll the persisted row instead.
// ~3.65s total across 5 attempts — comfortably past the BE's 500ms
// disconnect-poll + flush, capped so a stuck row doesn't poll forever.
const POLL_DELAYS = [200, 350, 600, 1000, 1500];

function pollDelayFor(attemptIndex: number): number {
  return POLL_DELAYS[attemptIndex] ?? POLL_DELAYS[POLL_DELAYS.length - 1] ?? 1500;
}

export interface UseStoppedStreamReconciliationArgs {
  chatId: number | null;
  sseState: StreamState;
  refetchMessages: UseQueryResult<MessageListResponse, ApiError>["refetch"];
  resetStream: () => void;
  qc: QueryClient;
}

/**
 * Runs the stopped-stream reconciliation side-effects. No return value —
 * pure side-effect hook.
 */
export function useStoppedStreamReconciliation({
  chatId,
  sseState,
  refetchMessages,
  resetStream,
  qc,
}: UseStoppedStreamReconciliationArgs): void {
  // Stop-button refetch + flush-lag comparison (backoff-poll).
  //
  // When the stream transitions to "stopped" we hold the in-memory partial on
  // screen (streamActive includes "stopped"). There is no open SSE channel
  // after stop (useSSE's stop() aborts the connection), so we can't wait for
  // a BE "flush-complete" frame — instead we poll the persisted row on a
  // backoff schedule (POLL_DELAYS) until it's durable (caught up to the
  // in-memory partial) or we exhaust the attempts, then resolve once.
  //
  // Implementation notes:
  //  - We read the refetch *result* directly (res.data) rather than the
  //    stale `messagesData` closure, which still holds pre-refetch cache.
  //  - We match the server row by `sseState.messageId` (captured at stop
  //    time), not `.at(-1)`, which would hit the previous turn's reply.
  //  - When the server row is shorter (flush-lag) and we give up polling, we
  //    patch the query-cache row with our in-memory partial so the display
  //    shows the full content even after resetStream() clears contentDeltas.
  //  - We always call resetStream() exactly once at the end so the Composer
  //    unlocks and the zombie "stopped" state cannot persist across chat
  //    switches.
  const stoppedPartialRef = useRef<string>("");
  const stoppedMsgIdRef = useRef<number | null>(null);
  // Always-current mirrors of the streaming content/id, updated every
  // render (not gated by an effect). The reconciliation effect below only
  // needs to see the LATEST contentDeltas/messageId at the moment it
  // actually fires (when status flips to "stopped") — it does not need to
  // re-fire on every delta chunk during an active stream, which is what
  // listing `sseState.contentDeltas` directly in the dep array would cause
  // (a fresh snapshot + timer reset on every token). Reading through refs
  // makes the omission exhaustive-deps-clean without changing when this
  // effect runs.
  const contentDeltasRef = useRef(sseState.contentDeltas);
  contentDeltasRef.current = sseState.contentDeltas;
  const messageIdRef = useRef(sseState.messageId);
  messageIdRef.current = sseState.messageId;
  useEffect(() => {
    if (sseState.status !== "stopped" || chatId === null) return;
    // Snapshot the partial content and message id now, before the refetch.
    stoppedPartialRef.current = contentDeltasRef.current.join("");
    stoppedMsgIdRef.current = messageIdRef.current;

    // Cancellation flag + timer handle for this effect run. A plain closure
    // variable (not a ref) is correct here: both the recursive poll and the
    // cleanup below live entirely within this single effect invocation, so
    // there's no cross-render state to preserve.
    let cancelled = false;
    let timerId: number | null = null;

    const finalize = (patch: string | null): void => {
      if (cancelled) return;
      if (patch !== null) {
        // Flush-lag give-up: server row never caught up within the backoff
        // budget. Patch the query cache so the display shows the in-memory
        // partial after resetStream() clears contentDeltas.
        const targetId = stoppedMsgIdRef.current;
        qc.setQueryData<MessageListResponse>(
          chatKeys.messages(chatId),
          (old) => {
            if (old === undefined) return old;
            return {
              ...old,
              messages: old.messages.map((m) =>
                m.id === targetId ? { ...m, content: patch } : m
              ),
            };
          }
        );
      }
      // Always reset so the Composer unlocks and the bubble clears — exactly
      // once, whether we stopped because the row is durable or because we
      // exhausted the backoff budget.
      resetStream();
    };

    const pollAttempt = (attemptIndex: number): void => {
      timerId = window.setTimeout(() => {
        if (cancelled) return;
        void refetchMessages().then((res) => {
          if (cancelled) return;
          const freshMessages = res.data?.messages ?? [];
          const targetId = stoppedMsgIdRef.current;
          const inMemoryContent = stoppedPartialRef.current;

          const { patch, durable } = resolveStoppedPartial(freshMessages, targetId, inMemoryContent);
          const attemptsExhausted = attemptIndex >= POLL_DELAYS.length - 1;
          if (durable || attemptsExhausted) {
            finalize(patch);
            return;
          }
          pollAttempt(attemptIndex + 1);
        });
      }, pollDelayFor(attemptIndex));
    };

    pollAttempt(0);

    return () => {
      cancelled = true;
      if (timerId !== null) window.clearTimeout(timerId);
    };
  }, [sseState.status, chatId, qc, refetchMessages, resetStream]);

  // Switching chats while status==='stopped' resets any lingering zombie
  // state. `sseState.status` and `resetStream` are listed for
  // exhaustiveness; the `prevChatIdForStopRef` check already gates the
  // actual reset to real chatId changes, so their presence here doesn't
  // change how often resetStream() gets called — only chatId changing does.
  const prevChatIdForStopRef = useRef<number | null>(null);
  useEffect(() => {
    if (prevChatIdForStopRef.current !== chatId) {
      prevChatIdForStopRef.current = chatId;
      if (sseState.status === "stopped") {
        resetStream();
      }
    }
  }, [chatId, resetStream, sseState.status]);
}
