/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSidebarStats — derive the lightweight "tokens today / streaming speed /
 * approx-saved-vs-cloud-equivalent" trio rendered in the sidebar footer.
 *
 * Data sources:
 *
 *   - `useQuotaMe`: backend-tracked `tokens_consumed_today` (authoritative).
 *   - `useMyAnalytics`: backend-tracked `messages_last_7_days` (for a coarse
 *     velocity proxy when no live SSE stream is active).
 *   - sessionStorage cache `lmchat:sidebar-stream-tps` populated by the SSE
 *     pipeline at stream completion: rolling EMA of completion tokens/s.
 *
 * The "approx-saved" line is illustrative: per-token average cost for a
 * comparable hosted LLM (input+output blended ~ $4 / Mtok). It is
 * intentionally rough; the value is to show non-zero usage, not to make
 * a billing claim.
 */
import { useQuotaMe } from "@/hooks/useQuota";

/**
 * GPT-5 Pro pricing (ported from v0.5.x app.js:1595–1598).
 * $30 / 1M input tokens, $180 / 1M output tokens (verified May 2026).
 * The sidebar uses these as the "vs GPT-5" comparison baseline.
 */
const GPT5_PRO_INPUT_USD_PER_MTOK = 30;
const GPT5_PRO_OUTPUT_USD_PER_MTOK = 180;

/**
 * Blended rate: since the backend only exposes total tokens_consumed_today
 * (no input/output split at the quota endpoint level), we approximate using
 * a ~50/50 input/output split as a conservative estimate.
 * Blended = (30 + 180) / 2 = 105 / Mtok.
 */
const GPT5_BLENDED_USD_PER_MTOK =
  (GPT5_PRO_INPUT_USD_PER_MTOK + GPT5_PRO_OUTPUT_USD_PER_MTOK) / 2;

export interface SidebarStats {
  tokensToday: number;
  /** Estimated cost saved (USD) vs GPT-5 Pro if the same tokens went through that API. */
  approxSavedUsd: number;
  /** Last-observed streaming tokens-per-second (rounded), null if none. */
  streamingTps: number | null;
  /** Whether the data has been loaded yet (UI can show skeletons). */
  isReady: boolean;
}

function readStreamingTps(): number | null {
  try {
    const raw = sessionStorage.getItem("lmchat:sidebar-stream-tps");
    if (raw === null) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.round(n) : null;
  } catch {
    return null;
  }
}

export function useSidebarStats(): SidebarStats {
  const { data: quota, isSuccess } = useQuotaMe();
  const tokensToday = quota?.tokens_consumed_today ?? 0;
  const approxSavedUsd = (tokensToday / 1_000_000) * GPT5_BLENDED_USD_PER_MTOK;
  const streamingTps = readStreamingTps();

  return {
    tokensToday,
    approxSavedUsd,
    streamingTps,
    isReady: isSuccess,
  };
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function formatUsd(n: number): string {
  if (n < 0.01) return "<$0.01";
  return `$${n.toFixed(2)}`;
}
