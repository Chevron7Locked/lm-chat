/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useKeyboardInset — visualViewport-based soft-keyboard offset tracker.
 *
 * Computes the pixel height of the on-screen soft keyboard by diffing
 * window.innerHeight against visualViewport.height + visualViewport.offsetTop.
 * Sets --keyboard-inset-fallback on <html> so composer.css can use it as a
 * padding-bottom fallback on browsers that don't support env(keyboard-inset-height).
 *
 * Layered approach:
 *   1. env(keyboard-inset-height) — Safari 17.4+ native (always wins if supported)
 *   2. --keyboard-inset-fallback — this hook (cross-browser JS fallback)
 *
 * No transitions: instant settle, no jank.
 * Side-effect only hook — returns null.
 */
import { useEffect } from "react";

export function useKeyboardInset(): null {
  useEffect(() => {
    const vv = window.visualViewport;
    if (vv == null) return;

    const update = (): void => {
      const keyboard = window.innerHeight - (vv.height + vv.offsetTop);
      const inset = Math.max(0, Math.round(keyboard));
      document.documentElement.style.setProperty(
        "--keyboard-inset-fallback",
        `${String(inset)}px`,
      );
    };

    vv.addEventListener("resize", update, { passive: true });
    vv.addEventListener("scroll", update, { passive: true });
    update();

    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      document.documentElement.style.removeProperty(
        "--keyboard-inset-fallback",
      );
    };
  }, []);

  return null;
}
