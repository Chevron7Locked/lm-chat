/* SPDX-License-Identifier: Apache-2.0 */
/**
 * launchableModes — detects references to the 5 sub-session preset slash
 * commands (``/research /code /write /analyze /architect``) inside an
 * assistant message's text, so ``ChatMessage`` can render a "Launch
 * /research" chip row below the bubble.
 *
 * The system prompt's capability legend can prompt the model to name one
 * of these commands — either suggesting one for a fitting task or listing
 * them when asked "what can you do." This module only detects; it never
 * changes model behavior.
 *
 * Scope is intentionally narrower than ``PRESET_BY_SLASH_CMD``: ``/general``
 * is a valid slash command but is not one of the five sub-agent modes
 * advertised by the capability legend, so it's excluded here. ``/compare
 * /compact /prompt /memory`` aren't preset slash commands at all and are
 * excluded for free.
 *
 * Matching is anchored so ``/research`` matches as a standalone token but
 * ``/researcher`` (longer word) and path-like ``/research/data`` do not.
 */
import { PRESETS } from "@/lib/presets";

const LAUNCHABLE_PRESET_IDS = [
  "research",
  "coder",
  "creative",
  "analyst",
  "architect",
] as const;

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Returns the preset ids referenced in `content` as standalone
 * `/slashCmd` tokens, in first-appearance order, with each id appearing
 * at most once. Empty array when none of the 5 commands are referenced.
 *
 * Guards against registry drift: a preset id whose `slashCmd` is missing
 * from `PRESETS` (null or absent) is silently skipped rather than matched.
 */
export function detectLaunchableModes(content: string): string[] {
  const hits: { index: number; presetId: string }[] = [];
  for (const id of LAUNCHABLE_PRESET_IDS) {
    const slashCmd = PRESETS[id]?.slashCmd;
    if (slashCmd === null || slashCmd === undefined) continue;
    const pattern = new RegExp(
      `(?<![\\w/])/${escapeRegExp(slashCmd)}(?![\\w/])`,
    );
    const index = content.search(pattern);
    if (index !== -1) hits.push({ index, presetId: id });
  }
  hits.sort((a, b) => a.index - b.index);
  return hits.map((h) => h.presetId);
}
