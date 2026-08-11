/* SPDX-License-Identifier: Apache-2.0 */
/**
 * formatShortcut — render a chord descriptor in the platform's idiom.
 *
 * On Mac, modifiers collapse into symbols: "⌘⇧E".
 * On other platforms, modifiers join with "+": "Ctrl+Shift+E".
 *
 * Designed to be called inline:
 *
 *   const platform = usePlatform();
 *   formatShortcut(platform, { mod: true, key: "Enter" });
 *   → "⌘Enter"  on Mac
 *   → "Ctrl+Enter" elsewhere
 */
import type { PlatformInfo } from "@/hooks/usePlatform";

export interface Chord {
  /** Cmd on Mac, Ctrl elsewhere. */
  mod?: boolean;
  shift?: boolean;
  alt?: boolean;
  /** Visible key label (e.g. "Enter", "K", ",", "/", "Shift"). */
  key: string;
}

export function formatShortcut(platform: PlatformInfo, chord: Chord): string {
  const parts: string[] = [];
  if (chord.mod === true) parts.push(platform.modLabel);
  if (chord.shift === true) parts.push(platform.shiftLabel);
  if (chord.alt === true) parts.push(platform.altLabel);
  parts.push(chord.key);
  return platform.isMac ? parts.join("") : parts.join("+");
}
