/**
 * Flow 36 — P13j chat export + share + incognito-blocks-share.
 *
 * What it proves (route-stubbed):
 *   1. The chat header exposes a menu button. Clicking it reveals
 *      Markdown + JSON export options and a Share section.
 *   2. Clicking "Markdown (.md)" triggers a Blob download. We replace
 *      URL.createObjectURL so the test can assert without actually
 *      writing to disk.
 *   3. Clicking "Create public link" POSTs /api/chats/{id}/share, then
 *      a share URL chip appears with copy / stop actions.
 *   4. INCOGNITO INVARIANT (R-15): on an incognito chat, the Share
 *      action renders disabled with a tooltip; clicking it does NOT
 *      send a POST.
 *   5. Cmd/Ctrl+Shift+E opens the menu without clicking the trigger.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 36 — P13j chat export + share", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);
  });

  test("export menu downloads Markdown and shares via POST", async ({ page }) => {
    const regularChat = {
      id: 1,
      user_id: 1,
      title: "Public worthy chat",
      folder: null,
      pinned: false,
      created_at: "2026-05-22T12:00:00Z",
      updated_at: "2026-05-22T12:00:00Z",
      settings: {},
      display_order: 0,
      incognito: false,
      incognito_expires_at: null,
      model_id: null,
    };

    let shareCreated = false;

    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());

      if (method === "GET" && url.pathname === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([regularChat]),
        });
      }
      if (method === "GET" && url.pathname === "/api/chats/1") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ...regularChat,
            messages: [
              {
                id: 11,
                chat_id: 1,
                role: "user",
                content: "Hello",
                reasoning_content: null,
                created_at: "2026-05-22T12:00:01Z",
                state: "final",
                response_id: null,
                model_id: null,
              },
              {
                id: 12,
                chat_id: 1,
                role: "assistant",
                content: "Hi there!",
                reasoning_content: "thinking...",
                created_at: "2026-05-22T12:00:02Z",
                state: "final",
                response_id: null,
                model_id: null,
              },
            ],
            has_more: false,
          }),
        });
      }

      // GET /api/chats/1/share returns null pre-share, then a token after.
      if (method === "GET" && url.pathname === "/api/chats/1/share") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: shareCreated
            ? JSON.stringify({
                token: "TOKENXYZ",
                url: "/share/TOKENXYZ",
                chat_id: 1,
                created_at: "2026-05-22T12:00:03Z",
              })
            : "null",
        });
      }
      if (method === "POST" && url.pathname === "/api/chats/1/share") {
        shareCreated = true;
        return route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            token: "TOKENXYZ",
            url: "/share/TOKENXYZ",
            chat_id: 1,
            created_at: "2026-05-22T12:00:03Z",
          }),
        });
      }
      if (method === "DELETE" && url.pathname === "/api/chats/1/share") {
        shareCreated = false;
        return route.fulfill({ status: 204, body: "" });
      }

      return route.continue();
    });

    // Replace URL.createObjectURL so download trigger can be observed.
    await page.addInitScript(() => {
      (window as unknown as { __exportCalled?: number }).__exportCalled = 0;
      const orig = URL.createObjectURL;
      URL.createObjectURL = function (blob: Blob): string {
        (window as unknown as { __exportCalled: number }).__exportCalled += 1;
        return orig.call(URL, blob);
      };
    });

    await page.goto("/chats/1");

    // The "chat-header-menu-button" testid marks a HIDDEN trigger — it only
    // exists as the target of the Cmd+Shift+E signal (see TopBar.tsx's
    // "opens the hidden-trigger ChatHeaderMenu" comment). The real,
    // visible way to open the menu is the ⋯ overflow's "Share / Export"
    // menuitem, which fires the same signal.
    async function openChatHeaderMenu() {
      await page.getByTestId("topbar-overflow-trigger").click();
      await page.getByRole("menuitem", { name: "Share / Export" }).click();
    }

    await openChatHeaderMenu();
    await expect(page.getByTestId("chat-header-menu-panel")).toBeVisible();

    // Click Markdown export — download trigger should fire (via createObjectURL).
    await page.getByTestId("chat-export-markdown").click();
    await expect.poll(async () =>
      page.evaluate(() => (window as unknown as { __exportCalled: number }).__exportCalled)
    ).toBeGreaterThanOrEqual(1);

    // Re-open and create a share link.
    await openChatHeaderMenu();
    await page.getByTestId("chat-share-create").click();
    // Once POST resolves the share-url chip appears.
    await expect(page.getByTestId("chat-share-url")).toContainText("/share/TOKENXYZ");
    // The "Stop sharing" button is now wired up.
    await expect(page.getByTestId("chat-share-stop")).toBeVisible();
  });

  test("Cmd+Shift+E opens the export menu without clicking the trigger", async ({ page, browserName }) => {
    test.skip(browserName === "webkit", "WebKit treats Meta+Shift+E inconsistently");
    const regularChat = {
      id: 1,
      user_id: 1,
      title: "Keyboard chat",
      folder: null,
      pinned: false,
      created_at: "2026-05-22T12:00:00Z",
      updated_at: "2026-05-22T12:00:00Z",
      settings: {},
      display_order: 0,
      incognito: false,
      incognito_expires_at: null,
      model_id: null,
    };

    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      if (method === "GET" && url.pathname === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([regularChat]),
        });
      }
      if (method === "GET" && url.pathname === "/api/chats/1") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ...regularChat, messages: [], has_more: false }),
        });
      }
      if (method === "GET" && url.pathname === "/api/chats/1/share") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "null",
        });
      }
      return route.continue();
    });

    await page.goto("/chats/1");

    // Wait for the chat surface to be ready (TopBar mounted).
    await expect(page.getByTestId("chat-header-menu-button")).toBeVisible();

    // Press Cmd/Ctrl + Shift + E.
    const mod = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${mod}+Shift+E`);
    await expect(page.getByTestId("chat-header-menu-panel")).toBeVisible();
  });

  test("incognito chats disable Share with a tooltip (R-15 invariant)", async ({ page }) => {
    const incognitoChat = {
      id: 9,
      user_id: 1,
      title: "Private chat",
      folder: null,
      pinned: false,
      created_at: "2026-05-22T12:00:00Z",
      updated_at: "2026-05-22T12:00:00Z",
      settings: {},
      display_order: 0,
      incognito: true,
      incognito_expires_at: 9_999_999_999,
      model_id: null,
    };

    let postWasCalled = false;

    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      if (method === "GET" && url.pathname === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([incognitoChat]),
        });
      }
      if (method === "GET" && url.pathname === "/api/chats/9") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ...incognitoChat, messages: [], has_more: false }),
        });
      }
      if (method === "GET" && url.pathname === "/api/chats/9/share") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "null",
        });
      }
      if (method === "POST" && url.pathname === "/api/chats/9/share") {
        postWasCalled = true;
        return route.fulfill({
          status: 403,
          contentType: "application/json",
          body: JSON.stringify({ detail: "incognito chats cannot be shared" }),
        });
      }
      return route.continue();
    });

    await page.goto("/chats/9");

    // Open the menu — via the visible ⋯ overflow's "Share / Export"
    // menuitem, not the hidden signal-only trigger (see the other test).
    await page.getByTestId("topbar-overflow-trigger").click();
    await page.getByRole("menuitem", { name: "Share / Export" }).click();
    // Share section shows the disabled stub.
    const disabled = page.getByTestId("chat-share-disabled-incognito");
    await expect(disabled).toBeVisible();
    await expect(disabled).toBeDisabled();
    // Clicking the disabled button must NOT issue a POST.
    await disabled.click({ force: true }).catch(() => { /* expected for disabled */ });
    // Give any in-flight request a beat to land.
    await page.waitForTimeout(120);
    expect(postWasCalled).toBe(false);
  });
});
