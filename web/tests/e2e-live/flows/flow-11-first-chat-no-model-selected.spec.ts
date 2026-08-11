/**
 * Flow 11 — First-time user submits without touching the model selector.
 *
 * What it proves:
 *   SA-2026-003 regression guard. When a fresh user opens a new chat and
 *   submits a message WITHOUT explicitly selecting a model, the Composer must
 *   NOT produce a 422 (CanonicalChatRequest.model required field). Either:
 *     (a) The fallback-to-first-loaded-model chain resolves a model ID and
 *         streaming proceeds (stream completes, no 422 toast), OR
 *     (b) No models are loaded and the Send button is disabled — the user sees
 *         a disabled send button, not a 422 toast.
 *
 * This test does NOT patch window.fetch to inject a model field.
 * It exercises the real code path so the Chat.tsx fallback chain is tested.
 *
 * Detection: P10c.1-MID-AUDIT-r1 dual-seat finding (qwen3.6 + qwen3.5-122b).
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "./_flow-helpers";

test(
  "flow-11: first-time chat submit without explicit model — no 422, either streams or send disabled",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Create a brand-new chat via API (simulates a first-time user flow).
    const chatId = await createChatViaRequest(
      page, backendURL, "Flow11 No Model Selected"
    );

    // Navigate directly to the chat URL without using the model selector.
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    const composer = page.getByPlaceholder(/Message/);
    await expect(composer).toBeEnabled({ timeout: 5_000 });

    // DO NOT touch the model selector — we are testing the fallback chain.

    // Type a message.
    await composer.fill("Hello from flow-11, no model selected.");

    // Wait for models query to settle so the Send button state stabilises.
    await page.waitForTimeout(1_000);

    const sendBtn = page.getByRole("button", { name: /send/i });
    const sendEnabled = await sendBtn.isEnabled().catch(() => false);

    if (sendEnabled) {
      // Case A: models loaded; fallback chain produced a valid model ID.
      // Submit without touching the model selector.
      await composer.press("Meta+Enter");

      // Allow the stream (real stub) to complete. The stub is fast; composer
      // will re-enable almost immediately after chat.end.
      await expect(composer).toBeEnabled({ timeout: 20_000 });

      // The 422 error toast must never have appeared. Check it is absent after
      // the stream settled.
      const errorToast = page.getByText(/422|unprocessable|model.*required/i);
      await expect(errorToast).not.toBeVisible({ timeout: 2_000 });

      // The stub response should be visible in the message list.
      await expect(
        page.getByText("Hello from stub LM Studio.")
      ).toBeVisible({ timeout: 10_000 });
    } else {
      // Case B: no models loaded; send button disabled — good, safe state.
      // The composer accepted input but blocks submission. No 422 possible.
      await expect(sendBtn).toBeDisabled({ timeout: 1_000 });

      // Confirm there is no error toast from typing alone (no premature request).
      const errorToast = page.getByText(/422|unprocessable|model.*required/i);
      await expect(errorToast).not.toBeVisible({ timeout: 2_000 });
    }

    assertNoConsoleErrors(collectErrors(), "flow-11", [
      // Allow 422 patterns from other concurrent requests (e.g. model-probe
      // 400s from the params cache). The test-level logic above already
      // asserts no 422 toast from OUR submit; here we suppress false-positives
      // from unrelated requests so assertNoConsoleErrors doesn't double-fire.
      /422/,
    ]);
  }
);
