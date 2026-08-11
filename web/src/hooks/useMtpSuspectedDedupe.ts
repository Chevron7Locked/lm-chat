/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useMtpSuspectedDedupe — dedupes the "MTP-suspected" stream-error banner so
 * it surfaces at most once per chat per tab session.
 *
 * Extracted from pages/Chat.tsx (FE decomposition cut #5) — a self-contained,
 * side-effect-only cluster with zero cross-references elsewhere in the
 * component.
 *
 * Adaptation note: the ref's declaration (previously standalone, near the
 * top of Chat.tsx) is combined here with the record effect into a single
 * hook, so its call site necessarily follows `sseState`'s definition (the
 * pre-extraction ref sat much earlier in Chat.tsx, well before `sseState`
 * existed). This is a pure lexical-position shift with no behavior change —
 * the ref is still created exactly once per component instance and the
 * render-site read (still in Chat.tsx, further down the JSX) already ran
 * after both original call sites either way.
 */
import { useEffect, useRef } from "react";
import type { RefObject } from "react";
import type { StreamState } from "@/hooks/useSSE";

/**
 * Tracks which chatIds have already surfaced the MTP-suspected banner this
 * tab session and records new occurrences as they arrive on `sseState`.
 *
 * Returns the backing ref so the render site can keep its original direct
 * `.current.has(chatId)` read (byte-closest to the pre-extraction call site).
 */
export function useMtpSuspectedDedupe(
  sseState: StreamState,
  chatId: number | null,
): RefObject<Set<number>> {
  // MTP-suspected dedupe: Set<chatId> of chats that have already surfaced
  // the MTP hint this tab session. Ephemeral by design (resets on tab reload).
  //
  // Implementation notes (load-bearing for correctness):
  //   - useRef rather than useState: mutating useState here would trigger a
  //     re-render that re-evaluates the dedupe check and suppresses the
  //     banner mid-flight.
  //   - The Set is mutated ONLY inside the useEffect below (NOT during
  //     render). Mutating refs during render breaks under React StrictMode's
  //     double-render: the first pass writes the set and returns the banner,
  //     the second pass reads the now-populated set and returns null, and
  //     React commits the second pass's output (no banner in dev). The
  //     effect runs once per render commit; Set.add is idempotent on
  //     duplicates so StrictMode's double-effect fire is safe.
  //   - The render's dedupe check is a pure read of the ref. Both StrictMode
  //     render passes see the same ref state and produce the same output.
  const mtpSuspectedShownRef = useRef<Set<number>>(new Set());

  // MTP-suspected dedupe: record chat in the set when an mtp_suspected error
  // is observed. The ref mutation happens here (NOT during render) so the
  // conditional render's check remains a pure read; see the comment on the
  // ref declaration for the StrictMode rationale. Set.add is idempotent so
  // the effect double-firing under StrictMode is safe.
  useEffect(() => {
    if (
      sseState.status === "error" &&
      sseState.error !== null &&
      sseState.error.code === "mtp_suspected" &&
      chatId !== null
    ) {
      mtpSuspectedShownRef.current.add(chatId);
    }
  }, [sseState.status, sseState.error?.code, chatId]);

  return mtpSuspectedShownRef;
}
