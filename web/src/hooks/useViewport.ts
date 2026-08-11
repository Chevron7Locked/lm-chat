/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useViewport — react-friendly viewport size tracker.
 *
 * Exposes a single `isMobile` boolean driven by a `matchMedia` query.
 * Falls back to `false` on the server (SSR-safe) so the desktop layout
 * is the default if JS hasn't run yet.
 */
import { useEffect, useState } from "react";

// The previous breakpoint matched 768px viewports as "mobile", which
// hid the sidebar on iPad portrait (768×1024) — the 2026-06-03 UI
// audit flagged this for every library route (/settings, /memory,
// /documents, /analytics, /prompts). Tightening to `767px` keeps
// real phones (≤414 typical) on the mobile shell while preserving
// the sidebar on actual tablets.
const MOBILE_BREAKPOINT = "(max-width: 767px)";

export interface Viewport {
  isMobile: boolean;
}

export function useViewport(): Viewport {
  const [isMobile, setIsMobile] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(MOBILE_BREAKPOINT).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia(MOBILE_BREAKPOINT);
    const handler = (e: MediaQueryListEvent): void => {
      setIsMobile(e.matches);
    };
    mq.addEventListener("change", handler);
    return () => {
      mq.removeEventListener("change", handler);
    };
  }, []);

  return { isMobile };
}
