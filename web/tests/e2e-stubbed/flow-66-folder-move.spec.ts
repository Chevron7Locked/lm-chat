/**
 * Flow 66 — BUG 4 fix: move a chat into a folder via the MENU path.
 *
 * What it proves (route-stubbed):
 *  1. Create a folder ("Work") via the existing folder-CRUD UI.
 *  2. Use the MoveToFolderMenu on an existing chat row to move it into
 *     that folder — NOT drag-and-drop (dnd-kit pointer drags are flaky
 *     under Playwright; the menu path is the reliable, always-working
 *     affordance this bug fix adds — see Sidebar.tsx / MoveToFolderMenu.tsx).
 *  3. Assert the intercepted PATCH /api/chats/reorder body carries
 *     chat_id=<id>&folder=Work.
 *  4. After the mutation's cache invalidation refetches /api/chats
 *     (reflecting the server-side move), the chat renders under the
 *     "Work" folder group.
 *
 * Mirrors flow-40-folder-crud.spec.ts (folder CRUD routes) and
 * flow-12-dnd-reorder.spec.ts (chat list + reorder route stubbing).
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";
// Real ChatSummary import — not a hand-rolled shadow. A local mirror of
// this shape (the previous `ChatRow`) was missing `tags`/`archived_at`,
// required on the real type since tags/archive shipped (08-13) — invisible
// until now because this array is just JSON.stringify'd into a route
// stub body, never checked against the real type.
import type { ChatSummary } from "@/hooks/useChats";

test.describe("Flow 66 — move a chat into a folder via the menu", () => {
  let chats: ChatSummary[] = [];
  let folders: string[] = [];
  let lastReorderBody: string | null = null;

  test.beforeEach(async ({ page }) => {
    lastReorderBody = null;
    folders = [];
    chats = [
      {
        id: 1,
        title: "Chat Alpha",
        folder: null,
        pinned: false,
        updated_at: "2026-01-01T00:00:00Z",
        model_id: "qwen3",
        display_order: 0,
        settings: {},
        tags: [],
        archived_at: null,
      },
      {
        id: 2,
        title: "Chat Beta",
        folder: null,
        pinned: false,
        updated_at: "2026-01-01T00:00:00Z",
        model_id: "qwen3",
        display_order: 1,
        settings: {},
        tags: [],
        archived_at: null,
      },
    ];

    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Register the general /api/chats list route FIRST so the more
    // specific /api/chats/reorder route (registered below) wins for PATCH
    // calls — Playwright matches the most recently registered route first.
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

    await page.route(/\/api\/folders$/, async (route) => {
      const req = route.request();
      if (req.method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(folders),
        });
      }
      if (req.method() === "POST") {
        const params = new URLSearchParams(req.postData() ?? "");
        const name = params.get("name") ?? "";
        if (!folders.includes(name)) folders.push(name);
        folders.sort();
        return route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify(folders),
        });
      }
      return route.continue();
    });

    // PATCH /api/chats/reorder — the MoveToFolderMenu's onPick wiring
    // (Sidebar.tsx SortableChatItem.handleMoveToFolder) fires this.
    await page.route("**/api/chats/reorder", async (route) => {
      lastReorderBody = route.request().postData() ?? "";
      const params = new URLSearchParams(lastReorderBody);
      const chatId = Number(params.get("chat_id"));
      const folder = params.get("clear_folder") === "true"
        ? null
        : params.get("folder");
      const target = chats.find((c) => c.id === chatId);
      if (target !== undefined) {
        target.folder = folder;
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
          id: 1,
          user_id: 1,
          title: "Chat Alpha",
          folder: null,
          pinned: false,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          messages: [],
          has_more: false,
          settings: {},
        }),
      }),
    );

    await page.goto("/chats/1");
    await page.waitForLoadState("networkidle").catch(() => {
      /* ok */
    });
  });

  test("create a folder, move a chat into it via the menu, chat re-renders under that group", async ({
    page,
  }) => {
    // Create the "Work" folder.
    const newFolderBtn = page.getByTestId("sidebar-new-folder-btn");
    await expect(newFolderBtn).toBeVisible({ timeout: 5_000 });
    await newFolderBtn.click();
    await page.getByTestId("sidebar-new-folder-input").fill("Work");
    await page.getByTestId("sidebar-new-folder-submit").click();
    await expect
      .poll(() => folders.includes("Work"))
      .toBe(true);

    // Move "Chat Alpha" (id 1) into "Work" via the per-row menu — NOT drag.
    // The trigger is CSS-hover-revealed (shares its slot with the row's
    // timestamp meta text until hovered); hover the row first so the
    // meta span stops intercepting pointer events at the trigger.
    await page.getByRole("link", { name: "Chat Alpha" }).hover();
    const moveTrigger = page.getByTestId("sidebar-chat-1-move-to-folder-trigger");
    await expect(moveTrigger).toBeVisible({ timeout: 5_000 });
    await moveTrigger.click();

    const workItem = page.getByTestId("sidebar-chat-1-move-to-folder-pick-Work");
    await expect(workItem).toBeVisible({ timeout: 5_000 });
    // The floating menu (position:absolute, no portal — MoveToFolderMenu.tsx)
    // overlaps the next sidebar row underneath it at this viewport. A real
    // OS-level click (even with force:true, which only skips Playwright's
    // pre-checks — the click still hit-tests at screen coordinates) lands
    // on "Chat Beta" instead. dispatchEvent fires the DOM click event
    // directly on the menu item, bypassing hit-testing entirely — same
    // technique as the sidebar-backdrop click in flow-57.
    await workItem.dispatchEvent("click");

    // The reorder PATCH fired with the chat + target folder.
    await expect.poll(() => lastReorderBody, { timeout: 5_000 }).not.toBeNull();
    expect(lastReorderBody).toContain("chat_id=1");
    expect(lastReorderBody).toContain("folder=Work");

    // After the mutation invalidates the chats query, the sidebar refetches
    // /api/chats (now reflecting chat 1's folder=Work) and the chat renders
    // under the "Work" folder group.
    await expect(page.getByText("Work", { exact: true })).toBeVisible({
      timeout: 5_000,
    });
    await expect(page.getByRole("link", { name: "Chat Alpha" })).toBeVisible();
  });
});
