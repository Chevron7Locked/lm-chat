/**
 * Flow 20 — STT graceful degradation in Firefox (no SpeechRecognition).
 *
 * What it proves:
 *   When neither window.SpeechRecognition nor window.webkitSpeechRecognition
 *   is defined (Firefox, or any browser that does not support the Web Speech
 *   API), the MicButton renders as disabled with an informative title/label
 *   and clicking it does not cause an error.
 *
 * Mechanism:
 *   The test removes both globals via page.addInitScript() before navigation.
 *   The MicButton component (MicButton.tsx) checks detectSTT() which reads
 *   these globals; if both are absent it returns { available: false }.
 *   The resulting MicButton has `disabled=true` and
 *   title="Speech-to-text not supported in this browser".
 *
 *   Note: since the test runs in Chromium (which normally has
 *   webkitSpeechRecognition), we must delete both globals before the page
 *   script runs — addInitScript ensures this.
 *
 * Bug surface:
 *   - If MicButton does not read the capability flag and always renders
 *     enabled, the disabled assertion will fail.
 *   - If clicking the disabled mic button triggers an error (e.g., tries
 *     to call new SpeechRecognition()), the pageerror listener will catch it.
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "./_flow-helpers";

// Remove both Speech Recognition globals to simulate Firefox.
const REMOVE_SPEECH_RECOGNITION_SCRIPT = `
(function() {
  try { delete window.webkitSpeechRecognition; } catch(e) {}
  try { delete window.SpeechRecognition; } catch(e) {}
  // Confirm removal.
  window.__sttGlobalsRemoved = true;
})();
`;

test(
  "flow-20: no SpeechRecognition global — MicButton is disabled; click is safe",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    // Remove STT globals before the page JS runs.
    await page.addInitScript({ content: REMOVE_SPEECH_RECOGNITION_SCRIPT });

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "Flow20 STT Firefox");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });

    // Confirm globals were removed (sanity check for the test mechanism).
    const globalsRemoved = await page.evaluate(() => {
      return (
        !("webkitSpeechRecognition" in window) &&
        !("SpeechRecognition" in window)
      );
    });
    expect(globalsRemoved).toBe(true);

    // Locate the mic button.
    // When disabled (unavailable), MicButton has
    // aria-label="Speech-to-text not supported in this browser".
    const micBtn = page.getByRole("button", {
      name: /speech-to-text not supported/i,
    });
    await expect(micBtn).toBeVisible({ timeout: 5_000 });
    await expect(micBtn).toBeDisabled({ timeout: 3_000 });

    // The title attribute should also carry the informative message.
    const titleAttr = await micBtn.getAttribute("title");
    expect(titleAttr).toMatch(/speech-to-text not supported/i);

    // Clicking a disabled button should be a no-op (no errors, no state change).
    // force:true bypasses Playwright's "is disabled" guard so we test the
    // onClick={isDisabled ? undefined : onToggle} branch in MicButton.tsx.
    await micBtn.click({ force: true });
    await page.waitForTimeout(200);

    // aria-pressed should remain false (no state change triggered).
    const ariaPressed = await micBtn.getAttribute("aria-pressed");
    expect(ariaPressed).toBe("false");

    // Composer textarea should be empty (no transcript injected).
    const composer = page.getByPlaceholder(/Message/);
    const composerValue = await composer.inputValue();
    expect(composerValue).toBe("");

    assertNoConsoleErrors(collectErrors(), "flow-20");
  }
);
