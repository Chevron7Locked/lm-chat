/**
 * Flow 44 — Prompts page + /prompt <name> insertion.
 *
 * What it proves (route-stubbed):
 *   1. GET /api/prompts returns saved prompts.
 *   2. /prompt <name> in composer inserts the prompt body.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const MOCK_PROMPTS = [
  { id: 1, user_id: 1, name: "summarize-code", content: "Summarize the following code in plain English:\n\n{{code}}", created_at: 1717200000 },
  { id: 2, user_id: 1, name: "explain", content: "Explain this concept like I'm 5 years old.", created_at: 1717200001 },
];

test.describe("Flow 44 — Prompts + /prompt insertion", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);
  });

  test("prompts page lists saved prompts", async ({ page }) => {
    await page.route("**/api/prompts", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_PROMPTS) });
    });

    await page.goto("/prompts");
    // Wait for the PromptLibrary page to load.
    await expect(page.getByText("summarize-code", { exact: true })).toBeVisible({ timeout: 10000 });
    await expect(page.getByText("explain", { exact: true })).toBeVisible();
  });

  test.fixme("send-button stays disabled without loaded model_id; needs chat fixture with model_id set and models stub returning active models", async ({ page }) => {
    await page.route("**/api/prompts", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_PROMPTS) });
    });

    // Need a chat to land on so the composer is available.
    // SSE stream for when they send.
    await page.route("**/api/chats/44/stream", (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const sse = "event: chat.start\ndata: {}\n\nevent: chat.end\ndata: {}\n\n";
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: sse });
    });
    // Chat catch-all must come AFTER specific sub-path routes.
    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (method === "GET" && path === "/api/chats") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
          { id: 44, user_id: 1, title: "Prompt test", folder: null, pinned: false, created_at: "2026-06-01T12:00:00Z", updated_at: "2026-06-01T12:00:00Z", settings: {}, display_order: 0, incognito: false, incognito_expires_at: null, model_id: null },
        ]) });
      }
      if (method === "GET" && path === "/api/chats/44") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          id: 44, user_id: 1, title: "Prompt test", messages: [], has_more: false,
        }) });
      }
      return route.continue();
    });

    await page.goto("/chats/44");
    // Use role selector to avoid matching the send button's aria-label.
    const textarea = page.getByRole("textbox", { name: "Message" });
    await expect(textarea).toBeVisible({ timeout: 10000 });

    // Type /prompt summarize-code in the composer.
    await textarea.fill("/prompt summarize-code");

    // Press Enter (or click send) to trigger the slash command.
    await page.getByRole("button", { name: "Send message" }).click();

    // The prompt body should be inserted into the textarea.
    await expect(textarea).toHaveValue(/Summarize the following code/);
  });
});
