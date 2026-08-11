/* SPDX-License-Identifier: Apache-2.0 */
/**
 * dedupeByKey — stable de-duplication for FE-built option/select lists.
 *
 * Model + embedder dropdowns are assembled client-side from one or more
 * upstream sources (a live LM Studio probe, a resolved-config snapshot, a
 * models list, sometimes two lists concatenated) that are not always
 * guaranteed unique on the field used as the React `key` — e.g. a raw
 * probe echoing the same loaded instance twice, or an independently
 * polled status snapshot listing the same model id more than once.
 * Rendering two elements with the same `key` trips React's "Encountered
 * two children with the same key" warning and silently drops one of the
 * pair instead of showing a phantom-duplicate option.
 *
 * Call this at the LIST-CONSTRUCTION site — right before (or while)
 * building the array that gets `.map()`-ed to `<option>`/pill elements —
 * so the rendered key is always unique. Keeps the FIRST occurrence of
 * each key, so callers should order their most informative entries first
 * (e.g. "loaded"/"active" before "unloaded").
 */
export function dedupeByKey<T>(
  items: readonly T[],
  keyOf: (item: T) => string,
): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of items) {
    const key = keyOf(item);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}
