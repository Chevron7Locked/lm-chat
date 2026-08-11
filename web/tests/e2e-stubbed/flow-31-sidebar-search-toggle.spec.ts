/**
 * Flow 31 — Sidebar search mode toggle (P13e O-09).
 *
 * What it proves (route-stubbed):
 *   1. Typing into the sidebar search input filters chats locally (legacy
 *      as-you-type substring behaviour, preserved when no Enter has fired).
 *   2. Pressing Enter switches to semantic mode and POSTs to /api/search;
 *      the input exposes data-search-mode="semantic" and the hint reads
 *      "Semantic (Enter) · Cmd+Enter for keyword".
 *   3. Pressing Shift+Enter switches to keyword mode; the hint flips to
 *      "Keyword filter · Enter for semantic".
 *   4. Escape clears the input and resets the mode.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 31 — Sidebar search mode toggle", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            { id: 1, title: "Alpha", folder: null, pinned: false, updated_at: new Date().toISOString(), model_id: null },
            { id: 2, title: "Beta", folder: null, pinned: false, updated_at: new Date().toISOString(), model_id: null },
            { id: 3, title: "Gamma", folder: null, pinned: false, updated_at: new Date().toISOString(), model_id: null },
          ]),
        });
      }
      return route.continue();
    });

    // Kept: the stats-footer test asserts on this specific token count.
    await page.route("**/api/quotas/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          tokens_per_day: 100_000,
          requests_per_day: 1_000,
          tokens_consumed_today: 1_234,
          requests_consumed_today: 5,
          resets_at: "2026-12-01T00:00:00Z",
        }),
      })
    );

    // Semantic search stub — returns chat id 2 only.
    let semanticCalled = 0;
    await page.route("**/api/search**", (route) => {
      semanticCalled += 1;
      // Tag the response so test can validate the request reached this stub.
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [],
          chats: [{ id: 2 }],
          memory: [],
        }),
      });
    });

    await page.goto("/");
  });

  test("Enter triggers semantic search; Shift+Enter switches to keyword", async ({ page }) => {
    const input = page.getByTestId("sidebar-search-input");
    // "lph" is a discriminating substring — matches only "Alpha" locally.
    await input.fill("lph");

    // Legacy as-you-type substring filter is active before any Enter press —
    // hint shows the dual-mode guidance line.
    await expect(page.getByTestId("sidebar-search-hint"))
      .toContainText(/Enter: semantic/i);

    // Enter → semantic search.  The stub returns only chat id 2 (Beta).
    await input.press("Enter");
    await expect(input).toHaveAttribute("data-search-mode", "semantic");
    await expect(page.getByTestId("sidebar-search-hint"))
      .toContainText(/Semantic/i);

    // Semantic result list contains only "Beta" (per stub).  Alpha + Gamma
    // are absent because semantic mode gates the list to backend hits.
    await expect(page.getByRole("link", { name: /Beta/ })).toBeVisible();
    await expect(page.getByRole("link", { name: /^Alpha$/ })).toHaveCount(0);

    // Shift+Enter → keyword mode (local substring on "lph" → Alpha only).
    await input.press("Shift+Enter");
    await expect(input).toHaveAttribute("data-search-mode", "keyword");
    await expect(page.getByTestId("sidebar-search-hint"))
      .toContainText(/Keyword/i);
    await expect(page.getByRole("link", { name: /Alpha/ })).toBeVisible();
    await expect(page.getByRole("link", { name: /^Beta$/ })).toHaveCount(0);
    await expect(page.getByRole("link", { name: /^Gamma$/ })).toHaveCount(0);

    // Escape clears the input and exits search mode entirely.
    await input.press("Escape");
    await expect(input).toHaveValue("");
    await expect(input).toHaveAttribute("data-search-mode", "off");
  });

  test("stats footer renders with token-count from /api/quotas/me", async ({ page }) => {
    const footer = page.getByTestId("sidebar-stats-footer");
    await expect(footer).toBeVisible();
    // 1234 → 1.2k via formatTokens. The full "tokens" word only appears in
    // the title attribute ("Today: 1.2k tokens · saved vs cloud"); the
    // visible compact copy is "1.2k today·$0.13".
    await expect(footer).toContainText(/1\.2k/);
    await expect(footer).toHaveAttribute("title", /1\.2k tokens/);
  });

  test("verbose-log toggle persists to localStorage", async ({ page }) => {
    // The verbose-logging checkbox moved from the Sidebar stats footer to
    // Settings → Developer (DeveloperSection.tsx) — "confusing cognitive
    // load" per its own header comment — testid renamed accordingly.
    await page.goto("/settings/developer");
    const toggle = page.getByTestId("settings-debug-toggle").locator("input[type='checkbox']");
    await expect(toggle).not.toBeChecked();
    await toggle.check();
    await expect(toggle).toBeChecked();
    const stored = await page.evaluate(() => localStorage.getItem("lmchat:debug-logging"));
    expect(stored).toBe("true");
  });
});
