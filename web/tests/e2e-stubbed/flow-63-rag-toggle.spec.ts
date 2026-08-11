/**
 * Flow 63 — RAG toggle in the TopBar.
 *
 * What it proves (route-stubbed):
 *   1. On a chat page the "Enable RAG for this chat" button is visible
 *      (desktop viewport; the mobile TopBar collapses it into the overflow).
 *   2. Clicking it fires PATCH /api/chats/1 with a form-encoded body
 *      containing `rag_enabled=true` (P7c contract, useChats.ts).
 *   3. After toggle the button aria-label flips to "Disable RAG for this
 *      chat", confirming the local state update.
 *
 * The RAG toggle is a `TopBarBtn` rendered in Chat.tsx ~line 2480 when
 * `onRagToggle` is defined.  The PATCH goes to `PATCH /api/chats/{id}` with
 * `Content-Type: application/x-www-form-urlencoded`.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const CHAT_ROW = {
  id: 1,
  user_id: 1,
  title: "RAG test chat",
  folder: null,
  pinned: false,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  display_order: 0,
  incognito: false,
  incognito_expires_at: null,
  model_id: "qwen3",
  settings: { rag_enabled: false },
};

test.describe("Flow 63 — RAG toggle", () => {
  test("RAG toggle button visible and PATCH fires with rag_enabled=true", async ({ page }) => {
    // Force desktop viewport so the TopBar toggle is rendered (not collapsed).
    await page.setViewportSize({ width: 1280, height: 800 });

    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Track the PATCH request body.
    let patchBody: string | null = null;
    let ragEnabled = false;

    // Chat list — scoped to the exact "/api/chats" path.  A bare
    // method-only check (no path guard) would also swallow GET
    // /api/chats/1/rag_mode and /api/chats/1/compactions, handing them the
    // list ARRAY instead of their expected object shapes — a downstream
    // component then read a field off the array as `undefined` and called
    // `.toLocaleString()` on it, white-screening the page to the
    // ErrorBoundary. Anything else falls through to bootstrap's own
    // typed defaults (including rag_mode/compactions).
    await page.route("**/api/chats*", (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const path = new URL(route.request().url()).pathname;
      if (path !== "/api/chats") return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{ ...CHAT_ROW, settings: { rag_enabled: ragEnabled } }]),
      });
    });

    // PATCH + GET /api/chats/1 — highest-priority (registered last).
    await page.route("**/api/chats/1", async (route) => {
      const method = route.request().method();
      if (method === "PATCH") {
        patchBody = route.request().postData();
        const params = new URLSearchParams(patchBody ?? "");
        if (params.has("rag_enabled")) {
          ragEnabled = params.get("rag_enabled") === "true";
        }
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ...CHAT_ROW,
            settings: { rag_enabled: ragEnabled },
          }),
        });
      }
      if (method === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ...CHAT_ROW,
            messages: [],
            has_more: false,
            settings: { rag_enabled: ragEnabled },
          }),
        });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });

    await page.goto("/chats/1");

    // Wait for the chat page to finish loading.
    await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible({ timeout: 10_000 });

    // The RAG toggle button (desktop TopBar) — aria reflects current disabled state.
    const enableBtn = page.getByRole("button", { name: "Enable RAG for this chat" });
    await expect(enableBtn).toBeVisible({ timeout: 5_000 });

    await enableBtn.click();

    // PATCH must have fired with rag_enabled=true.
    await expect.poll(() => patchBody).toMatch(/rag_enabled=true/);

    // Aria-label flips to "Disable RAG for this chat" once state updates.
    await expect(
      page.getByRole("button", { name: "Disable RAG for this chat" })
    ).toBeVisible({ timeout: 5_000 });
  });
});
