/**
 * Flow 16 — Admin quotas enforce 429.
 *
 * What it proves (route-stubbed):
 *  1. Admin navigates to /admin/quotas and lowers user 2's quota via
 *     the PATCH /api/admin/quotas/:user_id form-encoded mutation.  We
 *     capture the request body and assert the new limits.
 *  2. A subsequent /api/chat/stream call returns 429 (over quota); the
 *     useSSE error path surfaces it via an inline alert (the Chat page
 *     renders "Stream error: …" when sseState.error is non-null).
 *
 * The 429 path proves the wire-error → UI-alert link without depending
 * on the backend's actual quota enforcement code.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 16 — Admin quota patch + 429 enforcement", () => {
  test("admin lowers quota → user 429 surfaces as inline stream error", async ({ page }) => {
    // Authed as an admin — bootstrap covers array/object defaults; this
    // spec is already signed in on cold load.
    await bootstrapAuthedApp(page, { isAdmin: true, username: "admin" });

    let lastQuotaPatchBody: string | null = null;

    // Admin quotas list.
    let quotas = [
      { user_id: 2, tokens_per_day: 100_000, requests_per_day: 1_000 },
    ];
    await page.route("**/api/admin/quotas", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(quotas),
        });
      }
      return route.continue();
    });

    // PATCH /api/admin/quotas/2.
    await page.route("**/api/admin/quotas/2", async (route) => {
      if (route.request().method() === "PATCH" || route.request().method() === "POST") {
        lastQuotaPatchBody = route.request().postData() ?? "";
        const params = new URLSearchParams(lastQuotaPatchBody);
        const tokens = Number(params.get("tokens_per_day"));
        const requests = Number(params.get("requests_per_day"));
        quotas = [
          { user_id: 2, tokens_per_day: tokens, requests_per_day: requests },
        ];
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(quotas[0]),
        });
      }
      return route.continue();
    });

    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: 1, title: "Test Chat", folder: null, pinned: false,
              updated_at: new Date().toISOString(), model_id: "qwen",
              display_order: 0, settings: {},
            },
          ]),
        });
      }
      return route.continue();
    });
    await page.route("**/api/chats/1", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1, user_id: 1, title: "Test Chat", folder: null, pinned: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [], has_more: false, settings: {},
        }),
      })
    );

    // Chat stream → 429 (over quota).
    await page.route("**/api/chat/stream", (route) =>
      route.fulfill({
        status: 429,
        contentType: "application/json",
        body: JSON.stringify({ detail: "quota exceeded" }),
      })
    );

    // ─── admin/quotas → edit → save. ────────────────────────────────────────
    await page.goto("/admin/quotas");
    // The page heading's accessible name is just "Quotas" — "Admin" renders
    // as a separate sibling breadcrumb node, not part of the heading.
    await expect(page.getByRole("heading", { name: "Quotas" })).toBeVisible({
      timeout: 5_000,
    });
    await page.getByRole("button", { name: "Edit" }).click();

    // Lower the tokens-per-day to 10 (a deliberately low limit).
    const tokensInput = page.getByLabel("Tokens per day for user 2");
    await tokensInput.fill("10");
    await page.getByRole("button", { name: "Save" }).click();

    // Assert the PATCH fired with the new value.
    await expect.poll(() => lastQuotaPatchBody, { timeout: 5_000 }).not.toBeNull();
    expect(lastQuotaPatchBody).toContain("tokens_per_day=10");

    // ─── Send a message → /api/chat/stream returns 429 → UI alert. ─────────
    await page.goto("/chats/1");
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("hello");
    await composer.press("Control+Enter");

    // useSSE surfaces the HTTP error inline.  The Chat page renders a
    // role="alert" element; the 429 copy reads "Too many requests — you're
    // sending faster than the server allows."
    await expect(
      page.getByRole("alert").filter({ hasText: /too many requests|quota|429/i })
    ).toBeVisible({
      timeout: 8_000,
    });
  });
});
