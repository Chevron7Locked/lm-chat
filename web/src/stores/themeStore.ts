/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Theme store — Zustand 5.
 *
 * Manages "dark" | "light" | "system" preference, persists to localStorage,
 * and keeps document.documentElement in sync.
 *
 * System mode listens to the prefers-color-scheme media query so the page
 * follows the OS without a page reload. The light-mode CSS class name is
 * "light" (matching globals.css `:root.light`). Dark mode uses the default
 * token values (no class) so class="light" is the only toggle needed.
 */
import { create } from "zustand";

export type Theme = "dark" | "light" | "system";

interface ThemeState {
  /** The currently stored preference. */
  theme: Theme;
  /** The effective theme after resolving "system". */
  effective: "dark" | "light";
  /**
   * Set the active theme.  When `origin` is provided AND the browser
   * supports the View Transitions API AND the user hasn't requested
   * reduced motion, the theme swap is wrapped in a circular wipe
   * radiating from that screen-space point — typically the click
   * coordinate of the toggle button.  Otherwise the swap is instant.
   */
  setTheme: (t: Theme, origin?: { x: number; y: number }) => void;
}

// Loose view-transition surface — lib.dom's ViewTransition shape varies
// across TS versions; we only call the function form and don't depend on
// the returned object's fields.  Casting through this avoids fighting
// the standard Document.startViewTransition signature.
type StartViewTransition = ((cb: () => void) => unknown) | undefined;

const LS_KEY = "lmchat:theme";

/** Read user-preferred theme from localStorage; fall back to "dark".
 *
 *  Dark is the default for new users instead of "system" — the
 *  warm-future-library aesthetic is anchored on the dark
 *  parchment+copper palette and that's the experience LM Chat is designed
 *  around. Users who explicitly want light (or system-follow) set it in
 *  Settings → Appearance; their choice persists. */
function readStored(): Theme {
  try {
    const v = localStorage.getItem(LS_KEY);
    if (v === "dark" || v === "light" || v === "system") return v;
  } catch {
    // localStorage may be unavailable in sandboxed iframes — proceed with default.
  }
  return "dark";
}

/** Derive effective theme from stored preference + media query. */
function resolveEffective(t: Theme): "dark" | "light" {
  if (t === "dark") return "dark";
  if (t === "light") return "light";
  // "system" — follow OS preference.
  return window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
}

/** Apply the effective theme to the DOM and persist the preference. */
function applyTheme(t: Theme): void {
  const eff = resolveEffective(t);
  const root = document.documentElement;
  if (eff === "light") {
    root.classList.add("light");
    root.classList.remove("dark");
  } else {
    // Explicitly ADD `.dark` (was previously: remove both classes,
    // leaving the root unclassed).  globals.css uses
    // `@media (prefers-color-scheme: light) { :root:not(.dark) { ... } }`
    // to opt INTO light styles when neither class is set — so an
    // unclassed root on a macOS-light host actually rendered LIGHT
    // even when the user explicitly chose dark.  Adding `.dark`
    // disables the media-query opt-in.
    root.classList.add("dark");
    root.classList.remove("light");
  }
  try {
    localStorage.setItem(LS_KEY, t);
  } catch {
    // Ignore write failures (private browsing with full storage).
  }
}

// Listen for OS theme changes and update effective theme when in "system" mode.
let mediaUnsubscribe: (() => void) | null = null;

// Zustand store type alias used for the media-listener attachment helper.
type ThemeStore = ReturnType<typeof createStore>;

function attachMediaListener(store: ThemeStore): void {
  if (mediaUnsubscribe !== null) {
    mediaUnsubscribe();
    mediaUnsubscribe = null;
  }
  const mq = window.matchMedia("(prefers-color-scheme: light)");
  const handler = () => {
    const current = store.getState().theme;
    if (current === "system") {
      applyTheme("system");
      store.setState({ effective: resolveEffective("system") });
    }
  };
  mq.addEventListener("change", handler);
  mediaUnsubscribe = () => {
    mq.removeEventListener("change", handler);
  };
}

function createStore() {
  return create<ThemeState>((set, get) => {
    const stored = readStored();
    applyTheme(stored);
    return {
      theme: stored,
      effective: resolveEffective(stored),
      setTheme: (t: Theme, origin?: { x: number; y: number }) => {
        const prev = get().theme;
        if (prev === t) return;
        // View Transitions theme wipe: wraps the DOM mutation in
        // startViewTransition so the browser snapshots the before/after
        // frames and we can run a CSS-only circular reveal from the
        // click origin.  Falls back to an instant swap on browsers
        // without the API (Firefox today) or when the user has
        // prefers-reduced-motion enabled.
        const startViewTransition: StartViewTransition = (
          document as unknown as { startViewTransition?: StartViewTransition }
        ).startViewTransition;
        const reduceMotion =
          typeof window !== "undefined" &&
          window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        // Skip the View Transitions wrap when driven by an automation
        // tool (Playwright, Selenium, Puppeteer).  Tests need the DOM
        // mutation to be visible synchronously; the animation has no
        // value in a headless context and the deferred swap creates
        // flaky polls.
        const isAutomated =
          typeof navigator !== "undefined" && navigator.webdriver;
        // Firefox same-document View Transitions are still flagged/partial as of
        // 2026; the API can detect as `function` but the callback may not fire
        // reliably, leaving the theme un-swapped. Skip the wipe entirely on
        // Firefox so the toggle is instant + reliable cross-browser.
        const isFirefox =
          typeof navigator !== "undefined" &&
          /Firefox/i.test(navigator.userAgent);
        const canAnimate =
          typeof startViewTransition === "function" &&
          !reduceMotion &&
          !isAutomated &&
          !isFirefox;
        const swap = (): void => {
          applyTheme(t);
          set({ theme: t, effective: resolveEffective(t) });
        };
        if (canAnimate) {
          const root = document.documentElement;
          if (origin !== undefined) {
            root.style.setProperty("--theme-wipe-x", `${String(origin.x)}px`);
            root.style.setProperty("--theme-wipe-y", `${String(origin.y)}px`);
          } else {
            root.style.removeProperty("--theme-wipe-x");
            root.style.removeProperty("--theme-wipe-y");
          }
          // Defensive: any throw from startViewTransition (partial impls,
          // unsupported chained calls) falls back to a direct swap so the
          // theme always lands.
          try {
            startViewTransition(swap);
          } catch {
            swap();
          }
        } else {
          swap();
        }
      },
    };
  });
}

// Singleton store instance.
const _store = createStore();
// Attach media-query listener using the singleton.
attachMediaListener(_store);

export const useThemeStore = _store;
