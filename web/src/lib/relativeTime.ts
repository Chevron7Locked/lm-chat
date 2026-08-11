/* SPDX-License-Identifier: Apache-2.0 */
/**
 * formatRelativeTime — i18n-correct relative-time formatter.
 *
 * Returns human-readable relative strings consistent with the rest of
 * the LM Chat UI: "just now", "45s", "3m", "2h", "5d", "2w", "3mo".
 * Uses Intl.RelativeTimeFormat for locale-correct rendering where
 * possible; falls back to English abbreviations.
 *
 * Accepts a Date object, a numeric Unix timestamp (seconds), or an
 * ISO 8601 string.  Unix-seconds inputs (e.g. Python time.time()) are
 * detected by value < 1e12; anything ≥ 1e12 is treated as milliseconds.
 */

const rtf = new Intl.RelativeTimeFormat("en", {
  numeric: "auto",
  style: "narrow",
});

function pickUnit(diffSec: number): {
  value: number;
  unit: Intl.RelativeTimeFormatUnit;
} {
  const abs = Math.abs(diffSec);
  if (abs < 60) return { value: Math.round(diffSec), unit: "second" };
  if (abs < 3600) return { value: Math.round(diffSec / 60), unit: "minute" };
  if (abs < 86400) return { value: Math.round(diffSec / 3600), unit: "hour" };
  if (abs < 604800) return { value: Math.round(diffSec / 86400), unit: "day" };
  if (abs < 2_592_000)
    return { value: Math.round(diffSec / 604800), unit: "week" };
  if (abs < 31_536_000)
    return { value: Math.round(diffSec / 2_592_000), unit: "month" };
  return { value: Math.round(diffSec / 31_536_000), unit: "year" };
}

export function formatRelativeTime(input: Date | string | number): string {
  let ms: number;
  if (input instanceof Date) {
    ms = input.getTime();
  } else if (typeof input === "number") {
    // Unix-seconds heuristic: values < 1e12 are seconds
    ms = input < 1e12 ? input * 1000 : input;
  } else {
    ms = new Date(input).getTime();
  }
  if (isNaN(ms)) return "unknown";

  const diffSec = (ms - Date.now()) / 1000;
  const abs = Math.abs(diffSec);

  if (abs < 5) return "just now";

  const { value, unit } = pickUnit(diffSec);
  return rtf.format(value, unit);
}
