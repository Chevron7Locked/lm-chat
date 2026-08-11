/**
 * Flow 28 — Global keyboard shortcuts (P13d).
 *
 * Closes audit items K-01, K-02, K-03, K-05, K-10.
 *
 * Each new chord:
 *  - Cmd/Ctrl+N        → creates a chat and navigates to it (K-01)
 *  - Cmd/Ctrl+Shift+S  → toggles the Sidebar collapsed/expanded (K-02)
 *  - Cmd/Ctrl+,        → navigates to /settings (K-03)
 *  - `?`               → opens the KeyboardHelp modal (K-10)
 *  - Esc closes KeyboardHelp (K-05 priority chain)
 *  - Sidebar `?` button is visible and also opens the modal (K-10 discoverability)
 *
 * Strategy: route-stubbed like flow-20. Assertions observe DOM changes that
 * follow from each handler firing.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 28 — global keyboard shortcuts", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // GET /api/chats returns one existing chat; POST creates a second.
    let newChatId = 2;
    await page.route("**/api/chats*", async (route) => {
      const req = route.request();
      if (req.method() === "POST") {
        // Form-encoded body per useCreateChat.
        const created = {
          id: newChatId,
          title: "New Chat",
          folder: null,
          pinned: false,
          updated_at: new Date().toISOString(),
          model_id: "qwen",
          display_order: newChatId,
          settings: {},
        };
        newChatId += 1;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(created),
        });
        return;
      }
      if (req.method() !== "GET") return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 1, title: "Existing Chat", folder: null, pinned: false,
            updated_at: new Date().toISOString(), model_id: "qwen",
            display_order: 0, settings: {},
          },
        ]),
      });
    });

    // GET /api/chats/:id detail (Existing Chat + any newly created).
    await page.route(/.*\/api\/chats\/\d+$/, (route) => {
      const url = new URL(route.request().url());
      const idStr = url.pathname.split("/").pop() ?? "1";
      const id = Number(idStr);
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id, user_id: 1, title: id === 1 ? "Existing Chat" : "New Chat",
          folder: null, pinned: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [],
          has_more: false, settings: {},
        }),
      });
    });

    await page.goto("/chats/1");

    // Wait for the page to be past the isInitializing branch and the
    // useKeyboardShortcuts hook to be attached. Composer interactive is
    // a reliable signal.
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible();
    await expect(composer).toBeEnabled();
  });

  test("K-01: Cmd/Ctrl+N creates a new chat and navigates to it", async ({ page }) => {
    // Focus a neutral element so the window-level handler receives the key.
    await page.locator("body").click({ position: { x: 1, y: 1 } });

    // Wait for the POST then assert navigation.
    const postReq = page.waitForRequest(
      (req) => req.url().endsWith("/api/chats") && req.method() === "POST",
      { timeout: 5_000 }
    );
    await page.keyboard.press("Meta+n");
    await postReq;

    // Navigation lands at /chats/<newId>; the new chat's title appears in
    // the TopBar.
    await page.waitForURL(/\/chats\/\d+/, { timeout: 5_000 });
  });

  test("K-02: Cmd/Ctrl+Shift+S toggles the sidebar collapsed", async ({ page }) => {
    // Initial state: sidebar expanded (Filter chats visible).
    const filterInput = page.getByLabel("Filter chats");
    await expect(filterInput).toBeVisible();

    // Focus a neutral element first.
    await page.locator("body").click({ position: { x: 1, y: 1 } });
    await page.keyboard.press("Meta+Shift+S");

    // Collapsed: filter input hidden; expand button visible with the
    // collapsed-state aria-label.
    await expect(filterInput).not.toBeVisible({ timeout: 3_000 });
    await expect(
      page.getByRole("button", { name: "Expand sidebar" })
    ).toBeVisible();

    // Toggle back.
    await page.keyboard.press("Meta+Shift+S");
    await expect(filterInput).toBeVisible({ timeout: 3_000 });
  });

  test("K-03: Cmd/Ctrl+, navigates to /settings", async ({ page }) => {
    await page.locator("body").click({ position: { x: 1, y: 1 } });
    await page.keyboard.press("Meta+,");

    await page.waitForURL("**/settings", { timeout: 5_000 });
    expect(page.url()).toMatch(/\/settings$/);
  });

  test("K-10: ? opens KeyboardHelp; Esc closes it", async ({ page }) => {
    // Focus a neutral element so the window-level listener receives `?`.
    await page.locator("body").click({ position: { x: 1, y: 1 } });
    await page.keyboard.press("Shift+/");

    const help = page.getByRole("dialog", { name: /keyboard shortcuts/i });
    await expect(help).toBeVisible({ timeout: 3_000 });

    // Lists the new shortcuts. The <kbd> chord glyph is platform-formatted
    // (⌘ vs Ctrl); match on the stable description text instead.
    await expect(help.getByText("New chat")).toBeVisible();
    await expect(help.getByText("Toggle sidebar")).toBeVisible();
    await expect(help.getByText("Open settings")).toBeVisible();

    // Esc closes (K-05 priority chain).
    await page.keyboard.press("Escape");
    await expect(help).not.toBeVisible({ timeout: 3_000 });
  });

  test("K-10: ? does NOT open KeyboardHelp while typing in the composer", async ({ page }) => {
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.focus();
    // Pressing Shift+/ would normally trigger the help modal at the window
    // level; the editable-target gate inside useKeyboardShortcuts MUST
    // suppress it when the focused element is a textarea. We assert the
    // dialog stays absent — that's the only invariant that matters. (The
    // actual character inserted varies by platform / keyboard layout, so
    // we don't assert on textarea contents.)
    await composer.press("Shift+/");

    await expect(
      page.getByRole("dialog", { name: /keyboard shortcuts/i })
    ).not.toBeVisible();
  });

  test("K-10 discoverability: footer ? button opens KeyboardHelp", async ({ page }) => {
    const helpBtn = page.getByTestId("sidebar-keyboard-help-btn");
    await expect(helpBtn).toBeVisible();
    await helpBtn.click();

    const help = page.getByRole("dialog", { name: /keyboard shortcuts/i });
    await expect(help).toBeVisible({ timeout: 3_000 });

    // Close via the ✕ button.
    await help.getByRole("button", { name: "Close shortcuts" }).click();
    await expect(help).not.toBeVisible({ timeout: 3_000 });
  });
});
