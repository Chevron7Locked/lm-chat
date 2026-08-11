/**
 * A/B compare smoke test — M-002 P8b.2-MID-AUDIT-r1.
 *
 * Verifies that when a chat has ab_compare.enabled === true the Chat page
 * renders ABCompareView (two panes) instead of the normal message list.
 *
 * Test inventory (1 case):
 *   1. ab-compare panes visible when ab_compare.enabled is patched on a chat
 */

import { test, expect } from "./_fixtures";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Log in and wait for the home page to load. */
async function loginAndWait(
  page: import("@playwright/test").Page,
  backendURL: string,
  username: string,
  password: string
): Promise<void> {
  await page.goto(backendURL);
  await page.getByLabel("Username").fill(username);
  await page.locator("#lmchat-password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${backendURL}/`, { timeout: 10_000 });
}

/**
 * Create a chat via the API (form-encoded POST /api/chats) and return its id.
 * Uses fetch from within the browser context so cookies are attached.
 */
async function createChatViaApi(
  page: import("@playwright/test").Page,
  backendURL: string
): Promise<number> {
  const chatId = await page.evaluate(async (url: string) => {
    const params = new URLSearchParams({ title: "AB Smoke Test Chat" });
    const resp = await fetch(`${url}/api/chats`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: params.toString(),
      credentials: "include",
    });
    if (!resp.ok) throw new Error(`POST /api/chats → ${resp.status}`);
    const data = await resp.json() as { id: number };
    return data.id;
  }, backendURL);
  return chatId;
}

/**
 * PATCH the chat's ab_compare settings via the API.
 * model_a and model_b are set to placeholder IDs (the stub LM Studio
 * doesn't need them to be real loaded models for the view-render check).
 */
async function patchAbCompare(
  page: import("@playwright/test").Page,
  backendURL: string,
  chatId: number
): Promise<void> {
  await page.evaluate(
    async ({ url, id }: { url: string; id: number }) => {
      const params = new URLSearchParams({
        ab_compare: JSON.stringify({
          enabled: true,
          model_a: "qwen3-8b",
          model_b: "gemma-4-26b-a4b-it-mlx",
        }),
      });
      const resp = await fetch(`${url}/api/chats/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
        credentials: "include",
      });
      if (!resp.ok) throw new Error(`PATCH /api/chats/${id} → ${resp.status}`);
    },
    { url: backendURL, id: chatId }
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe("A/B compare view wiring (M-002)", () => {
  test(
    "ab_compare panes render when ab_compare.enabled is true",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Create a chat.
      const chatId = await createChatViaApi(page, backendURL);

      // Patch ab_compare settings.
      await patchAbCompare(page, backendURL, chatId);

      // Navigate to the chat page.
      await page.goto(`${backendURL}/chats/${chatId}`);
      await page.waitForURL(`${backendURL}/chats/${chatId}`, { timeout: 10_000 });

      // The Composer should be visible (ab mode still shows Composer).
      // Use the textarea specifically to avoid matching the "Send message" button.
      await expect(
        page.getByRole("textbox", { name: "Message" })
      ).toBeVisible({ timeout: 10_000 });

      // The A/B compare view should be present — identified by aria-label.
      const abView = page.getByLabel("A/B model comparison");
      await expect(abView).toBeVisible({ timeout: 5_000 });

      // Both pane regions should be rendered.
      const paneA = page.getByRole("region", { name: /Model.*response/i }).first();
      const paneB = page.getByRole("region", { name: /Model.*response/i }).nth(1);
      await expect(paneA).toBeVisible({ timeout: 5_000 });
      await expect(paneB).toBeVisible({ timeout: 5_000 });

      // The normal message list placeholder should NOT be visible.
      await expect(
        page.getByText("Send a message to start the conversation.")
      ).not.toBeVisible();
    }
  );
});
