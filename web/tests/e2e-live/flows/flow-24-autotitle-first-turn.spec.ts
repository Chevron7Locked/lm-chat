/**
 * Flow 24 — Auto-title: first assistant turn → sidebar title updates.
 *
 * AC27 — precondition gate: hard-fail if no model is loaded.
 * AC28 — round-trip: send first message → SSE complete → sidebar title
 *         changes from "New Chat" to a non-default generated title.
 * AC29 — "Generating title…" placeholder (best-effort, ≤750ms race).
 *
 * Spec: A-autotitle-verify v0.6.3.r3
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "./_flow-helpers";

const AUTO_TITLE_DEFAULTS = new Set(["New Chat", "Incognito Chat", ""]);

test(
  "flow-24: auto-title — first assistant turn triggers sidebar title update",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    // ── AC27: precondition — at least one model must be loaded ──────────────
    // Fetch directly from the backend (not through the browser). If no model
    // is loaded, hard-fail with test.fail() so CI surfaces an actionable
    // "model not loaded" error rather than an opaque Playwright timeout
    // 30 seconds later.
    const modelsResp = await page.request.get(`${backendURL}/api/models`);
    const modelsBody = await modelsResp.json() as Array<{ key: string }>;
    const loadedModels = Array.isArray(modelsBody) ? modelsBody : [];

    if (loadedModels.length === 0) {
      test.fail(
        true,
        "flow-24 precondition failed: no LM Studio models are loaded. " +
          "Load at least one model before running the autotitle live flow."
      );
      return;
    }

    // ── Setup ────────────────────────────────────────────────────────────────
    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "New Chat");

    // Navigate to the chat.
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    const composer = page.getByPlaceholder(/Message/);
    await composer.waitFor({ state: "visible", timeout: 15_000 });

    // Wait for the model selector to populate (same guard as other flows).
    const modelSelect = page.locator('[data-testid="chat-header-model-select"]');
    await modelSelect.waitFor({ state: "visible", timeout: 5_000 });
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]'
        );
        if (sel === null) return false;
        const realOptions = Array.from(sel.options).filter((o) => o.value !== "");
        return sel.value !== "" && realOptions.length > 0;
      },
      null,
      { timeout: 30_000 },
    );

    // ── AC29: best-effort "Generating title…" placeholder ───────────────────
    // Race a 750ms window against the placeholder appearing. If it never
    // appears (fast LM Studio response < 100ms), log a warning and continue —
    // do NOT fail.
    let placeholderObserved = false;
    const placeholderRace = page
      .locator("text=Generating title…")
      .waitFor({ state: "visible", timeout: 750 })
      .then(() => {
        placeholderObserved = true;
      })
      .catch(() => {
        // Best-effort — not a failure.
      });

    // ── Send the first message ───────────────────────────────────────────────
    await composer.fill("Hello, this is my first message in flow-24.");
    await page.keyboard.press("Enter");

    // Wait for SSE stream to complete: the composer re-enables after "complete".
    await expect(composer).toBeEnabled({ timeout: 30_000 });

    // Resolve the placeholder race (will have settled either way by now).
    await placeholderRace;

    if (!placeholderObserved) {
      test.info().annotations.push({
        type: "warn",
        description:
          "flow-24 AC29: 'Generating title…' placeholder was not visible within 750ms — " +
          "LM Studio may have responded faster than the observability window. " +
          "This is expected when the title endpoint is very fast.",
      });
    }

    // ── AC28: poll the sidebar until the title is non-default ───────────────
    // The sidebar renders a data-chat-id attribute on each row.
    // Poll for up to 30 seconds after SSE complete.
    const sidebarItem = page.locator(`[data-chat-id="${String(chatId)}"]`);

    await expect(async () => {
      const titleText = await sidebarItem.textContent();
      const trimmed = (titleText ?? "").trim();
      // Title must be non-empty and not one of the default placeholder values.
      expect(trimmed.length).toBeGreaterThan(0);
      expect(AUTO_TITLE_DEFAULTS.has(trimmed)).toBe(false);
    }).toPass({ timeout: 30_000, intervals: [500, 500, 1000, 1000] });

    assertNoConsoleErrors(collectErrors(), "flow-24");
  }
);
