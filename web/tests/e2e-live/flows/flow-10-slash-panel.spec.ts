/**
 * Flow 10 — Slash `/panel` command.
 *
 * What it proves:
 *   When a user types /panel in the Composer and submits:
 *     1. An info toast appears with the message:
 *        "/panel isn't in this build yet — multi-model convergence is coming."
 *     2. The toast uses aria-live="polite" (info variant).
 *     3. No crash or unhandled error occurs.
 *     4. The Composer is cleared after the command dispatches.
 *
 * Source: Composer.tsx dispatchSlashCommand("panel"):
 *   push({ variant: "info", message: "/panel isn't in this build yet — multi-model convergence is coming." })
 *
 * This test also exercises the legacy-parity test_all_variants_render info
 * variant from a dedicated flow file (P10c scoping rule: one flow = one file).
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  openNewChatInSpa,
} from "./_flow-helpers";

test(
  "flow-10: /panel shows info toast (build-not-ready message); no crash; composer clears",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);
    await openNewChatInSpa(page);

    const composer = page.getByPlaceholder(/Message/);
    await expect(composer).toBeVisible({ timeout: 8_000 });

    // Submit /panel.
    await composer.fill("/panel");
    await composer.press("Meta+Enter");

    // Info toast with the build-not-ready message.
    const panelToast = page
      .getByRole("alert")
      .filter({ hasText: /multi-model convergence/i });
    await expect(panelToast).toBeVisible({ timeout: 8_000 });

    // Toast uses aria-live="polite" (info variant per Toast.tsx).
    await expect(panelToast).toHaveAttribute("aria-live", "polite");

    // Toast message contains the expected text (stable substring of Composer.tsx output).
    await expect(panelToast).toContainText("multi-model convergence is coming");

    // Composer was cleared after dispatch.
    await expect(composer).toHaveValue("", { timeout: 3_000 });

    // No unhandled errors — page is still functional.
    // Confirm Composer is still interactive (type a char, then clear).
    await composer.fill("a");
    await expect(composer).toHaveValue("a");
    await composer.clear();

    assertNoConsoleErrors(collectErrors(), "flow-10");
  }
);
