/**
 * Flow 20 — Composer keyboard shortcuts.
 *
 * What it proves (route-stubbed):
 *  1. Cmd/Ctrl+Enter while focused in the Composer submits the message
 *     (textarea clears and POST /api/chat/stream fires).
 *  2. Cmd/Ctrl+/ opens the {@link SlashPalette} popover (Gap 4, P10c.2);
 *     Esc closes it.
 *  3. Cmd/Ctrl+Shift+M while focused in the Composer toggles the mic
 *     (calls handleSttToggle).  Without a SpeechRecognition mock we
 *     observe the unsupported-browser warning toast as proof the
 *     handler fired.
 *  4. Esc closes open overlays (the right-side Settings panel + the
 *     in-Composer slash menu when "/" is typed).
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

test.describe("Flow 20 — Composer keyboard shortcuts", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);
    await page.route("**/api/chats*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 1, title: "Kbd Chat", folder: null, pinned: false,
            updated_at: new Date().toISOString(), model_id: "qwen",
            display_order: 0, settings: {},
          },
        ]),
      })
    );
    // GET /api/chats/1 returns empty before any stream; once the stream
    // completes the assistant message is included so the in-memory delta
    // can be replaced by the persisted server message after refetch.
    let streamCompleted = false;
    await page.route("**/api/chats/1", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1, user_id: 1, title: "Kbd Chat", folder: null, pinned: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: streamCompleted
            ? [
                {
                  id: 1, chat_id: 1, role: "user", content: "hello kbd",
                  reasoning_content: null,
                  created_at: new Date().toISOString(),
                },
                {
                  id: 2, chat_id: 1, role: "assistant", content: "kbd reply",
                  reasoning_content: null,
                  created_at: new Date().toISOString(),
                },
              ]
            : [],
          has_more: false, settings: {},
        }),
      })
    );
    await page.route("**/api/chat/stream", async (route) => {
      streamCompleted = true;
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSseText("kbd reply"),
      });
    });

    // Strip SpeechRecognition so the Cmd+Shift+M test observes the
    // not-supported toast (proves the handler fired, not the SR result).
    await page.addInitScript(() => {
      const w = window as unknown as Record<string, unknown>;
      delete w.webkitSpeechRecognition;
      delete w.SpeechRecognition;
    });

    await page.goto("/chats/1");
  });

  test("Cmd/Ctrl+Enter submits the composer", async ({ page }) => {
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("hello kbd");
    await composer.press("Control+Enter");
    // After submit the composer clears.
    await expect(composer).toHaveValue("", { timeout: 5_000 });
    // The streamed delta should appear in the message list.
    await expect(page.getByText("kbd reply")).toBeVisible({ timeout: 10_000 });
  });

  test("Cmd/Ctrl+/ opens the SlashPalette popover; Esc closes it", async ({ page }) => {
    // Wait for Chat.tsx to mount its useKeyboardShortcuts listener.
    // The composer being interactive proves the page is past the
    // isInitializing branch and the hook has attached.
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible();
    await expect(composer).toBeEnabled();

    // Focus the document body before pressing — some platforms route
    // Cmd+/ to the focused input in a way that bypasses the window
    // listener.  Pressing on a neutral element guarantees the
    // window-level useKeyboardShortcuts handler receives it.
    await page.locator("body").click({ position: { x: 1, y: 1 } });
    await page.keyboard.press("Meta+/");

    const palette = page.getByRole("dialog", { name: "Slash command palette" });
    await expect(palette).toBeVisible({ timeout: 5_000 });

    // The palette lists available slash commands.
    await expect(
      palette.getByRole("listbox", { name: "Available slash commands" })
    ).toBeVisible();
    await expect(palette.getByText("/clear")).toBeVisible();
    await expect(palette.getByText("/help")).toBeVisible();

    // Esc closes the palette.
    await page.keyboard.press("Escape");
    await expect(palette).not.toBeVisible({ timeout: 3_000 });
  });

  test("Cmd/Ctrl+Shift+M fires the mic toggle (unsupported → toast)", async ({ page }) => {
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.focus();
    await composer.press("Control+Shift+M");
    // Warning toasts render with role="status" (polite announcement), not
    // role="alert".
    await expect(
      page.getByRole("status").filter({ hasText: /not supported/i })
    ).toBeVisible({ timeout: 3_000 });
  });

  test("Esc closes the slash menu", async ({ page }) => {
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("/");
    await expect(
      page.getByRole("listbox", { name: "Slash commands" })
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("listbox", { name: "Slash commands" })
    ).not.toBeVisible();
  });

  test("Esc closes the chat settings rail", async ({ page }) => {
    // 2026-06-13 redesign: the "Open settings" slide-over was replaced by
    // the ChatSettingsRail, opened via the ⋯ overflow menu's "Chat settings"
    // menuitem (see chat.spec.ts for the same pattern).
    await page.getByTestId("topbar-overflow-trigger").click();
    await page.getByRole("menuitem", { name: "Chat settings" }).click();
    await expect(page.getByTestId("chat-settings-rail")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("chat-settings-rail")).not.toBeVisible();
  });
});
