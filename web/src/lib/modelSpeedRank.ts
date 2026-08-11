/* SPDX-License-Identifier: Apache-2.0 */
/**
 * modelSpeedRank — cheap "how fast is this model" heuristic from its id/name.
 *
 * Used to pick a sensible DEFAULT background-tasks model at first setup. The
 * background model runs the best-effort aux calls (auto-memory distillation,
 * chat titles, follow-up chips); on a slow model those calls take tens of
 * seconds and used to time out entirely. The old default just grabbed the
 * FIRST loaded general model in probe order, which on a multi-model rig was
 * often the largest (e.g. a 122B-MoE) — the worst pick for background work.
 *
 * There is no size field on the setup probe's model objects, so infer relative
 * speed from the parameter tokens embedded in the id/name. For a
 * mixture-of-experts model the ACTIVE parameter count ("a3b", "a10b") drives
 * throughput far more than the total ("35b", "122b"), so prefer that; fall back
 * to the total when no active token is present (dense models), and to Infinity
 * when neither is found so a size-unknown model is only chosen as a last resort.
 *
 * Lower is faster. This is a heuristic, not a benchmark — it only has to order
 * "obviously small" ahead of "obviously huge", which is enough to stop defaulting
 * onto the biggest model on the box.
 *
 * @example
 *   modelSpeedRank("qwen3.6-35b-a3b-mtp")   // 3   (active 3B)
 *   modelSpeedRank("qwen3.5-122b-a10b-mtp") // 10  (active 10B)
 *   modelSpeedRank("qwen3.5-9b")            // 9   (dense, total 9B)
 *   modelSpeedRank("some-unlabelled-model") // Infinity
 */
export function modelSpeedRank(idOrName: string): number {
  const s = idOrName.toLowerCase();
  // MoE active params first: "a3b", "a10b", "a1.5b".
  const active = /a(\d+(?:\.\d+)?)b(?![a-z0-9])/.exec(s)?.[1];
  if (active !== undefined) return Number.parseFloat(active);
  // Dense total params: "35b", "9b", "1.5b".
  const total = /(\d+(?:\.\d+)?)b(?![a-z0-9])/.exec(s)?.[1];
  if (total !== undefined) return Number.parseFloat(total);
  return Number.POSITIVE_INFINITY;
}

/**
 * Pick the fastest (smallest active-param) entry from a list, by a key that
 * exposes an id/name to rank. Ties keep the first occurrence (stable). Returns
 * undefined for an empty list.
 */
export function pickFastestModel<T>(
  items: readonly T[],
  labelOf: (item: T) => string,
): T | undefined {
  let best: T | undefined;
  let bestRank = Number.POSITIVE_INFINITY;
  for (const item of items) {
    const rank = modelSpeedRank(labelOf(item));
    if (best === undefined || rank < bestRank) {
      best = item;
      bestRank = rank;
    }
  }
  return best;
}

/**
 * Pick the slowest (largest active-param) entry — the mirror of
 * {@link pickFastestModel}. Used by the live-dogfood harness to deliberately
 * run background aux calls (distillation / titles / follow-ups) against the
 * WORST realistic model, since that pathological-but-real condition is where
 * the silent-timeout class of bug lives. Ties keep the first occurrence.
 * A size-unknown model (rank Infinity) sorts as "slowest"; that is acceptable
 * for the harness (an unlabelled model is treated as a worst-case), and callers
 * that need a genuinely-slow model should confirm the fleet actually spans a
 * range. Returns undefined for an empty list.
 */
export function pickSlowestModel<T>(
  items: readonly T[],
  labelOf: (item: T) => string,
): T | undefined {
  let worst: T | undefined;
  let worstRank = Number.NEGATIVE_INFINITY;
  for (const item of items) {
    const rank = modelSpeedRank(labelOf(item));
    if (worst === undefined || rank > worstRank) {
      worst = item;
      worstRank = rank;
    }
  }
  return worst;
}
