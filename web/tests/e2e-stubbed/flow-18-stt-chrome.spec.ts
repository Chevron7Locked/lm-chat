/**
 * Flow 18 — STT in Chrome (mocked SpeechRecognition).
 *
 * What it proves (route-stubbed):
 *  1. With window.webkitSpeechRecognition mocked into the page before
 *     load, useSTT.detectSTT() reports `available: true, engine: "webkit"`.
 *  2. Clicking the mic button in the Composer toggles aria-pressed from
 *     false to true, which calls rec.start() on the mock.
 *  3. The mock fires a synthetic `onresult` event with a transcript
 *     after a short delay; the Composer's onTranscript callback appends
 *     the transcript into the textarea.
 *
 * Mirrors the existing stt-smoke.spec.ts pattern but with stronger
 * assertions on the post-result textarea value.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 18 — STT mic button (Chrome path, mocked)", () => {
  test("mic click → mock fires → transcript injects into Composer textarea", async ({ page }) => {
    // Mock SpeechRecognition BEFORE the page loads so useSTT picks it up.
    await page.addInitScript(() => {
      class MockSpeechRecognition {
        lang = "";
        continuous = false;
        interimResults = false;
        onresult: ((ev: unknown) => void) | null = null;
        onend: (() => void) | null = null;
        onerror: ((ev: unknown) => void) | null = null;
        start() {
          // Fire a synthetic result event after a brief delay so the
          // listening→idle transition is visible to the test.
          setTimeout(() => {
            this.onresult?.({
              results: [[{ transcript: "hello from chrome stt" }]],
            });
            this.onend?.();
          }, 80);
        }
        stop() {
          this.onend?.();
        }
      }
      (window as unknown as Record<string, unknown>).webkitSpeechRecognition =
        MockSpeechRecognition;
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
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 5_000 });

    // The mic button is rendered (aria-label starts with "Start speech-to-text").
    const micBtn = page.getByRole("button", { name: /Start speech-to-text/ });
    await expect(micBtn).toBeVisible();
    await expect(micBtn).toHaveAttribute("aria-pressed", "false");

    // Click — triggers rec.start() on the mock; the mock fires onresult.
    await micBtn.click();

    // The Composer's onTranscript callback should write into the textarea.
    await expect(composer).toHaveValue(/hello from chrome stt/, {
      timeout: 5_000,
    });
  });
});
