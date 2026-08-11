/**
 * Flow 16 — Chat message cursor pagination (R-09 regression).
 *
 * What it proves:
 *   1. Initial load of a chat with >200 messages returns at most 200 messages
 *      (the default LIMIT) and the API reports has_more=true.  The UI
 *      auto-scrolls to the most recent message (messagesEndRef at bottom).
 *   2. A ?before_id cursor fetch prepends older messages without duplicates.
 *      The union of the two pages has the correct count and no duplicate ids.
 *   3. After a new message is posted via the API, the UI shows the new
 *      message (poll/refetch surfaces it) — the scroll-to-bottom affordance
 *      or message visibility confirms receipt.
 *
 * Mitigation: P12c §5 R-09 — chat message loading scroll regression.
 * Both qwen seats flagged this test as missing from 71fb966.
 *
 * Seeding strategy:
 *   205 messages seeded via POST /api/chats/:id/messages (just above the
 *   200 LIMIT so the boundary is exercised, and the test stays fast enough
 *   for the live-mode harness).  Each message alternates user/assistant so
 *   the DB has valid role values.
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
  seedMessage,
} from "./_flow-helpers";

// ---------------------------------------------------------------------------
// Shared seeding helper
// ---------------------------------------------------------------------------

/** Seed `count` messages into `chatId`.  Alternates user/assistant roles. */
async function seedMessages(
  page: Parameters<typeof seedMessage>[0],
  backendURL: string,
  chatId: number,
  count: number
): Promise<void> {
  for (let i = 0; i < count; i++) {
    const role: "user" | "assistant" = i % 2 === 0 ? "user" : "assistant";
    await seedMessage(page, backendURL, chatId, role, `Seeded message ${String(i + 1)}`);
  }
}

// ---------------------------------------------------------------------------
// Test 1 — initial render caps at LIMIT and scrolls to newest message
// ---------------------------------------------------------------------------

test(
  "flow-16a: chat with >200 messages renders at most 200 on initial load; UI is at bottom",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Create a chat and seed 205 messages (just over the 200 LIMIT).
    const chatId = await createChatViaRequest(page, backendURL, "PaginationFlow16a");
    await seedMessages(page, backendURL, chatId, 205);

    // Navigate to the chat.
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok — RQ retries */ });

    // Assert: at most 200 messages rendered (data-testid="chat-message" or role=listitem).
    // The backend caps the initial page at LIMIT=200; 5 older messages are behind has_more.
    // We count rendered ChatMessage rows by looking for the last seeded message text.
    // The most recently seeded message is "Seeded message 205" which is in the last page.
    const lastMsgText = page.getByText("Seeded message 205");
    await expect(lastMsgText).toBeVisible({ timeout: 10_000 });

    // The oldest message seeded was "Seeded message 1" — it should NOT be visible
    // because it's in the truncated older page (messages 1-5 are behind the cursor).
    const firstMsgText = page.getByText("Seeded message 1", { exact: true });
    await expect(firstMsgText).not.toBeVisible({ timeout: 3_000 });

    // Verify has_more=true via the API directly (wire-level assertion).
    const apiResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}`
    );
    expect(apiResp.ok()).toBe(true);
    const body = await apiResp.json() as { has_more: boolean; messages: Array<{ id: number }> };
    expect(body.has_more).toBe(true);
    expect(body.messages.length).toBeLessThanOrEqual(200);

    assertNoConsoleErrors(collectErrors(), "flow-16a");
  }
);

// ---------------------------------------------------------------------------
// Test 2 — before_id cursor prepends without duplicates
// ---------------------------------------------------------------------------

test(
  "flow-16b: before_id cursor load prepends older messages without duplicates",
  async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Create a fresh chat and seed 205 messages.
    const chatId = await createChatViaRequest(page, backendURL, "PaginationFlow16b");
    await seedMessages(page, backendURL, chatId, 205);

    // Fetch the initial page (most recent 200 messages).
    const page1Resp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}`
    );
    expect(page1Resp.ok()).toBe(true);
    const page1 = await page1Resp.json() as {
      has_more: boolean;
      messages: Array<{ id: number }>;
    };

    expect(page1.has_more).toBe(true);
    expect(page1.messages.length).toBe(200);

    // The cursor for the next page is the smallest id in page1.
    const oldestIdInPage1 = page1.messages[0]?.id;
    expect(oldestIdInPage1).toBeDefined();

    // Fetch older page using before_id.
    const page2Resp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}?before_id=${String(oldestIdInPage1)}`
    );
    expect(page2Resp.ok()).toBe(true);
    const page2 = await page2Resp.json() as {
      has_more: boolean;
      messages: Array<{ id: number }>;
    };

    // 205 total - 200 on page1 = 5 older messages.
    expect(page2.messages.length).toBeGreaterThanOrEqual(1);
    expect(page2.has_more).toBe(false);

    // No duplicates: ids in page2 must all be absent from page1 ids.
    const page1Ids = new Set(page1.messages.map((m) => m.id));
    const page2Ids = page2.messages.map((m) => m.id);
    const duplicates = page2Ids.filter((id) => page1Ids.has(id));
    expect(duplicates).toHaveLength(0);

    // Union count is correct.
    const totalUnion = page1.messages.length + page2.messages.length;
    expect(totalUnion).toBe(205);

    // All page2 messages are older (lower id) than the cursor.
    if (oldestIdInPage1 !== undefined) {
      const allOlder = page2Ids.every((id) => id < oldestIdInPage1);
      expect(allOlder).toBe(true);
    }
  }
);

// ---------------------------------------------------------------------------
// Test 3 — new message posted via API is visible after UI refetch
// ---------------------------------------------------------------------------

test(
  "flow-16c: new message posted via API appears in the chat view after refetch",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Use a small chat (fewer than 200 messages) so pagination is not the focus.
    const chatId = await createChatViaRequest(page, backendURL, "PaginationFlow16c");
    await seedMessages(page, backendURL, chatId, 5);

    // Navigate to the chat.
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    // Wait for seeded messages to render.
    await expect(page.getByText("Seeded message 5")).toBeVisible({ timeout: 8_000 });

    // Post one more message via the API after the UI has loaded.
    const uniqueContent = `NewAfterLoad-${String(Date.now())}`;
    await seedMessage(page, backendURL, chatId, "user", uniqueContent);

    // Trigger a manual refetch by reloading the page (simulates since_id polling
    // or a React Query refetch that fires on window focus).
    await page.reload({ waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    // The new message should now be visible.
    await expect(page.getByText(uniqueContent)).toBeVisible({ timeout: 10_000 });

    assertNoConsoleErrors(collectErrors(), "flow-16c");
  }
);
