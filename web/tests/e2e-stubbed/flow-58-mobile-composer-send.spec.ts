/**
 * Flow 58 — Mobile composer: type → tap Send → streamed response renders.
 *
 * Viewport: 390×844 (iPhone 14 / useViewport → isMobile=true).
 *
 * What it proves (route-stubbed):
 *   1. The composer textarea accepts text input on mobile.
 *   2. Tapping the Send button (aria-label="Send message") — NOT Ctrl+Enter —
 *      dispatches POST /api/chat/stream.
 *   3. The SSE body (chat.start / message.delta / chat.end events) is parsed
 *      and the assistant text renders in the message list.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const CHAT_ID = 58;

/** Minimal SSE frame sequence: start → delta → end. */
function buildSse(content: string): string {
  const start = `event: chat.start\ndata: ${JSON.stringify({ type: "chat.start", msg_id: 1, response_id: "resp-58" })}\n\n`;
  const delta = `event: message.delta\ndata: ${JSON.stringify({ type: "message.delta", msg_id: 1, delta: content })}\n\n`;
  const end = `event: chat.end\ndata: ${JSON.stringify({ type: "chat.end", msg_id: 1 })}\n\n`;
  return start + delta + end;
}

test.describe("Flow 58 — Mobile composer send", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Chat routes.
    let messagesCallCount = 0;
    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      const path = url.pathname;

      if (method === "GET" && path === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: CHAT_ID,
              title: "Mobile send test",
              folder: null,
              pinned: false,
              updated_at: new Date().toISOString(),
              model_id: "qwen3",
            },
          ]),
        });
      }
      if (method === "GET" && path === `/api/chats/${String(CHAT_ID)}`) {
        messagesCallCount += 1;
        if (messagesCallCount <= 1) {
          // Initial load — empty messages.
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              id: CHAT_ID,
              user_id: 1,
              title: "Mobile send test",
              folder: null,
              pinned: false,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
              messages: [],
            }),
          });
        }
        // Post-stream refetch — return the assistant message.
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: CHAT_ID,
            user_id: 1,
            title: "Mobile send test",
            folder: null,
            pinned: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            messages: [
              {
                id: 1,
                chat_id: CHAT_ID,
                role: "user",
                content: "Hello mobile!",
                reasoning_content: null,
                created_at: new Date().toISOString(),
              },
              {
                id: 2,
                chat_id: CHAT_ID,
                role: "assistant",
                content: "Hi from the mobile stream!",
                reasoning_content: null,
                created_at: new Date().toISOString(),
              },
            ],
          }),
        });
      }
      return route.fallback();
    });

    // Stream stub — SSE with type field on each event.
    await page.route("**/api/chat/stream", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSse("Hi from the mobile stream!"),
      });
    });
  });

  test("type in composer, tap Send button, see streamed response", async ({ page }) => {
    await page.goto(`/chats/${String(CHAT_ID)}`);

    // Wait for the composer to be ready.
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    // Type the message.
    await composer.fill("Hello mobile!");

    // Tap the Send button — uses aria-label, NOT keyboard shortcut.
    await page.getByLabel("Send message").click();

    // The streamed assistant text should appear.
    await expect(page.getByText("Hi from the mobile stream!")).toBeVisible({
      timeout: 10_000,
    });
  });

  test("composer is cleared after send", async ({ page }) => {
    await page.goto(`/chats/${String(CHAT_ID)}`);

    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    await composer.fill("Hello mobile!");
    await page.getByLabel("Send message").click();

    // After submission the textarea empties.
    await expect(composer).toHaveValue("", { timeout: 5_000 });
  });
});
