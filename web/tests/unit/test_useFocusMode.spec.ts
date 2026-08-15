/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useFocusMode — session-only focus-mode toggle + reduced-motion signal.
 *
 * Coverage:
 *  - starts OFF; toggle flips on/off; setFocusMode(false) exits.
 *  - motionEnabled = true when motion is allowed.
 *  - motionEnabled = false under prefers-reduced-motion (the instant path —
 *    Chat omits the `.lmchat-focus-animated` class, so no transition runs).
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useFocusMode } from "@/hooks/useFocusMode";

const realMatchMedia = window.matchMedia.bind(window);

/** Stub matchMedia so the reduced-motion query resolves to `reduce`. */
function setReducedMotion(reduce: boolean): void {
  window.matchMedia = (query: string) => ({
    matches: reduce && query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  });
}

afterEach(() => {
  window.matchMedia = realMatchMedia;
  vi.restoreAllMocks();
});

describe("useFocusMode", () => {
  it("starts off and toggles on/off", () => {
    const { result } = renderHook(() => useFocusMode());
    expect(result.current.focusMode).toBe(false);

    act(() => {
      result.current.toggleFocusMode();
    });
    expect(result.current.focusMode).toBe(true);

    act(() => {
      result.current.toggleFocusMode();
    });
    expect(result.current.focusMode).toBe(false);
  });

  it("setFocusMode(false) exits focus mode", () => {
    const { result } = renderHook(() => useFocusMode());
    act(() => {
      result.current.setFocusMode(true);
    });
    expect(result.current.focusMode).toBe(true);

    act(() => {
      result.current.setFocusMode(false);
    });
    expect(result.current.focusMode).toBe(false);
  });

  it("motionEnabled is true when motion is allowed", () => {
    setReducedMotion(false);
    const { result } = renderHook(() => useFocusMode());
    expect(result.current.motionEnabled).toBe(true);
  });

  it("motionEnabled is false under prefers-reduced-motion (instant path)", () => {
    setReducedMotion(true);
    const { result } = renderHook(() => useFocusMode());
    expect(result.current.motionEnabled).toBe(false);
  });

  it("locks the top-chrome reveal on enter, in the same tick as focusMode", () => {
    const { result } = renderHook(() => useFocusMode());
    expect(result.current.revealLocked).toBe(false);

    act(() => {
      result.current.toggleFocusMode();
    });
    // Set synchronously in toggleFocusMode (not a frame later) so the hover
    // latch can't grab the bar mid enter-transition.
    expect(result.current.focusMode).toBe(true);
    expect(result.current.revealLocked).toBe(true);
  });

  it("releases the reveal lock on the first pointer move", () => {
    const { result } = renderHook(() => useFocusMode());
    act(() => {
      result.current.toggleFocusMode();
    });
    expect(result.current.revealLocked).toBe(true);

    act(() => {
      window.dispatchEvent(new Event("pointermove"));
    });
    expect(result.current.revealLocked).toBe(false);
  });

  it("clears the reveal lock on exit", () => {
    const { result } = renderHook(() => useFocusMode());
    act(() => {
      result.current.toggleFocusMode();
    });
    expect(result.current.revealLocked).toBe(true);

    act(() => {
      result.current.toggleFocusMode();
    });
    expect(result.current.focusMode).toBe(false);
    expect(result.current.revealLocked).toBe(false);
  });
});
