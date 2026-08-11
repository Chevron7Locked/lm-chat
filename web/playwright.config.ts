import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for lm-chat web e2e tests (route-stubbed suite).
 *
 * Uses `vite preview` on the production build (web/dist) so the suite does
 * not depend on a running dev server or an already-occupied port 5173.
 * Port 5192 is used to avoid the common 5173-5174 range occupied by other
 * Vite instances during development AND port 5180 which on this machine is
 * held by a sibling project's dev server (2026-06-04). With
 * ``reuseExistingServer: true``, the suite will silently use ANY listener
 * on the chosen port — so the port choice matters.
 *
 * Prerequisites: run `pnpm build` once before executing this suite.
 *
 * Live backend requirement: the login e2e tests use route stubbing so they
 * do not require a running FastAPI backend. The Playwright route intercept
 * simulates /api/auth/login responses. To run against the real backend,
 * remove the route stubs in tests/e2e-stubbed/login.spec.ts and start the
 * backend separately. The live-backend suite lives in tests/e2e-live/ and
 * runs via playwright.live.config.ts.
 */
export default defineConfig({
  testDir: "./tests/e2e-stubbed",
  fullyParallel: true,
  forbidOnly: !!process.env["CI"],
  retries: process.env["CI"] ? 2 : 0,
  ...(process.env["CI"] ? { workers: 1 } : {}),
  reporter: "html",
  use: {
    baseURL: process.env["PLAYWRIGHT_BASE_URL"] ?? "http://localhost:5192",
    trace: "on-first-retry",
  },
  // Gap 5: visual regression — screenshot comparison settings.
  // Baselines are stored in tests/screenshots/__snapshots__/.
  // Regenerate with `make visual-baseline`.
  expect: {
    toHaveScreenshot: {
      // 1% pixel-ratio tolerance; tighten to 0 for pixel-perfect CI.
      maxDiffPixelRatio: 0.01,
      animations: "disabled",
    },
  },
  // Firefox parity: the stubbed e2e suite runs against both Chromium AND
  // Firefox so a Firefox-only regression in one of the core UX paths
  // (real dropdown, Enter sends, optimistic render, no dead-ends) fires
  // in CI, not at demo time.
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    // ── Theme-aware a11y projects (used by playwright.live.config.ts) ───────
    {
      name: "dark-mode",
      use: {
        ...devices["Desktop Chrome"],
        contextOptions: { colorScheme: "dark", reducedMotion: "reduce" },
      },
    },
    {
      name: "light-mode",
      use: {
        ...devices["Desktop Chrome"],
        contextOptions: { colorScheme: "light", reducedMotion: "reduce" },
      },
    },
  ],
  webServer: {
    command: "pnpm vite preview --port 5192 --strictPort",
    url: "http://localhost:5192",
    reuseExistingServer: !process.env["CI"],
    timeout: 30_000,
  },
});
