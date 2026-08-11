import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for the live-backend e2e suite.
 *
 * Covers:
 *   - tests/e2e-live/   — parity + smoke tests (spawns live FastAPI backend)
 *   - tests/a11y/       — axe-core sweep (also requires live backend via the
 *                         shared _fixtures.ts worker fixture)
 *
 * Server lifecycle is managed by the _fixtures.ts worker fixture —
 * no webServer block here.
 *
 * Run: pnpm test:e2e:live
 *
 * Theme projects (a11y):
 *   dark-mode  — default theme, 0-violation target for WCAG 2a+2aa
 *   light-mode — known-fail allowlist (shrink-only) for WCAG 2a+2aa
 *   §2B Accessibility extensions: each project seeds the appropriate theme
 *   via localStorage before page load so the SPA renders in the correct
 *   colour scheme without needing UI clicks.
 */
export default defineConfig({
  testDir: "./tests",
  testMatch: ["**/e2e-live/**/*.spec.ts", "**/a11y/**/*.spec.ts"],
  fullyParallel: false,
  forbidOnly: !!process.env["CI"],
  retries: 0,
  workers: 1,
  reporter: "html",
  use: {
    // baseURL is set per-worker by the fixture (dynamic port).
    trace: "on-first-retry",
    // TEST-HARNESS: emulate prefers-reduced-motion so the View
    // Transitions theme wipe (themeStore.setTheme), the
    // AnimatedSavedCounter tween, and the streaming-bubble mask
    // are all bypassed.  Each honours reduced-motion at the
    // source by skipping the wrap — the test sees the post-state
    // synchronously without waiting on RAF / view-transition
    // callbacks.  Removes a class of timing flakes that surfaced
    // after the OVERDRIVE commit.
    // Playwright 1.60: colorScheme + reducedMotion moved under contextOptions.
    contextOptions: {
      colorScheme: "dark",
      reducedMotion: "reduce",
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "firefox",
      use: { ...devices["Desktop Firefox"] },
    },
    // ── Theme-aware a11y projects ────────────────────────────────────────────
    {
      name: "dark-mode",
      use: {
        ...devices["Desktop Chrome"],
        contextOptions: {
          colorScheme: "dark",
          reducedMotion: "reduce",
        },
      },
    },
    {
      name: "light-mode",
      use: {
        ...devices["Desktop Chrome"],
        contextOptions: {
          colorScheme: "light",
          reducedMotion: "reduce",
        },
      },
    },
  ],
});
