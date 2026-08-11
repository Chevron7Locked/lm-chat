/* SPDX-License-Identifier: Apache-2.0 */
/**
 * resolveStoppedPartial — flush-lag comparison helper. The `durable`
 * signal supports the backoff-poll fix for a stopped stream.
 *
 * When the user hits Stop, the SSE hook records the in-memory partial and the
 * assistant message id. There is no open SSE channel after stop (the
 * connection was aborted), so the caller cannot wait for a BE "flush-complete"
 * frame — instead it polls: it refetches the persisted row on a backoff
 * schedule and calls this helper each time to decide (a) whether the cache
 * needs patching with the longer in-memory content, and (b) whether the
 * server row is now DURABLE (caught up), so the caller can stop polling.
 *
 * Rules:
 *  - `targetId === null` → nothing to reconcile → `{ patch: null, durable: true }`.
 *  - Server row not found (not persisted yet) → keep polling →
 *    `{ patch: null, durable: false }`.
 *  - Server row content shorter than the in-memory partial (flush-lag) → a
 *    patch is available if the caller gives up, but the row isn't durable yet
 *    → `{ patch: inMemoryContent, durable: false }`.
 *  - Server row content has caught up (length >= in-memory length) → stop
 *    polling, no patch needed → `{ patch: null, durable: true }`.
 *
 * Exported as a pure function so unit tests can import the production
 * implementation directly rather than re-implementing the comparison logic.
 */

export interface MessageLike {
  id: number;
  content: string;
}

export interface ResolveStoppedPartialResult {
  /** Non-null when the cache should be patched with this content. */
  patch: string | null;
  /** True when the server row is caught up (or there's nothing to reconcile) — the caller should stop polling. */
  durable: boolean;
}

/**
 * Determine whether the query cache needs to be patched after a stop-refetch,
 * and whether the server row is durable enough to stop polling.
 *
 * @param freshMessages  The messages array returned by the refetch.
 * @param targetId       The message id of the stopped assistant row, or null.
 * @param inMemoryContent The joined contentDeltas snapshot taken at stop time.
 * @returns `{ patch, durable }` — `patch` is non-null when the cache needs
 *   patching with `inMemoryContent`; `durable` is true when the caller should
 *   stop polling (nothing to reconcile, or the server row has caught up).
 */
export function resolveStoppedPartial(
  freshMessages: MessageLike[],
  targetId: number | null,
  inMemoryContent: string,
): ResolveStoppedPartialResult {
  if (targetId === null) {
    return { patch: null, durable: true };
  }
  const serverRow = freshMessages.find((m) => m.id === targetId);
  if (serverRow === undefined) {
    // Row not yet persisted — no patch (caller falls back to in-memory
    // display), keep polling.
    return { patch: null, durable: false };
  }
  const serverContent = serverRow.content;
  if (serverContent.length < inMemoryContent.length) {
    // Flush-lag: server row is shorter than what we delivered in-memory.
    // A patch is available if the caller gives up, but keep polling for now.
    return { patch: inMemoryContent, durable: false };
  }
  // Caught up — server row is durable, stop polling.
  return { patch: null, durable: true };
}
