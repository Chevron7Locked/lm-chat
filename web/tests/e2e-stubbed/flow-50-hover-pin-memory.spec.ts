/**
 * Flow 50 — Hover-bar message-level pin.
 *
 * What it proves (route-stubbed):
 *   1. Hover an assistant message → action bar appears.
 *   2. Pin button in action bar fires a pin action.
 *   3. Pinned item appears in the pin-nav strip.
 *
 * NOTE: The current ChatMessage does not have a pin button in its hover
 * action bar. Pinning is managed exclusively via the client-side
 * usePinnedMessagesStore (localStorage). This spec is fixme'd until a pin
 * button is added to the message hover action bar.
 */
import { test, expect } from "@playwright/test";

const AUTH_ME = { user_id: 1, username: "alice", is_admin: false, expires_at: "2026-12-01T00:00:00Z" };

test.describe("Flow 50 — Hover-bar message-level pin", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/auth/login", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(AUTH_ME) })
    );
    await page.route("**/api/auth/me/probe", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...AUTH_ME, needs_setup: false, totp_enabled: false, email: null, display_name: null, avatar_url: null }) })
    );
    await page.route("**/api/auth/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(AUTH_ME) })
    );
    await page.route("**/api/models", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/memory/pins", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/chats", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      if (method === "GET" && url.pathname === "/api/chats") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
          { id: 50, user_id: 1, title: "Pin test chat", folder: null, pinned: false, created_at: "2026-06-01T12:00:00Z", updated_at: "2026-06-01T12:00:00Z", settings: {}, display_order: 0, incognito: false, incognito_expires_at: null, model_id: null },
        ]) });
      }
      if (method === "GET" && url.pathname === "/api/chats/50") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          id: 50, user_id: 1, title: "Pin test chat", messages: [
            { id: 501, chat_id: 50, role: "user", content: "Hello", reasoning_content: null, created_at: "2026-06-01T12:00:01Z", state: "final", response_id: null, model_id: null },
            { id: 502, chat_id: 50, role: "assistant", content: "Hi there! How can I help?", reasoning_content: null, created_at: "2026-06-01T12:00:02Z", state: "final", response_id: null, model_id: null },
          ], has_more: false,
        }) });
      }
      return route.continue();
    });
  });

  test("hover assistant message reveals action bar with pin option", async ({ page }) => {
    // The ChatMessage component does not have a pin button in its hover
    // action bar. This feature requires a product-code change to add
    // a pin button to the lmchat-message-actions div.
    test.fixme(true, "ChatMessage hover action bar lacks a pin button — needs product-code addition");
  });
});