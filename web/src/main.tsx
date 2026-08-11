/* SPDX-License-Identifier: Apache-2.0 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./globals.css";
import { AppRouter } from "./router";

// Eagerly import the theme store so its `applyTheme(stored)` boot
// effect runs on module load — BEFORE any route renders.  Without
// this, the store only initialized when a component imported
// `useThemeStore` (today: Settings/Appearance + the chat shell's
// keyboard shortcuts).  Standalone pages (Memory, Prompts,
// Documents, Analytics) never triggered the store, so the `.light`
// class was never added to <html> and the default-dark CSS leaked
// through — every route flip looked like a theme flip.  Import
// order matters: after globals.css so :root.light / :root.dark
// selectors exist before applyTheme runs.
import "./stores/themeStore";

// Eagerly import the display store so its applyAll() boot effect runs on
// module load — before any route renders. Mirrors themeStore pattern.
import "./stores/displayStore";

// Auto-recover from a stale lazy chunk after a redeploy. When the app is
// rebuilt, Vite re-hashes its chunks; an already-open tab that then navigates
// to a lazy route (e.g. /projects/:id) requests a chunk hash the server no
// longer has → a dynamic-import failure ("error loading dynamically imported
// module"). The shell is served network-first (see public/sw.js), so a reload
// pulls the fresh index + current chunk hashes. Guarded via sessionStorage so
// a genuinely-missing chunk can't cause a reload loop.
if (typeof window !== "undefined") {
  window.addEventListener("vite:preloadError", (event: Event) => {
    const KEY = "lmchat:last-preload-reload";
    const now = Date.now();
    const last = Number(window.sessionStorage.getItem(KEY) ?? "0");
    if (now - last > 15_000) {
      window.sessionStorage.setItem(KEY, String(now));
      event.preventDefault();
      window.location.reload();
    }
  });
}

// Signature for the curious developer who opens DevTools.  Single
// line, no styling tricks, no recruiting CTA — just the kind of
// acknowledgement that says a human cared.  Printed exactly once.
if (typeof console !== "undefined" && typeof window !== "undefined") {
  console.log(
    "%cLM Chat — running on your machine, by design.",
    "color: oklch(0.75 0.13 75); font-weight: 600;",
  );
}

// Register PWA service worker in production builds.
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((err: unknown) => {
      // Non-fatal: app works without the service worker.
      console.error("Service worker registration failed:", err);
    });
  });
}

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("missing #root element");

const root = createRoot(rootEl);
root.render(
  <StrictMode>
    <AppRouter />
  </StrictMode>,
);
