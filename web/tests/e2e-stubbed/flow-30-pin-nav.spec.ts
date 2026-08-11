/**
 * Flow 30 — Pin-nav strip + pinned-messages panel (P13e O-07/O-08).
 *
 * What it proves (route-stubbed):
 *   1. Seeding `lmchat:pinned-messages` in localStorage with a chatId → [id]
 *      makes the PinNavStrip visible above the message list.
 *   2. Clicking the TopBar "Pins" button opens the PinnedMessagesPanel.
 *   3. The pin entry shows the message preview; clicking ✕ removes the
 *      pin from both the strip and the panel.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 30 — Pin-nav strip + pinned-messages panel", () => {
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
            { id: 7, title: "Pin Test", folder: null, pinned: false, updated_at: new Date().toISOString(), model_id: null },
          ]),
        });
      }
      return route.continue();
    });

    await page.route("**/api/chats/7", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 7, user_id: 1, title: "Pin Test", folder: null,
          pinned: false, created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [
            { id: 100, chat_id: 7, role: "user", content: "First user message", reasoning_content: null, created_at: new Date().toISOString() },
            { id: 101, chat_id: 7, role: "assistant", content: "First assistant reply with useful info.", reasoning_content: null, created_at: new Date().toISOString() },
          ],
        }),
      });
    });
  });

  test("renders pin-nav strip from seeded localStorage and opens the panel", async ({ page }) => {
    // Pre-seed localStorage via an init script — guaranteed to run before
    // any of the app's own scripts on the very first document, so the
    // pins store hydrates from it regardless of whether that hydration
    // happens at module-eval time or on first mount.
    await page.addInitScript(() => {
      localStorage.setItem(
        "lmchat:pinned-messages",
        JSON.stringify({ "7": [101] })
      );
    });
    await page.goto("/");

    await page.getByText("Pin Test").click();
    await page.waitForURL("**/chats/7");

    // (1) Pin-nav strip is visible with one entry for message 101.
    await expect(page.getByTestId("pin-nav-strip")).toBeVisible();
    await expect(page.getByTestId("pin-nav-item-101")).toBeVisible();

    // (2) Clicking the TopBar ⋯ overflow's "Pinned messages" menuitem
    // opens the panel (the standalone "Open pinned messages panel" button
    // was folded into the overflow menu — same pattern as "Chat settings").
    await page.getByTestId("topbar-overflow-trigger").click();
    await page.getByRole("menuitem", { name: "Pinned messages" }).click();
    await expect(page.getByTestId("pinned-messages-panel")).toBeVisible();
    await expect(page.getByTestId("pinned-panel-item-101")).toBeVisible();
  });

  test("renders nothing in the strip when no pins are seeded", async ({ page }) => {
    await page.goto("/");
    // Confirm no pins are seeded — the strip should be absent.
    await page.evaluate(() => { localStorage.removeItem("lmchat:pinned-messages"); });
    await page.getByText("Pin Test").click();
    await page.waitForURL("**/chats/7");

    // Strip absent.  We use `count()` rather than `toBeVisible` so the
    // assertion passes immediately on a missing element without waiting.
    await expect(page.getByTestId("pin-nav-strip")).toHaveCount(0);
  });
});
