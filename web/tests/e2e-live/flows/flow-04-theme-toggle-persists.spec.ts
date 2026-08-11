/**
 * Flow 4 — Theme toggle persistence.
 *
 * What it proves:
 *   The user can toggle the theme between dark and light via the Settings page.
 *   After toggling:
 *     1. The document.documentElement reflects the correct CSS class ("light"
 *        for light mode; no class for dark mode).
 *     2. The theme preference is written to localStorage under `lmchat:theme`.
 *     3. After a full page reload the theme is restored from localStorage.
 *
 * Note on themeStore.ts:
 *   The LS key is "lmchat:theme" (constant LS_KEY in themeStore.ts:24).
 *   Light mode adds class="light" to <html>; dark mode has no class.
 *   The Settings page renders three buttons: "Dark", "Light", "System".
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
} from "./_flow-helpers";

test(
  "flow-04: theme toggle dark→light persists to localStorage and survives reload",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Navigate to Settings → Appearance (the theme toggle lives on the
    // Appearance tab, not the default Account tab).  Without this the
    // Dark / Light / System buttons never render and the test times out.
    await page.goto(`${backendURL}/settings/appearance`);
    await page.waitForURL(`${backendURL}/settings/appearance`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {/* RQ retries */});

    // Ensure Settings heading is visible.
    await expect(
      page.getByRole("heading", { name: "Settings" })
    ).toBeVisible({ timeout: 8_000 });

    // First set to Dark to establish a known baseline.
    const darkBtn = page.getByRole("button", { name: "Dark", exact: true });
    await expect(darkBtn).toBeVisible({ timeout: 5_000 });
    await darkBtn.click();

    // Verify dark mode: no "light" class on <html>.
    await expect(page.locator("html")).not.toHaveClass(/light/, { timeout: 3_000 });

    // POLL localStorage — the OVERDRIVE theme wipe wraps the DOM
    // mutation in startViewTransition(), which can defer the
    // localStorage write across a RAF.  Polling tolerates that
    // without making the test brittle.
    await expect.poll(
      async () => page.evaluate(() => localStorage.getItem("lmchat:theme")),
      { timeout: 5_000 },
    ).toBe("dark");

    // Now switch to Light.
    const lightBtn = page.getByRole("button", { name: "Light", exact: true });
    await lightBtn.click();

    // Verify light mode: "light" class added to <html>.
    await expect(page.locator("html")).toHaveClass(/light/, { timeout: 5_000 });

    // Read localStorage.
    const themeAfterLight = await page.evaluate(() =>
      localStorage.getItem("lmchat:theme")
    );
    expect(themeAfterLight).toBe("light");

    // Reload — theme should restore from localStorage.
    await page.reload({ waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {/* RQ retries */});

    // After reload, light class should still be present (restored from LS).
    await expect(page.locator("html")).toHaveClass(/light/, { timeout: 5_000 });

    // Toggle back to Dark so we leave the worker session in dark mode
    // (avoids polluting subsequent tests in the same worker if any share LS).
    await page.goto(`${backendURL}/settings/appearance`);
    await page.waitForURL(`${backendURL}/settings/appearance`, { timeout: 10_000 });
    const darkBtnAgain = page.getByRole("button", { name: "Dark", exact: true });
    await darkBtnAgain.waitFor({ state: "visible", timeout: 8_000 });
    await darkBtnAgain.click();

    assertNoConsoleErrors(collectErrors(), "flow-04");
  }
);
