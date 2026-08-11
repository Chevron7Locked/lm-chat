/**
 * Flow 42 — Copy message button on mobile viewport.
 *
 * Mobile-specific smoke test. Proves:
 *  1. Action row is always visible (no hover on touch devices)
 *  2. Copy button is rendered and functional
 *  3. Toast appears on copy tap
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

test.describe("Flow 42 — Copy message on mobile", () => {
  test("copy button visible and functional without hover", async ({ page, browserName }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: 7,
              user_id: 1,
              title: "Chat 7",
              folder: null,
              pinned: false,
              created_at: "2026-05-22T12:00:00Z",
              updated_at: "2026-05-22T12:00:00Z",
              settings: {},
              display_order: 0,
              model_id: null,
            },
          ]),
        });
      }
      return route.continue();
    });
    await page.route("**/api/chats/7", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 7,
          user_id: 1,
          title: "Chat 7",
          folder: null,
          pinned: false,
          created_at: "2026-05-22T12:00:00Z",
          updated_at: "2026-05-22T12:00:00Z",
          messages: [
            {
              id: 100,
              chat_id: 7,
              role: "user",
              content: "hello from mobile",
              reasoning_content: null,
              created_at: "2026-05-22T12:00:00Z",
            },
          ],
          has_more: false,
        }),
      }),
    );

    // clipboard-read/write are Chromium-only permission strings — Firefox
    // rejects "clipboard-read" from grantPermissions. This test only taps
    // Copy and asserts the toast (it never reads clipboard contents), and
    // Firefox allows writeText on a user gesture without a grant, so the
    // grant is Chromium-only. (The dark-mode/light-mode projects also run
    // the Chromium engine, so browserName === "chromium" covers them.)
    if (browserName === "chromium") {
      await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    }
    await page.goto("/chats/7");

    // Action row is always visible on mobile (no hover required)
    const messageRow = page.locator('[data-message-id="100"]');
    const actionRow = messageRow.locator(".lmchat-message-actions");
    await expect(actionRow).toBeVisible({ timeout: 5_000 });
    await expect(actionRow).toHaveCSS("opacity", "1");

    // Copy button is visible
    await expect(page.getByTestId("chat-message-copy-btn-100")).toBeVisible({
      timeout: 5_000,
    });

    // Tap copy button and verify toast
    await page.getByTestId("chat-message-copy-btn-100").tap();
    await expect(page.getByText("Message copied.")).toBeVisible({
      timeout: 5_000,
    });
  });
});
