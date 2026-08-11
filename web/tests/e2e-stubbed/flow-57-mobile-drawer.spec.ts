/**
 * Flow 57 — Mobile drawer open / close.
 *
 * Viewport: 390×844 (iPhone 14 / useViewport → isMobile=true).
 *
 * What it proves (route-stubbed):
 *   1. The hamburger button (topbar-mobile-menu) is visible on initial load.
 *   2. Tapping it opens the sidebar drawer (sidebar-shell becomes visible).
 *   3. Tapping the close button (sidebar-close-btn) hides the drawer.
 *   4. No horizontal overflow — scrollWidth ≤ innerWidth throughout.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const CHAT_ID = 57;

test.describe("Flow 57 — Mobile drawer", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Chat routes.
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
              title: "Drawer test chat",
              folder: null,
              pinned: false,
              updated_at: new Date().toISOString(),
              model_id: "qwen3",
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
            title: "Drawer test chat",
            messages: [],
            has_more: false,
            folder: null,
            pinned: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
        });
      }
      return route.fallback();
    });
  });

  test("hamburger opens drawer; close button dismisses it; no horizontal overflow", async ({ page }) => {
    await page.goto(`/chats/${String(CHAT_ID)}`);

    // Composer signals the chat shell has mounted.
    await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible({
      timeout: 10_000,
    });

    // Drawer is initially hidden on mobile — sidebar-shell is not in the DOM
    // (conditional render: !isMobile || mobileDrawerOpen || drawerClosing).
    await expect(page.getByTestId("sidebar-shell")).not.toBeVisible();

    // Hamburger is visible.
    const hamburger = page.getByTestId("topbar-mobile-menu");
    await expect(hamburger).toBeVisible();

    // No horizontal overflow before opening.
    const overflowBefore = await page.evaluate(
      () => document.body.scrollWidth <= window.innerWidth
    );
    expect(overflowBefore).toBe(true);

    // Open the drawer.
    await hamburger.click();
    await expect(page.getByTestId("sidebar-shell")).toBeVisible({ timeout: 3_000 });

    // No horizontal overflow with drawer open.
    const overflowOpen = await page.evaluate(
      () => document.body.scrollWidth <= window.innerWidth
    );
    expect(overflowOpen).toBe(true);

    // Close via the in-drawer close button.
    await page.getByTestId("sidebar-close-btn").click();
    await expect(page.getByTestId("sidebar-shell")).not.toBeVisible({ timeout: 3_000 });

    // No horizontal overflow after close.
    const overflowAfter = await page.evaluate(
      () => document.body.scrollWidth <= window.innerWidth
    );
    expect(overflowAfter).toBe(true);
  });

  test("tapping the backdrop also closes the drawer", async ({ page }) => {
    await page.goto(`/chats/${String(CHAT_ID)}`);
    await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible({
      timeout: 10_000,
    });

    // Open drawer.
    await page.getByTestId("topbar-mobile-menu").click();
    await expect(page.getByTestId("sidebar-shell")).toBeVisible({ timeout: 3_000 });

    // Click the backdrop via JS dispatchEvent — the element sits behind the
    // drawer nav in z-order, so Playwright's synthetic pointer events are
    // intercepted by the nav even with { force: true }.  dispatchEvent fires
    // the DOM click event directly on the backdrop element, which React's
    // bubbling handler picks up identically to a real tap on the exposed scrim
    // area outside the nav panel.
    await page.getByTestId("sidebar-backdrop").dispatchEvent("click");
    await expect(page.getByTestId("sidebar-shell")).not.toBeVisible({ timeout: 3_000 });
  });
});
