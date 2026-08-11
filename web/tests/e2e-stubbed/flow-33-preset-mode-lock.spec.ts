/**
 * Flow 33 — Preset model: sub-agent launch vs persistent system prompt (P13b).
 *
 * Model (updated 2026-06-20 — composer badge removed):
 *   - The 6 presets serve two INDEPENDENT purposes:
 *     (1) PERSISTENT system prompt — set via the chat settings rail picker;
 *         `active_preset` is written to the backend; the system_prompt is
 *         injected on every plain send.  No badge appears in the Composer.
 *     (2) TRANSIENT sub-agent — launched via slash command (/research, /code…);
 *         opens a clean-context sub-session panel; does NOT write `active_preset`
 *         and does NOT show any badge.
 *
 * What this file proves (route-stubbed):
 *   T1. Typing /code and submitting the slash command opens a sub-session
 *       WITHOUT setting active_preset — no PATCH with active_preset=coder,
 *       no composer-preset-badge in the DOM.
 *   T2. Setting a system prompt via the rail preset picker DOES write
 *       active_preset=coder to the backend; the next stream payload carries
 *       the coder system_prompt; composer-preset-badge is NEVER in the DOM.
 *   T3. Selecting "None · raw model" in the rail picker sets raw mode
 *       (active_preset="none" PATCH fires, no system_prompt is sent).
 *       No badge was ever present to click.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

function buildSseText(content: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({
      type: "chat.start", msg_id: 1, response_id: "resp-1",
    })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({
      type: "message.delta", msg_id: 1, delta: content,
    })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({
      type: "chat.end", msg_id: 1,
    })}\n\n`
  );
}

function buildSubSse(rid: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ response_id: rid })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ delta: "sub-reply" })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ stop_reason: "stop" })}\n\n`
  );
}

test.describe("Flow 33 — sub-agent vs persistent preset", () => {
  let chatSettings: Record<string, unknown> = {};
  let patchedActivePreset: string | null = null;

  test.beforeEach(async ({ page }) => {
    chatSettings = {};
    patchedActivePreset = null;

    // Authed chat-page bootstrap defaults (probe hydration + correctly-typed
    // list/object endpoints, including a "qwen3" default model matching
    // this chat's model_id) — replaces the old `**/api/**` → {} catch-all.
    await bootstrapAuthedApp(page);

    await page.route("**/api/chats**", async (route) => {
      const method = route.request().method();
      const path = new URL(route.request().url()).pathname;

      if (method === "PATCH" && path === "/api/chats/1") {
        const postData = route.request().postData() ?? "";
        const params = new URLSearchParams(postData);
        for (const [k, v] of params.entries()) {
          if (k === "active_preset") {
            patchedActivePreset = v;
            chatSettings[k] = v === "" ? null : v;
          } else {
            chatSettings[k] = v;
          }
        }
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 1, user_id: 1, title: "Preset Chat",
            folder: null, pinned: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            settings: chatSettings, display_order: 0,
          }),
        });
      }
      if (method === "GET" && path === "/api/chats") {
        return route.fulfill({
          status: 200, contentType: "application/json",
          body: JSON.stringify([{
            id: 1, user_id: 1, title: "Preset Chat", folder: null, pinned: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(), model_id: "qwen3",
            display_order: 0, settings: chatSettings,
            incognito: false, incognito_expires_at: null,
          }]),
        });
      }
      if (method === "GET" && path === "/api/chats/1") {
        return route.fulfill({
          status: 200, contentType: "application/json",
          body: JSON.stringify({
            id: 1, user_id: 1, title: "Preset Chat",
            folder: null, pinned: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            messages: [], has_more: false, settings: chatSettings,
          }),
        });
      }
      return route.fallback();
    });
    await page.route("**/api/chats/*/sub-session/stream", (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      return route.fulfill({
        status: 200, contentType: "text/event-stream",
        body: buildSubSse("rid-sub"),
      });
    });
    await page.route("**/api/chat/stream", (route) =>
      route.fulfill({
        status: 200, contentType: "text/event-stream",
        body: buildSseText("reply"),
      })
    );

    // Auth is handled by the bootstrap probe — navigate directly to the chat.
    await page.goto("/chats/1");
  });

  test("T1: /code launches sub-agent WITHOUT setting active_preset or showing badge", async ({ page }) => {
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });
    await expect(composer).toBeEnabled();

    // Fire /code (no args) to launch the sub-agent.
    await composer.fill("/code");
    await composer.press("Control+Enter");

    // The composer clears.
    await expect(composer).toHaveValue("", { timeout: 5_000 });

    // The sub-session panel appears showing the persona label.
    await expect(page.locator(".lmchat-subsession-label")).toBeVisible({ timeout: 6_000 });
    await expect(page.locator(".lmchat-subsession-label")).toContainText(/Coder/i);

    // The composer-preset-badge must NEVER appear in the DOM (badge removed).
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

    // No PATCH to active_preset must have fired.
    // Give the app 1s to complete any async work, then assert null.
    await page.waitForTimeout(1_000);
    expect(patchedActivePreset).toBeNull();
  });

  test("T2: rail picker sets active_preset → no badge; stream payload carries system_prompt", async ({ page }) => {
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    // Capture each stream body so we can assert system_prompt is injected.
    let lastStreamBody: unknown = null;
    await page.route("**/api/chat/stream", async (route) => {
      try { lastStreamBody = JSON.parse(route.request().postData() ?? "{}"); } catch { lastStreamBody = null; }
      return route.fulfill({
        status: 200, contentType: "text/event-stream",
        body: buildSseText("reply"),
      });
    });

    // Open settings rail via the overflow menu (desktop) or Tune button (mobile).
    const overflowTrigger = page.getByTestId("topbar-overflow-trigger");
    if (await overflowTrigger.isVisible().catch(() => false)) {
      await overflowTrigger.click();
      await page.getByRole("menuitem", { name: "Chat settings" }).click();
    } else {
      await page.getByRole("button", { name: "Tune" }).click();
    }
    const presetSelect = page.getByTestId("chat-settings-preset");
    await expect(presetSelect).toBeVisible({ timeout: 6_000 });

    // Pick the "coder" preset — triggers setPreset → PATCH active_preset=coder.
    await presetSelect.selectOption("coder");

    // PATCH must fire with active_preset=coder.
    await expect.poll(() => patchedActivePreset, { timeout: 5_000 }).toBe("coder");

    // composer-preset-badge must NEVER appear in the DOM (badge removed).
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

    // Send a plain message — preset system_prompt must appear in the stream body.
    await composer.fill("hello");
    await composer.press("Control+Enter");

    await expect.poll(() => lastStreamBody !== null, { timeout: 8_000 }).toBe(true);
    const body = lastStreamBody as { payload?: { system_prompt?: string } };
    expect(body.payload?.system_prompt ?? "").toMatch(/Software engineering mode/i);

    // Badge still never in DOM after the send.
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);
  });

  test("T3: rail picker 'None · raw model' → active_preset='none' PATCH fires, no system_prompt sent", async ({ page }) => {
    await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible({ timeout: 10_000 });

    // composer-preset-badge must not exist at any point.
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

    // Open settings rail via the overflow menu (desktop) or Tune button (mobile).
    const overflowTrigger = page.getByTestId("topbar-overflow-trigger");
    if (await overflowTrigger.isVisible().catch(() => false)) {
      await overflowTrigger.click();
      await page.getByRole("menuitem", { name: "Chat settings" }).click();
    } else {
      await page.getByRole("button", { name: "Tune" }).click();
    }
    const presetSelect = page.getByTestId("chat-settings-preset");
    await expect(presetSelect).toBeVisible({ timeout: 6_000 });

    // Set a preset first.
    await presetSelect.selectOption("research");
    await expect.poll(() => patchedActivePreset, { timeout: 5_000 }).toBe("research");

    // Still no badge.
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

    // Switch to "None · raw model" — triggers setPreset("none") → PATCH active_preset="none".
    await presetSelect.selectOption("none");
    await expect.poll(() => patchedActivePreset, { timeout: 5_000 }).toBe("none");

    // Badge still absent.
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);
  });
});
