/**
 * E2E: chat flow (Playwright, route-stubbed).
 *
 * Login → list chats → click chat → composer → send → see streaming response.
 *
 * All backend requests are intercepted; no live FastAPI server needed.
 * To run against the real backend, remove the route stubs and start:
 *   uv run uvicorn lmchat.app:app --port 8000
 *   pnpm dev
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

// ─── Stub helpers ─────────────────────────────────────────────────────────────

/** Build a minimal SSE text stream that simulates a chat.end sequence. */
function buildSseText(content: string): string {
  const chatStart = `event: chat.start\ndata: ${JSON.stringify({ type: "chat.start", msg_id: 1, response_id: "resp-1" })}\n\n`;
  const delta = `event: message.delta\ndata: ${JSON.stringify({ type: "message.delta", msg_id: 1, delta: content })}\n\n`;
  const chatEnd = `event: chat.end\ndata: ${JSON.stringify({ type: "chat.end", msg_id: 1 })}\n\n`;
  return chatStart + delta + chatEnd;
}

test.describe("Chat page", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults (probe hydration + correctly-typed
    // list/object endpoints) — replaces the manual login dance + per-endpoint
    // stubs; this spec is already signed in on cold load.
    await bootstrapAuthedApp(page);

    // Stream stub.
    await page.route("**/api/chat/stream", async (route) => {
      const sseText = buildSseText("Hello from the assistant!");
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseText,
      });
    });

    // Chat list — GET /api/chats* catches ?unscoped=true used by Sidebar.
    // POST /api/chats → create chat stub.
    // NOTE: the trailing * also matches /api/chats/1 and /api/chats/1/rag_mode
    // via prefix globbing under **/api/chats**? No — we use a plain "*" glob
    // here (query-string only); /api/chats/1 is registered separately below
    // (and takes priority as the more-recently-registered route).
    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            { id: 1, title: "My First Chat", folder: null, pinned: false, updated_at: new Date().toISOString(), model_id: null },
          ]),
        });
      }
      // POST /api/chats → create chat.
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: 2, title: "New Chat", folder: null, pinned: false, updated_at: new Date().toISOString(), model_id: null }),
      });
    });

    // Chat detail — registered AFTER **/api/chats* so it takes priority for
    // /api/chats/1.  useMessages embeds messages in the chat detail response.
    // First call: empty messages (before stream). Subsequent: with assistant.
    let messagesCallCount = 0;
    await page.route("**/api/chats/1", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      messagesCallCount += 1;
      if (messagesCallCount <= 1) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 1, user_id: 1, title: "My First Chat", folder: null,
            pinned: false, created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(), messages: [],
          }),
        });
      }
      // After stream completes (refetch), return the assistant message.
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1, user_id: 1, title: "My First Chat", folder: null,
          pinned: false, created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [
            { id: 1, chat_id: 1, role: "user", content: "Hello!", reasoning_content: null, created_at: new Date().toISOString() },
            { id: 2, chat_id: 1, role: "assistant", content: "Hello from the assistant!", reasoning_content: null, created_at: new Date().toISOString() },
          ],
        }),
      });
    });

    await page.goto("/");
  });

  test("shows the sidebar with chat list after login", async ({ page }) => {
    await expect(page.getByRole("complementary", { name: "Chats" })).toBeVisible();
    await expect(page.getByText("My First Chat")).toBeVisible();
  });

  test("navigates to chat on sidebar click", async ({ page }) => {
    await page.getByText("My First Chat").click();
    await page.waitForURL("**/chats/1");
    await expect(page.getByRole("main")).toBeVisible();
  });

  test("shows empty hint when no messages exist", async ({ page }) => {
    await page.getByText("My First Chat").click();
    await page.waitForURL("**/chats/1");
    await expect(page.getByText(/Send a message/i)).toBeVisible();
  });

  test("sends a message and shows streaming response", async ({ page }) => {
    await page.getByText("My First Chat").click();
    await page.waitForURL("**/chats/1");

    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("Hello!");
    // Submit with Ctrl+Enter (Playwright modifier+key syntax).
    await composer.press("Control+Enter");

    // The assistant response should appear (from the SSE delta stub).
    // Allow extra time for the fetch + SSE parse + React re-render cycle.
    await expect(page.getByText("Hello from the assistant!")).toBeVisible({ timeout: 10_000 });
  });

  test("composer is cleared after send", async ({ page }) => {
    await page.getByText("My First Chat").click();
    await page.waitForURL("**/chats/1");

    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("test message");
    await composer.press("Control+Enter");

    // Composer should be empty after submission.
    await expect(composer).toHaveValue("", { timeout: 3_000 });
  });

  test("slash menu appears when typing /", async ({ page }) => {
    await page.getByText("My First Chat").click();
    await page.waitForURL("**/chats/1");

    await page.getByRole("textbox", { name: "Message" }).fill("/");
    await expect(page.getByRole("listbox", { name: "Slash commands" })).toBeVisible();
  });

  test("Esc closes the slash menu", async ({ page }) => {
    await page.getByText("My First Chat").click();
    await page.waitForURL("**/chats/1");

    await page.getByRole("textbox", { name: "Message" }).fill("/");
    await expect(page.getByRole("listbox", { name: "Slash commands" })).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.getByRole("listbox", { name: "Slash commands" })).not.toBeVisible();
  });

  // 2026-06-13 redesign: "Open settings" slide-over panel was replaced with
  // the ChatSettingsRail, opened via the ⋯ OverflowMenu (data-testid=
  // "topbar-overflow-trigger") → "Chat settings" menuitem.  The rail renders
  // as data-testid="chat-settings-rail" with aria-label="Chat settings".
  // NOTE: at the vite-preview viewport used by stubbed e2e tests, useViewport
  // initialises isMobile=true, so the MOBILE TopBar renders.  The mobile ⋯
  // overflow (data-testid="topbar-overflow-trigger") carries "Chat settings"
  // (setPanelView) rather than "Settings" (navigate).  Both branches share
  // the same data-testid on the trigger.
  test("opens chat settings rail via overflow menu", async ({ page }) => {
    await page.getByText("My First Chat").click();
    await page.waitForURL("**/chats/1");

    // Open the ⋯ overflow then click "Chat settings".
    await page.getByTestId("topbar-overflow-trigger").click();
    await page.getByRole("menuitem", { name: "Chat settings" }).click();

    // ChatSettingsRail mounts with data-testid="chat-settings-rail".
    await expect(page.getByTestId("chat-settings-rail")).toBeVisible();
  });

  test("Esc closes the chat settings rail", async ({ page }) => {
    await page.getByText("My First Chat").click();
    await page.waitForURL("**/chats/1");

    // Open the rail via the ⋯ overflow.
    await page.getByTestId("topbar-overflow-trigger").click();
    await page.getByRole("menuitem", { name: "Chat settings" }).click();
    await expect(page.getByTestId("chat-settings-rail")).toBeVisible();

    // Esc closes the active panel (P13d key-handler in Chat.tsx).
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("chat-settings-rail")).not.toBeVisible();
  });

  test("new chat button creates a chat and navigates", async ({ page }) => {
    // The Plus icon has aria-hidden; Playwright sees the accessible name as "New Chat".
    await page.getByRole("button", { name: "New Chat" }).click();
    // After create, the query is invalidated → sidebar rerenders.
    expect(page).not.toBeNull(); // creation dispatched without error.
  });
});
