/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Follow-up suggestion chips — Feature 5.
 *
 * Ported from v0.5.x app.js:4019–4044.
 *
 * Sourcing: MODEL-GENERATED. The assistant is instructed (via a directive the
 * backend appends to the system prompt, gated by LM_CHAT_FOLLOWUPS_ENABLED) to
 * emit a hidden HTML comment on its last line:
 *   <!--followups:["q1","q2","q3"]-->
 * This module strips that marker before display and surfaces the questions as
 * clickable chips. The injection lives server-side so it never leaks into the
 * client payload.
 */

export interface ExtractFollowupsResult {
  /** The assistant text with the followups comment stripped. */
  text: string;
  /** Up to 3 follow-up question strings, or empty array if none. */
  followups: string[];
}

/**
 * Parse and strip the hidden followups comment from assistant content.
 * Ported from v0.5.x app.js:4022–4043 (extractFollowups).
 */
export function extractFollowups(text: string): ExtractFollowupsResult {
  const match = /<!--followups:?\s*(\[.*?\])\s*-->\s*$/s.exec(text);
  if (match?.[1] === undefined) return { text, followups: [] };
  try {
    const parsed: unknown = JSON.parse(match[1]);
    if (
      Array.isArray(parsed) &&
      parsed.length > 0 &&
      parsed.every((q): q is string => typeof q === "string")
    ) {
      return {
        text: text.slice(0, match.index).trimEnd(),
        followups: parsed.slice(0, 3),
      };
    }
  } catch {
    // Malformed JSON — fall through.
  }
  return { text, followups: [] };
}
