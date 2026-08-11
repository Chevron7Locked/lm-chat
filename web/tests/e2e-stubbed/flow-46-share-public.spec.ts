/**
 * Flow 46 — /share/TOKEN public read-only.
 *
 * What it proves (route-stubbed):
 *   1. Navigate to a share URL with NO auth cookie.
 *   2. GET /api/share/{token} returns message list.
 *   3. Messages render read-only; composer is absent.
 */
import { test, expect } from "@playwright/test";

test.describe("Flow 46 — Public share view", () => {
  test("shared chat renders read-only with no composer", async ({ page }) => {
    // Stub /api/share/{token} — no auth needed.
    await page.route("**/api/share/*", (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "GET") return route.continue();
      if (!url.pathname.startsWith("/api/share/")) return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          title: "Shared Chat",
          created_at: "2026-06-01T12:00:00Z",
          messages: [
            { id: 101, role: "user", content: "What is the meaning of life?", reasoning_content: null, created_at: "2026-06-01T12:00:01Z" },
            { id: 102, role: "assistant", content: "42", reasoning_content: null, created_at: "2026-06-01T12:00:02Z" },
          ],
        }),
      });
    });

    // Navigate without auth cookie — browser has no session.
    await page.goto("/share/SHARE_TOKEN_XYZ", { waitUntil: "networkidle" });

    // The share page should show the chat title.
    await expect(page.getByText("Shared Chat")).toBeVisible({ timeout: 10000 });

    // Both messages should be visible.
    await expect(page.getByText("What is the meaning of life?")).toBeVisible();
    await expect(page.getByText("42")).toBeVisible();

    // No composer or chat input should be present on the share page.
    const composer = page.locator(".lmchat-composer-textarea, [aria-label='Message'], [data-testid='composer-textarea']");
    await expect(composer).toHaveCount(0);
  });
});