/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useFocusMode — session-only "focus mode" toggle for the chat surface.
 *
 * Focus mode hides the sidebar + top chrome and floats the composer so the
 * conversation is the sole focus. State is intentionally session-only (plain
 * React state, no localStorage): a reload always returns to the normal layout.
 * The exit affordance is always visible in focus mode, so nothing is stranded,
 * but a focus mode that survived reloads would be a surprising trap.
 *
 * `motionEnabled` is the inverse of `prefers-reduced-motion`. Chat uses it to
 * add the `lmchat-focus-animated` class ONLY when motion is allowed, so
 * reduced-motion users get an instant (transition-free) enter/exit. The CSS
 * also carries a `@media (prefers-reduced-motion: reduce)` guard, so motion is
 * suppressed at both the JS and CSS layers.
 */
import { useCallback, useEffect, useRef, useState } from "react";

export interface UseFocusModeResult {
  /** Whether focus mode is currently active. */
  focusMode: boolean;
  /** Set focus mode directly (supports an updater callback). */
  setFocusMode: (next: boolean | ((prev: boolean) => boolean)) => void;
  /** Flip focus mode on/off. */
  toggleFocusMode: () => void;
  /** True when motion is allowed (i.e. NOT prefers-reduced-motion). */
  motionEnabled: boolean;
  /**
   * True for a brief beat right after ENTERING focus mode, cleared on the first
   * pointer move (or a short timeout). While set, the top-chrome hover-reveal is
   * suppressed so the chrome fully recedes on enter even when the cursor is
   * still resting on the (in-topbar) toggle — otherwise `.lmchat-topbar-shell:hover`
   * would hold the bar open and the enter transition would only half-play.
   */
  revealLocked: boolean;
}

const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function prefersReducedMotion(): boolean {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return false;
  }
  return window.matchMedia(REDUCED_MOTION_QUERY).matches;
}

export function useFocusMode(): UseFocusModeResult {
  const [focusMode, setFocusMode] = useState(false);
  const [revealLocked, setRevealLocked] = useState(false);
  const [motionEnabled, setMotionEnabled] = useState(
    () => !prefersReducedMotion(),
  );

  // Mirror of focusMode readable synchronously inside toggleFocusMode, so the
  // enter path can set the lock in the SAME render batch as focusMode (a
  // separate effect-driven set would leave a one-frame gap where the hover
  // latch could grab the bar mid-transition).
  const focusModeRef = useRef(focusMode);
  focusModeRef.current = focusMode;

  // On ENTER, briefly lock the top-chrome hover-reveal so the bar recedes fully
  // even while the cursor still rests on the toggle. The lock releases on the
  // first pointer move (the natural "I'm reading now" signal) or after a short
  // fallback timeout — whichever comes first. (The lock is SET synchronously in
  // toggleFocusMode; this effect owns the release listeners + the exit reset.)
  useEffect(() => {
    if (!focusMode) {
      setRevealLocked(false);
      return;
    }
    if (typeof window === "undefined") return;
    const release = (): void => {
      setRevealLocked(false);
    };
    window.addEventListener("pointermove", release, { once: true });
    const t = window.setTimeout(release, 700);
    return () => {
      window.removeEventListener("pointermove", release);
      window.clearTimeout(t);
    };
  }, [focusMode]);

  // Track live changes to the OS reduced-motion preference so the animated
  // class flips without a reload (mirrors themeStore / usePresence).
  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return;
    }
    const mq = window.matchMedia(REDUCED_MOTION_QUERY);
    const onChange = (): void => {
      setMotionEnabled(!mq.matches);
    };
    onChange();
    mq.addEventListener("change", onChange);
    return () => {
      mq.removeEventListener("change", onChange);
    };
  }, []);

  const toggleFocusMode = useCallback(() => {
    const entering = !focusModeRef.current;
    // Same-batch as the focusMode flip so there is no intermediate frame where
    // the bar is in focus mode but the reveal is not yet locked.
    if (entering) setRevealLocked(true);
    setFocusMode(entering);
  }, []);

  return {
    focusMode,
    setFocusMode,
    toggleFocusMode,
    motionEnabled,
    revealLocked,
  };
}
