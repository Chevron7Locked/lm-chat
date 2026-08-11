/**
 * Flow 27 — Admin integrations route registered (P13c.1).
 *
 * What it proves (route-stubbed):
 *  1. The `/admin/integrations` path is registered in the router and is NOT
 *     swallowed by the catch-all that redirects unknown paths to `/`.
 *  2. An authenticated admin lands on the AdminIntegrations page heading,
 *     not on the Chat home (which is what the catch-all redirect would do).
 *
 * Regression bracket: B-02 (parity audit). The page existed at
 * `web/src/pages/AdminIntegrations.tsx` and `UserMenu` linked to it, but the
 * Route was missing from `web/src/router.tsx` — so the catch-all silently
 * redirected admins back to `/`.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 27 — /admin/integrations route registered", () => {
  test("admin navigates to /admin/integrations and lands on the page (not /)", async ({ page }) => {
    // Authed as an admin — bootstrap covers the array/object defaults the
    // would-be-redirect-to-Chat path would need (it must NOT fire).
    await bootstrapAuthedApp(page, { isAdmin: true, username: "admin" });

    await page.goto("/admin/integrations");

    // Assert: the page heading is visible, AND the URL did not get
    // catch-all-redirected back to `/`.
    await expect(
      page.getByRole("heading", { name: "Admin: Integrations" })
    ).toBeVisible({ timeout: 5_000 });
    expect(new URL(page.url()).pathname).toBe("/admin/integrations");
  });
});
