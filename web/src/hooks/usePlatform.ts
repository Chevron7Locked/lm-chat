/* SPDX-License-Identifier: Apache-2.0 */
/**
 * usePlatform — detect whether the user is on macOS for shortcut hints.
 *
 * Returns a stable object the lifetime of the component instance:
 *   - isMac      — true when navigator hints suggest a Mac platform
 *   - modKey     — "Meta" on Mac, "Control" on everything else (matches
 *                  KeyboardEvent.key)
 *   - modLabel   — "⌘" on Mac, "Ctrl" elsewhere (for visible labels)
 *   - altLabel   — "⌥" on Mac, "Alt" elsewhere
 *   - shiftLabel — "⇧" on Mac, "Shift" elsewhere
 *
 * SSR-safe: when `navigator` is absent we default to non-Mac so the
 * rendered hint is sensible for the broadest audience.
 */
import { useMemo } from "react";

export interface PlatformInfo {
  isMac: boolean;
  modKey: "Meta" | "Control";
  modLabel: string;
  altLabel: string;
  shiftLabel: string;
}

function detectIsMac(): boolean {
  if (typeof navigator === "undefined") return false;
  // navigator.platform is deprecated but still the most reliable
  // discriminator across browsers as of 2026.  userAgentData is not
  // universally available; fall back to the UA string.
  const platform = navigator.platform;
  if (platform.toLowerCase().includes("mac")) return true;
  const ua = navigator.userAgent.toLowerCase();
  if (ua.includes("mac")) return true;
  // iOS/iPadOS report Macintosh on iPad in desktop mode — treat as Mac.
  if (ua.includes("iphone") || ua.includes("ipad")) return true;
  return false;
}

export function usePlatform(): PlatformInfo {
  return useMemo<PlatformInfo>(() => {
    const isMac = detectIsMac();
    return {
      isMac,
      modKey: isMac ? "Meta" : "Control",
      modLabel: isMac ? "⌘" : "Ctrl",
      altLabel: isMac ? "⌥" : "Alt",
      shiftLabel: isMac ? "⇧" : "Shift",
    };
  }, []);
}
