/**
 * Flow 29 — Thinking indicator visibility during streaming (P13e O-02).
 *
 * What it proves (route-stubbed):
 *   1. After clicking into a chat and sending a message, while the SSE stream
 *      is open and no content tokens have been received, the
 *      `[data-testid='thinking-indicator']` element is visible.
 *   2. As soon as the first content delta arrives, the indicator disappears.
 *
 * The SSE stub deliberately delays the first message.delta event so the
 * "open-but-empty" window is observable by Playwright.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 29 — Thinking indicator", () => {
  test("appears while SSE is open with no tokens, then disappears", async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            { id: 1, title: "Thinking Test", folder: null, pinned: false, updated_at: new Date().toISOString(), model_id: null },
          ]),
        });
      }
      return route.continue();
    });

    await page.route("**/api/chats/1", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1, user_id: 1, title: "Thinking Test", folder: null,
          pinned: false, created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(), messages: [],
        }),
      });
    });

    // ─── SSE stub — hold the response, then deliver the full stream ─────────
    // route.fulfill() always sends a COMPLETE body and closes the
    // connection — Playwright has no API for genuinely incremental SSE
    // chunks. Fulfilling with chat.start only (no chat.end) doesn't hold
    // the "open" window either: the app's SSE client sees the connection
    // close without a terminal event and immediately surfaces a stream
    // error ("model stopped sending before any reply arrived"), which
    // races out the thinking indicator before the assertion below runs.
    // Instead, delay the fulfill() call itself: the fetch stays genuinely
    // pending (no bytes received at all) for the delay window, which is
    // exactly the "open with no tokens" state the Thinking indicator is
    // meant to cover — then deliver the complete chat.start+delta+end in
    // one shot.
    await page.route("**/api/chat/stream", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 2_000));
      const chatStart = `event: chat.start\ndata: ${JSON.stringify({ type: "chat.start", msg_id: 1, response_id: "resp-1" })}\n\n`;
      const delta = `event: message.delta\ndata: ${JSON.stringify({ type: "message.delta", msg_id: 1, delta: "Hello!" })}\n\n`;
      const end = `event: chat.end\ndata: ${JSON.stringify({ type: "chat.end", msg_id: 1 })}\n\n`;
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: chatStart + delta + end,
      });
    });

    await page.goto("/");
    await page.getByText("Thinking Test").click();
    await page.waitForURL("**/chats/1");

    // Send a message — the stream stub holds the response pending for 2s.
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("Are you thinking?");
    await composer.press("Control+Enter");

    // The Thinking indicator should appear while the request is in flight
    // and no content tokens have arrived.
    await expect(page.getByTestId("thinking-indicator")).toBeVisible({ timeout: 1_500 });

    // Once the full stream lands, the indicator disappears and the reply
    // renders.
    await expect(page.getByText("Hello!")).toBeVisible({ timeout: 5_000 });
    await expect(page.getByTestId("thinking-indicator")).not.toBeVisible();
  });
});
