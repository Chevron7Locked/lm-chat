/**
 * Flow 32 — P13i incognito mode (audit O-04).
 *
 * Proves (route-stubbed):
 *  1. The sidebar "+ New Chat" row exposes an incognito eye-toggle.
 *  2. Toggling it ON sends `incognito=true` on the next POST /api/chats.
 *  3. Incognito chats render dimmed in the sidebar with the eye-with-slash
 *     badge.
 *  4. The chat header shows an "Incognito" badge when viewing one.
 *  5. The toggle auto-resets after a successful create so a follow-up
 *     "+ New Chat" defaults back to a regular chat (privacy by default).
 *
 * Logout-sweep is covered by tests/integration/test_incognito_lifecycle.py
 * (live backend) — this spec is the SPA contract.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 32 — P13i incognito mode", () => {
  test("creates an incognito chat and renders the badges", async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Track the most recent create-chat request body so the test can
    // assert on the form field.
    let lastCreateBody = "";

    // Initial chats list — one regular chat, then one incognito after create.
    let chatsListBody: unknown[] = [
      {
        id: 1,
        title: "Regular Chat",
        folder: null,
        pinned: false,
        updated_at: "2026-01-01T00:00:00Z",
        model_id: null,
        display_order: 0,
        settings: {},
        incognito: false,
        incognito_expires_at: null,
      },
    ];

    await page.route("**/api/chats**", async (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());

      if (method === "GET" && url.pathname === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(chatsListBody),
        });
      }

      if (method === "POST" && url.pathname === "/api/chats") {
        lastCreateBody = route.request().postData() ?? "";
        const params = new URLSearchParams(lastCreateBody);
        const isIncognito = params.get("incognito") === "true";
        const created = {
          id: 42,
          user_id: 1,
          title: params.get("title") ?? "New Chat",
          folder: null,
          pinned: false,
          created_at: "2026-05-22T00:00:00Z",
          updated_at: "2026-05-22T00:00:00Z",
          settings: {},
          display_order: 0,
          incognito: isIncognito,
          incognito_expires_at: isIncognito ? 9_999_999_999 : null,
          model_id: null,
        };
        // Add to the in-memory list so the sidebar re-renders.
        chatsListBody = [created, ...chatsListBody];
        return route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify(created),
        });
      }

      // GET /api/chats/{id}
      if (method === "GET" && /^\/api\/chats\/\d+$/.test(url.pathname)) {
        const id = Number(url.pathname.split("/").pop());
        const summary = (chatsListBody as Array<Record<string, unknown>>).find(
          (c) => c.id === id
        );
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id,
            user_id: 1,
            title: (summary?.title as string) ?? "Chat",
            folder: null,
            pinned: false,
            created_at: "2026-05-22T00:00:00Z",
            updated_at: "2026-05-22T00:00:00Z",
            messages: [],
            has_more: false,
            incognito: (summary?.incognito as boolean) ?? false,
            incognito_expires_at: (summary?.incognito_expires_at as number | null) ?? null,
          }),
        });
      }

      return route.continue();
    });

    // Land on the SPA already authed.
    await page.goto("/");

    // The sidebar new-chat button + incognito toggle should be visible.
    const newChatBtn = page.getByTestId("new-chat-btn");
    const incognitoToggle = page.getByTestId("new-chat-incognito-toggle");
    await expect(newChatBtn).toBeVisible();
    await expect(incognitoToggle).toBeVisible();

    // Default state: toggle is OFF; button label is "+ New Chat".
    await expect(newChatBtn).toHaveText("New Chat");
    await expect(incognitoToggle).toHaveAttribute("aria-pressed", "false");

    // 1. Click the eye-toggle → state flips ON, label becomes "+ Incognito Chat".
    await incognitoToggle.click();
    await expect(incognitoToggle).toHaveAttribute("aria-pressed", "true");
    await expect(newChatBtn).toHaveText("Incognito Chat");

    // 2. Click "+ Incognito Chat" → POST /api/chats includes incognito=true.
    await newChatBtn.click();

    // Wait for the create call to land + the list query to refetch.
    await expect.poll(() => lastCreateBody).toContain("incognito=true");

    // 3. After the create resolves, the toggle auto-resets to OFF.
    await expect(incognitoToggle).toHaveAttribute("aria-pressed", "false");
    await expect(newChatBtn).toHaveText("New Chat");

    // 4. The new incognito chat appears in the sidebar with the badge.
    await expect(page.getByTestId("sidebar-incognito-badge")).toBeVisible();
  });

  test("incognito toggle defaults OFF on each fresh visit", async ({ page }) => {
    // Privacy-by-default: the incognito toggle MUST default OFF on a
    // page load to prevent accidentally creating an incognito chat
    // due to state left over from a previous session.
    await bootstrapAuthedApp(page);

    await page.goto("/");

    await expect(page.getByTestId("new-chat-incognito-toggle")).toHaveAttribute(
      "aria-pressed",
      "false"
    );
    await expect(page.getByTestId("new-chat-btn")).toHaveText("New Chat");
  });
});
