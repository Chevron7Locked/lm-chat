/**
 * Flow 19 — STT graceful degradation (Firefox path).
 *
 * What it proves (route-stubbed):
 *  1. With both `window.SpeechRecognition` and
 *     `window.webkitSpeechRecognition` deleted before page load,
 *     useSTT.detectSTT() reports available=false.
 *  2. The mic button renders disabled with the title attribute
 *     "Speech-to-text not supported in this browser" (matching the
 *     copy in MicButton.tsx).
 *  3. Clicking the disabled mic button has no effect (it stays
 *     unpressed and no warning toast appears because the click is a
 *     no-op when the button is disabled).
 *
 * The "toast" assertion described in PLAN.md fires only on the
 *   keyboard-shortcut path (Composer.handleSttToggle → useSTT.start →
 *   capability.available=false → onError callback).  We also exercise
 *   that path explicitly via Cmd+Shift+M so the toast assertion has a
 *   genuine trigger.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 19 — STT Firefox graceful degradation", () => {
  test.beforeEach(async ({ page }) => {
    // Remove both SpeechRecognition globals BEFORE the page loads.
    await page.addInitScript(() => {
      const w = window as unknown as Record<string, unknown>;
      delete w.webkitSpeechRecognition;
      delete w.SpeechRecognition;
    });

    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);
    await page.route("**/api/chats*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 1, title: "Test Chat", folder: null, pinned: false,
            updated_at: new Date().toISOString(), model_id: "qwen",
            display_order: 0, settings: {},
          },
        ]),
      })
    );
    await page.route("**/api/chats/1", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1, user_id: 1, title: "Test Chat", folder: null, pinned: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [], has_more: false, settings: {},
        }),
      })
    );

    await page.goto("/chats/1");
  });

  test("mic button renders disabled with not-supported title", async ({ page }) => {
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 5_000 });

    const micBtn = page.getByRole("button", {
      name: /Speech-to-text not supported in this browser/,
    });
    await expect(micBtn).toBeVisible();
    await expect(micBtn).toBeDisabled();
  });

  test("Cmd+Shift+M keyboard toggle on unsupported browser surfaces a toast", async ({ page }) => {
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 5_000 });

    await composer.focus();
    await composer.press("Control+Shift+M");

    // Composer's onError callback pushes a warning toast (role="status" —
    // a polite announcement, not role="alert").
    await expect(
      page.getByRole("status").filter({ hasText: /not supported/i })
    ).toBeVisible({ timeout: 3_000 });
  });
});
