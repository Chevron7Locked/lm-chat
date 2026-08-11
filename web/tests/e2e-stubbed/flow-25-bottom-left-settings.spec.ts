/**
 * Flow 25 — Bottom-left UserMenu Settings (P13a, fixes B-01 / A-08 / S-11).
 *
 * What it proves (route-stubbed):
 *  1. The Sidebar's UserMenu "Settings" item is no longer a dead callback;
 *     clicking it navigates to ``/settings``.
 *  2. ``/settings`` lands on the default tab (Profile — the first item in
 *     the Account nav group; see pages/Settings.tsx NAV_GROUPS).
 *  3. Clicking the Profile tab sets the URL to ``/settings/profile``.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 25 — bottom-left Settings click", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);
    await page.goto("/");
  });

  test("UserMenu Settings navigates to /settings (no-op fix)", async ({ page }) => {
    await page.getByTestId("user-menu-avatar").click();
    await page.getByTestId("user-menu-settings").click();
    await page.waitForURL((url) => url.pathname.startsWith("/settings"));
    await expect(page.getByTestId("settings-page")).toBeVisible();
  });

  test("/settings lands on Profile by default", async ({ page }) => {
    await page.goto("/settings");
    // Settings tabs follow the ARIA tabs pattern (aria-selected on the
    // active <button role="tab">), not aria-current.
    await expect(page.getByTestId("settings-tab-profile")).toHaveAttribute(
      "aria-selected", "true"
    );
  });

  test("clicking Profile tab updates URL to /settings/profile", async ({ page }) => {
    await page.goto("/settings");
    await page.getByTestId("settings-tab-profile").click();
    await page.waitForURL((url) => url.pathname === "/settings/profile");
    await expect(page.getByTestId("settings-tab-profile")).toHaveAttribute(
      "aria-selected", "true"
    );
  });
});
