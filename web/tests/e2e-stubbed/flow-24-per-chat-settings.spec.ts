/**
 * Flow 24 — Per-chat settings rail (P13a).
 *
 * Closes audit gaps S-01..S-12 + B-01 (the bottom-left dead-click is
 * tested in flow-25; this flow covers the top-right TopBar Settings
 * button that mounts :class:`ChatSettingsRail`).
 *
 * What it proves (route-stubbed):
 *  1. Opening a chat and clicking the top-right "Settings" button mounts
 *     the per-chat ChatSettingsRail (replacing the previous LazySettings
 *     panel — S-10 / PP-2 fix).
 *  2. The rail seeds initial values from the chat row's settings.
 *  3. Changing the temperature fires a PATCH /api/chats/:id with the new
 *     value, and on reload the rail seeds with the persisted value.
 *
 * 2026-06-13 redesign: the top-right "Settings" button was replaced by the
 * ⋯ overflow menu's "Chat settings" menuitem (same pattern as
 * chat.spec.ts's "opens chat settings rail via overflow menu" test).
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 24 — per-chat settings rail", () => {
  // Persisted settings shared across requests in this describe block to
  // simulate the backend round-trip.
  let chatSettings: Record<string, unknown> = {};

  test.beforeEach(async ({ page }) => {
    chatSettings = { temperature: 0.4, system_prompt: "initial prompt" };

    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    await page.route("**/api/chats*", async (route) => {
      // PATCH /api/chats/:id is matched below; this route handles only the
      // bare /api/chats list endpoint.
      if (route.request().method() !== "GET") {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 1, title: "Settings chat", folder: null, pinned: false,
            updated_at: new Date().toISOString(), model_id: "qwen",
            display_order: 0, settings: chatSettings,
          },
        ]),
      });
    });
    await page.route("**/api/chats/1", async (route) => {
      if (route.request().method() === "PATCH") {
        // Parse the form-encoded patch and update chatSettings to simulate
        // persistence.
        const postData = route.request().postData() ?? "";
        const params = new URLSearchParams(postData);
        for (const [k, v] of params.entries()) {
          if (k === "temperature") chatSettings[k] = Number(v);
          else if (k === "self_consistency_enabled" || k === "chain_of_verification_enabled" || k === "stateless") {
            chatSettings[k] = v === "true";
          } else {
            chatSettings[k] = v;
          }
        }
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 1, user_id: 1, title: "Settings chat",
            folder: null, pinned: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            settings: chatSettings, display_order: 0,
          }),
        });
        return;
      }
      // GET /api/chats/1 — return messages + settings.
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1, user_id: 1, title: "Settings chat",
          folder: null, pinned: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [], has_more: false, settings: chatSettings,
        }),
      });
    });

    await page.goto("/chats/1");
  });

  async function openChatSettingsRail(page: import("@playwright/test").Page) {
    await page.getByTestId("topbar-overflow-trigger").click();
    await page.getByRole("menuitem", { name: "Chat settings" }).click();
  }

  test("clicking TopBar Settings mounts the per-chat rail", async ({ page }) => {
    await openChatSettingsRail(page);
    await expect(page.getByTestId("chat-settings-rail")).toBeVisible({ timeout: 5_000 });
  });

  test("rail seeds initial values from chat settings", async ({ page }) => {
    await openChatSettingsRail(page);
    const temp = page.getByTestId("chat-settings-temperature");
    await expect(temp).toHaveValue("0.4", { timeout: 5_000 });
    const sp = page.getByTestId("chat-settings-system-prompt");
    await expect(sp).toHaveValue("initial prompt");
  });

  test("changing temperature persists via PATCH and survives reload", async ({ page }) => {
    await openChatSettingsRail(page);
    const temp = page.getByTestId("chat-settings-temperature");
    await expect(temp).toBeVisible();

    // Set new value + blur to trigger the persist hook.
    await temp.fill("1.2");
    await temp.blur();

    // Wait for the in-memory chatSettings store to receive the PATCH.
    await expect.poll(() => chatSettings["temperature"]).toBe(1.2);

    // Reload and reopen the rail; the persisted value must come back.
    await page.reload();
    await openChatSettingsRail(page);
    await expect(page.getByTestId("chat-settings-temperature")).toHaveValue(
      "1.2", { timeout: 5_000 }
    );
  });
});
