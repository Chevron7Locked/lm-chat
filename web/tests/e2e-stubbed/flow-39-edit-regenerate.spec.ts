/**
 * Flow 39 — P13l.1 Edit message + Regenerate.
 *
 * Stubbed-network smoke test.  Proves:
 *  1. After a user message is rendered, the Edit hover-button fires
 *     PATCH /api/messages/{id} with the new content.
 *  2. The copy button is visible on both user and assistant messages.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const CHAT_ID = 7;

test.describe("Flow 39 — P13l.1 edit + regenerate", () => {
  test("user message edit fires PATCH with the new content", async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Chat list + chat detail. Trailing ** catches ?unscoped=true on list
    // AND the /api/chats/:id subpath (single-* would miss the latter).
    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const path = new URL(route.request().url()).pathname;
      if (method === "GET" && path === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: CHAT_ID,
              user_id: 1,
              title: "Chat 7",
              folder: null,
              pinned: false,
              created_at: "2026-05-22T12:00:00Z",
              updated_at: "2026-05-22T12:00:00Z",
              settings: {},
              display_order: 0,
              incognito: false,
              incognito_expires_at: null,
              model_id: null,
            },
          ]),
        });
      }
      if (method === "GET" && path === `/api/chats/${String(CHAT_ID)}`) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: CHAT_ID,
            user_id: 1,
            title: "Chat 7",
            folder: null,
            pinned: false,
            created_at: "2026-05-22T12:00:00Z",
            updated_at: "2026-05-22T12:00:00Z",
            messages: [
              {
                id: 100,
                chat_id: CHAT_ID,
                role: "user",
                content: "original",
                reasoning_content: null,
                created_at: "2026-05-22T12:00:00Z",
                state: "final",
                response_id: null,
                model_id: null,
              },
            ],
            has_more: false,
          }),
        });
      }
      return route.fallback();
    });

    // PATCH /api/messages/100 — form-encoded body (content=<text>).
    let patchedContent: string | null = null;
    await page.route("**/api/messages/100", async (route) => {
      if (route.request().method() !== "PATCH") return route.fallback();
      const body = route.request().postData() ?? "";
      const params = new URLSearchParams(body);
      patchedContent = params.get("content");
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 100,
          chat_id: CHAT_ID,
          role: "user",
          content: patchedContent,
          reasoning_content: null,
          created_at: "2026-05-22T12:00:00Z",
          state: "final",
          response_id: null,
          model_id: null,
        }),
      });
    });

    await page.goto(`/chats/${String(CHAT_ID)}`);

    // The message-row must be hovered to reveal the action bar (CSS opacity
    // transition; the edit button has opacity:0 until the row is hovered).
    const msgRow = page.locator(`[data-message-id="100"]`);
    await expect(msgRow).toBeVisible({ timeout: 10_000 });
    await msgRow.hover();

    const editBtn = page.getByTestId("chat-message-edit-btn-100");
    await expect(editBtn).toBeVisible({ timeout: 5_000 });
    await editBtn.click();

    const ta = page.getByTestId("chat-message-edit-textarea-100");
    await expect(ta).toBeVisible({ timeout: 4_000 });
    await ta.fill("edited via e2e");
    await page.getByTestId("chat-message-edit-save-100").click();

    await expect.poll(() => patchedContent, { timeout: 5_000 }).toBe("edited via e2e");
  });

  test("copy button visible on user and assistant messages", async ({ page }) => {
    await bootstrapAuthedApp(page);

    // Chat list + chat detail with both user and assistant messages.
    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const path = new URL(route.request().url()).pathname;
      if (method === "GET" && path === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: CHAT_ID,
              user_id: 1,
              title: "Chat 7",
              folder: null,
              pinned: false,
              created_at: "2026-05-22T12:00:00Z",
              updated_at: "2026-05-22T12:00:00Z",
              settings: {},
              display_order: 0,
              incognito: false,
              incognito_expires_at: null,
              model_id: null,
            },
          ]),
        });
      }
      if (method === "GET" && path === `/api/chats/${String(CHAT_ID)}`) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: CHAT_ID,
            user_id: 1,
            title: "Chat 7",
            folder: null,
            pinned: false,
            created_at: "2026-05-22T12:00:00Z",
            updated_at: "2026-05-22T12:00:00Z",
            messages: [
              {
                id: 100,
                chat_id: CHAT_ID,
                role: "user",
                content: "original",
                reasoning_content: null,
                created_at: "2026-05-22T12:00:00Z",
                state: "final",
                response_id: null,
                model_id: null,
              },
              {
                id: 101,
                chat_id: CHAT_ID,
                role: "assistant",
                content: "hello back",
                reasoning_content: null,
                created_at: "2026-05-22T12:00:01Z",
                state: "final",
                response_id: null,
                model_id: null,
              },
            ],
            has_more: false,
          }),
        });
      }
      return route.fallback();
    });

    await page.goto(`/chats/${String(CHAT_ID)}`);

    // Hover the user message row to reveal the action bar (CSS opacity reveal).
    const userMsgRow = page.locator('[data-message-id="100"]');
    await expect(userMsgRow).toBeVisible({ timeout: 10_000 });
    await userMsgRow.hover();
    await expect(page.getByTestId("chat-message-copy-btn-100")).toBeVisible({
      timeout: 5_000,
    });

    // Hover the assistant message row to reveal its action bar.
    const asstMsgRow = page.locator('[data-message-id="101"]');
    await asstMsgRow.hover();
    await expect(page.getByTestId("chat-message-copy-btn-101")).toBeVisible({
      timeout: 5_000,
    });

    // Copy button is last in action row for assistant.
    const assistantActions = page.locator(
      '[data-message-id="101"] .lmchat-message-actions > button:last-child',
    );
    await expect(assistantActions).toHaveAttribute(
      "data-testid",
      expect.stringMatching(/^chat-message-copy-btn-/),
    );

    // Click copy on user message and verify toast.
    await userMsgRow.hover();
    await page.getByTestId("chat-message-copy-btn-100").click();
    await expect(page.getByText("Message copied.")).toBeVisible({
      timeout: 5_000,
    });
  });
});
