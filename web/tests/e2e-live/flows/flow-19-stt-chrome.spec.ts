/**
 * Flow 19 — Speech-to-text in Chrome (mock SpeechRecognition).
 *
 * What it proves:
 *   When window.webkitSpeechRecognition (or SpeechRecognition) is available,
 *   clicking the mic button starts listening (aria-pressed=true), a synthetic
 *   recognition result event injects a transcript into the Composer textarea,
 *   and the aria-live announcement updates accordingly.
 *
 * Mechanism:
 *   Playwright runs in Chromium but microphone permission is not granted in
 *   CI / headless mode.  Instead of using a real microphone, this test:
 *     1. Mocks window.webkitSpeechRecognition before page navigation.
 *     2. The mock captures the recognition instance created by useSTT.
 *     3. After the mic button is clicked, the mock fires a synthetic
 *        SpeechRecognitionEvent with a fake transcript via onresult.
 *     4. Asserts the transcript appears in the Composer textarea.
 *
 * The mock is installed via page.addInitScript() so it runs before any
 * React code executes, making it available when the Composer mounts.
 *
 * Bug surface:
 *   - If useSTT checks webkitSpeechRecognition but the mock is not picked
 *     up (e.g., the check fires before addInitScript is installed), the
 *     MicButton will be disabled.  The test will catch that via the
 *     aria-pressed assertion.
 *   - If the onresult callback does not inject the transcript into the
 *     Composer, the textarea assertion will fail.
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "./_flow-helpers";

// Mock SpeechRecognition script injected before page load.
// It replaces window.webkitSpeechRecognition with a fake class whose
// start() method fires onresult synchronously with a fixed transcript.
const MOCK_SPEECH_RECOGNITION_SCRIPT = `
(function() {
  // Keep track of the last created instance so the test can fire events on it.
  window.__sttMockInstance = null;

  class MockSpeechRecognition {
    constructor() {
      this.lang = '';
      this.continuous = false;
      this.interimResults = false;
      this.onresult = null;
      this.onend = null;
      this.onerror = null;
      window.__sttMockInstance = this;
    }
    start() {
      // Fire a synthetic result event after a brief timeout.
      setTimeout(() => {
        if (this.onresult) {
          // Build a minimal SpeechRecognitionEvent shape.
          const result = {
            0: { transcript: 'hello from mock speech recognition' },
            length: 1,
            isFinal: true,
          };
          const results = {
            0: result,
            length: 1,
            item: (i) => results[i],
          };
          // Use Object.create to satisfy Array.from() in useSTT onresult handler.
          const arrLike = { length: 1, 0: result };
          this.onresult({ results: arrLike });
        }
        // Fire onend to transition state back to idle.
        if (this.onend) {
          setTimeout(() => { this.onend(); }, 50);
        }
      }, 100);
    }
    stop() {
      if (this.onend) this.onend();
    }
  }

  // Install as both webkit-prefixed and standard variants.
  window.webkitSpeechRecognition = MockSpeechRecognition;
  window.SpeechRecognition = MockSpeechRecognition;
})();
`;

test(
  "flow-19: mock SpeechRecognition — click mic → transcript injects into Composer",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    // Install the mock before any page navigation.
    await page.addInitScript({ content: MOCK_SPEECH_RECOGNITION_SCRIPT });

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "Flow19 STT Chrome");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });

    // Verify the mic button is present and NOT disabled (capability detected).
    const micBtn = page.getByRole("button", { name: /start speech-to-text|stop recording/i });
    await expect(micBtn).toBeVisible({ timeout: 5_000 });
    await expect(micBtn).not.toBeDisabled({ timeout: 3_000 });

    // Initial state: aria-pressed should be false (not listening).
    await expect(micBtn).toHaveAttribute("aria-pressed", "false", { timeout: 3_000 });

    // Click the mic button to start listening.
    await micBtn.click();

    // After clicking, aria-pressed should transition to true (listening=true).
    // Allow a brief moment for the React state update.
    await expect(micBtn).toHaveAttribute("aria-pressed", "true", { timeout: 3_000 });

    // The mock fires onresult after 100ms and onend after 150ms.
    // Wait for the transcript to be injected and state to return to idle.
    await page.waitForTimeout(500);

    // The Composer textarea should now contain the mock transcript.
    const composer = page.getByPlaceholder(/Message/);
    const composerValue = await composer.inputValue();
    expect(composerValue.toLowerCase()).toContain("hello from mock speech recognition");

    // aria-pressed should be false again (listening ended after onend).
    await expect(micBtn).toHaveAttribute("aria-pressed", "false", { timeout: 3_000 });

    assertNoConsoleErrors(collectErrors(), "flow-19");
  }
);
