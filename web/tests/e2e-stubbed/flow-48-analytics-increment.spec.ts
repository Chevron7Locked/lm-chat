/**
 * Flow 48 — Analytics token increment.
 *
 * What it proves (route-stubbed):
 *   1. Stub /api/quotas/me returning analytics tokens data.
 *   2. The tokens-today value renders on the analytics page.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 48 — Analytics token increment", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Kept: the test's assertion depends on this specific token count.
    await page.route("**/api/quotas/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tokens_per_day: 50000, requests_per_day: 500, tokens_consumed_today: 1500, requests_consumed_today: 12, resets_at: "2026-06-15T00:00:00Z" }) })
    );
    await page.route("**/api/analytics/me", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          total_messages: 150,
          total_chats: 25,
          messages_last_7_days: 45,
          top_models: [{ model_id: "qwen2.5-7b-instruct", count: 100 }],
          messages_by_day: [],
        }),
      });
    });
  });

  test("analytics page shows tokens-today from quota endpoint", async ({ page }) => {
    await page.goto("/analytics");
    await expect(page.locator("h1")).toContainText("Analytics", { timeout: 10000 });

    // The tokens-today value should be visible somewhere on the page.
    // It appears as "1.5k · tokens today" in the sidebar stats.
    await expect(page.getByText(/1\.5k.*tokens today|tokens today.*1\.5k/i)).toBeVisible({ timeout: 5000 });
  });
});
