/* SPDX-License-Identifier: Apache-2.0 */
/**
 * visual.spec.ts — Playwright visual regression harness for LMChat.
 *
 * These tests capture screenshots and diff them against stored baselines
 * (PNG snapshots committed to the repo). On first run use
 *   `make visual-baseline`
 * to generate the baseline images. Subsequent runs via `make visual-test`
 * will fail if any pixel delta exceeds Playwright's default threshold.
 *
 * SETUP NOTE: Run `make visual-baseline` (needs app on :8011) before
 * running `make visual-test`. Baselines are stored in the auto-generated
 * __snapshots__ directory next to this file.
 *
 * Config: uses playwright.config.ts webServer (pnpm vite preview on :5192),
 * so `pnpm build` must be run first. For live-app snapshots override
 * PLAYWRIGHT_BASE_URL=http://localhost:8011 in the environment.
 */
import { test, expect } from "@playwright/test";

// ---------------------------------------------------------------------------
// Login page — no auth required; always reachable in stubbed mode.
// ---------------------------------------------------------------------------

// TODO: run `make visual-baseline` to generate baseline snapshots first,
// then remove the .skip modifier to enable the gate in CI.
test.describe.skip("Visual regression — login page", () => {
  test("login page matches baseline", async ({ page }) => {
    await page.goto("/");
    // Wait for the login form to be visible (the SPA redirects unauthenticated
    // users to /login or shows the login form inline).
    await page
      .getByRole("heading", { name: /sign in|log in|lmchat/i })
      .first()
      .waitFor({ state: "visible", timeout: 10_000 })
      .catch(() => {
        // Heading may differ; proceed with the raw DOM state.
      });
    // Drain any CSS transitions before snapshotting.
    await page.waitForTimeout(300);
    await expect(page).toHaveScreenshot("login-page.png", {
      fullPage: false,
      animations: "disabled",
    });
  });
});

// ---------------------------------------------------------------------------
// Home / empty-state — requires the app on :8011 and a valid session cookie.
//
// Enable by removing .skip once baselines are captured with:
//   PLAYWRIGHT_BASE_URL=http://localhost:8011 make visual-baseline
// ---------------------------------------------------------------------------
test.describe.skip("Visual regression — home page (live app)", () => {
  test("home page empty state matches baseline", async ({ page }) => {
    await page.goto("/");
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(400);
    await expect(page).toHaveScreenshot("home-empty-state.png", {
      fullPage: false,
      animations: "disabled",
    });
  });
});
