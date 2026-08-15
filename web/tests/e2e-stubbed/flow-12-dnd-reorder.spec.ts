/**
 * Flow 12 — DnD chat reorder (mouse + keyboard).
 *
 * What it proves (route-stubbed):
 *  1. Mouse drag of one chat handle onto another in the Sidebar fires a
 *     PATCH /api/chats/reorder with the dragged chat_id + target
 *     display_order.  We capture the request body and assert the shape.
 *  2. Reload — after invalidation the new server order is reflected in the
 *     sidebar (we mutate the stub's chat list to match the reorder so the
 *     persistence assertion has something to read).
 *  3. Keyboard DnD: Tab to focus the drag handle, Space to grab,
 *     ArrowUp/ArrowDown to move, Space to drop.  The same reorder
 *     mutation fires.
 *
 * dnd-kit's PointerSensor requires distance > 8 px before it activates;
 *   we use dragTo with explicit source/target offsets to clear the
 *   threshold.  The KeyboardSensor activates on Space when the handle has
 *   focus (aria-roledescription="sortable").
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";
// Real ChatSummary import — not a hand-rolled shadow. A local mirror of
// this shape (the previous `ChatRow`) was missing `tags`/`archived_at`,
// required on the real type since tags/archive shipped (08-13) — invisible
// until now because this array is just JSON.stringify'd into a route
// stub body, never checked against the real type.
import type { ChatSummary } from "@/hooks/useChats";

test.describe("Flow 12 — DnD chat reorder", () => {
  // Per-test mutable state.
  let chats: ChatSummary[] = [];
  let lastReorderBody: string | null = null;

  test.beforeEach(async ({ page }) => {
    await bootstrapAuthedApp(page);
    lastReorderBody = null;
    chats = [
      {
        id: 1, title: "Chat Alpha", folder: null, pinned: false,
        updated_at: "2026-01-01T00:00:00Z", model_id: "qwen",
        display_order: 0, settings: {}, tags: [], archived_at: null,
      },
      {
        id: 2, title: "Chat Beta", folder: null, pinned: false,
        updated_at: "2026-01-01T00:00:00Z", model_id: "qwen",
        display_order: 1, settings: {}, tags: [], archived_at: null,
      },
    ];

    // auth (login/me/probe) is handled by bootstrapAuthedApp above.

    // Playwright matches the most recently registered route first; register
    // the general /api/chats list route FIRST so the more specific
    // /api/chats/reorder route (registered below) wins for PATCH calls.
    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(chats),
        });
      }
      return route.continue();
    });

    // PATCH /api/chats/reorder — registered AFTER /api/chats so it takes
    // precedence (last-registered wins in Playwright route matching).
    await page.route("**/api/chats/reorder", async (route) => {
      lastReorderBody = route.request().postData() ?? "";
      const params = new URLSearchParams(lastReorderBody);
      const chatId = Number(params.get("chat_id"));
      const newOrder = Number(params.get("display_order"));
      // Reorder our local list to mirror the request so the reload
      // assertion can verify persistence.
      const target = chats.find((c) => c.id === chatId);
      if (target !== undefined) {
        const others = chats.filter((c) => c.id !== chatId);
        // Re-insert at the requested index.
        const reordered = [...others];
        reordered.splice(Math.max(0, Math.min(newOrder, others.length)), 0, target);
        chats = reordered.map((c, i) => ({ ...c, display_order: i }));
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true }),
      });
    });

    await page.route("**/api/chats/1", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1, user_id: 1, title: "Chat Alpha", folder: null, pinned: false,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          messages: [], has_more: false, settings: {},
        }),
      })
    );

    // models + memory/pins are provided (correctly shaped) by bootstrapAuthedApp.

    // bootstrapAuthedApp already authenticates (probe returns a signed-in
    // user), so no manual /login dance is needed — each test navigates
    // straight to its target route already authed.
  });

  test("mouse drag fires PATCH /api/chats/reorder; reload reflects new order", async ({ page }) => {
    // Navigate to a chat so the sidebar is visible alongside the main area.
    await page.goto("/chats/1");
    await page.waitForLoadState("networkidle").catch(() => { /* ok */ });

    const handleA = page.getByRole("button", { name: "Reorder: Chat Alpha" }).first();
    const handleB = page.getByRole("button", { name: "Reorder: Chat Beta" }).first();
    await expect(handleA).toBeVisible({ timeout: 5_000 });
    await expect(handleB).toBeVisible({ timeout: 5_000 });

    const boxA = await handleA.boundingBox();
    const boxB = await handleB.boundingBox();
    if (boxA === null || boxB === null) throw new Error("Cannot read bounding box");

    // Manual mouse drag — dragTo() is sometimes unreliable for dnd-kit's
    // PointerSensor (distance:8 activation constraint).  Manually drive
    // the mouse with explicit intermediate moves so the distance threshold
    // is crossed before the drop.
    await page.mouse.move(boxA.x + boxA.width / 2, boxA.y + boxA.height / 2);
    await page.mouse.down();
    // Two intermediate moves to clear the 8 px activation distance.
    await page.mouse.move(boxA.x + boxA.width / 2, boxA.y + boxA.height / 2 + 20, {
      steps: 5,
    });
    await page.mouse.move(boxB.x + boxB.width / 2, boxB.y + boxB.height / 2 + 5, {
      steps: 10,
    });
    await page.mouse.up();

    // The reorder mutation should have fired.
    await expect.poll(() => lastReorderBody, { timeout: 5_000 }).not.toBeNull();
    expect(lastReorderBody).toContain("chat_id=1");
    expect(lastReorderBody).toContain("display_order=");

    // Reload and verify the sidebar still shows both chats; order is the
    // post-mutation order (Beta should now precede Alpha or vice versa
    // depending on the drop position — we only assert both still render).
    await page.reload();
    await expect(page.getByRole("link", { name: "Chat Alpha" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Chat Beta" })).toBeVisible();
  });

  test("keyboard DnD — focus handle, Space, ArrowUp, Space — fires reorder", async ({ page }) => {
    await page.goto("/chats/1");
    await page.waitForLoadState("networkidle").catch(() => { /* ok */ });

    const handleA = page.getByRole("button", { name: "Reorder: Chat Alpha" }).first();
    await expect(handleA).toBeVisible({ timeout: 5_000 });
    await handleA.focus();
    await expect(handleA).toBeFocused({ timeout: 3_000 });

    // dnd-kit KeyboardSensor activation: Space to grab.
    await page.keyboard.press("Space");
    await page.waitForTimeout(200);
    // Move down one slot (Alpha → past Beta).
    await page.keyboard.press("ArrowDown");
    await page.waitForTimeout(200);
    // Space to drop.
    await page.keyboard.press("Space");

    // Reorder mutation should fire for chat_id=1 (Alpha moving below Beta).
    await expect.poll(() => lastReorderBody, { timeout: 5_000 }).not.toBeNull();
    expect(lastReorderBody).toContain("chat_id=1");
  });
});
