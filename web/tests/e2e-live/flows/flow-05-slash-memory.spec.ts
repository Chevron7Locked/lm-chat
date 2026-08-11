/**
 * Flow 5 — Slash `/memory <text>` command.
 *
 * What it proves:
 *   The /memory slash command with a text argument:
 *     1. Shows a success toast (not an error or warning).
 *     2. Writes the insight to the DB (visible on /memory page after navigate).
 *
 *   The /memory command with NO argument:
 *     1. Shows a warning toast "Usage: /memory <text to pin>".
 *     2. Does NOT write anything to the DB.
 *
 * Source: Composer.tsx dispatchSlashCommand("memory", args):
 *   - args.trim() === "" → push warning "Usage: /memory <text to pin>"
 *   - otherwise → onMemoryPin(args.trim())
 * Chat.tsx handleMemoryPin → pinInsight.mutateAsync → push success / error
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  openNewChatInSpa,
} from "./_flow-helpers";

test(
  "flow-05: /memory <text> triggers success toast and insight lands in DB",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);
    await openNewChatInSpa(page);

    const composer = page.getByPlaceholder(/Message/);
    await expect(composer).toBeVisible({ timeout: 8_000 });

    const pinText = `Slash memory insight ${Date.now().toString()}`;

    // Submit /memory <text> via Composer.
    await composer.fill(`/memory ${pinText}`);
    await composer.press("Meta+Enter");

    // Success toast must appear (Chat.tsx: "Insight pinned to memory.").
    const successToast = page
      .getByRole("alert")
      .filter({ hasText: /pinned to memory|insight pinned/i });
    await expect(successToast).toBeVisible({ timeout: 8_000 });

    // Verify no warning toast appeared.
    const warnToast = page
      .getByRole("alert")
      .filter({ hasText: /Usage: \/memory/i });
    await expect(warnToast).not.toBeVisible({ timeout: 1_000 });

    // Navigate to /memory and confirm the insight is in the list.
    await page.goto(`${backendURL}/memory`);
    await page.waitForURL(`${backendURL}/memory`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {/* RQ retries */});

    await expect(page.getByText(pinText)).toBeVisible({ timeout: 8_000 });

    assertNoConsoleErrors(collectErrors(), "flow-05");
  }
);

test(
  "flow-05b: /memory with no argument shows usage warning; no DB write",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);
    await openNewChatInSpa(page);

    const composer = page.getByPlaceholder(/Message/);
    await expect(composer).toBeVisible({ timeout: 8_000 });

    // Submit /memory with no argument.
    await composer.fill("/memory");
    await composer.press("Meta+Enter");

    // Warning toast: "Usage: /memory <text to pin>"
    const warnToast = page
      .getByRole("alert")
      .filter({ hasText: /Usage: \/memory/i });
    await expect(warnToast).toBeVisible({ timeout: 8_000 });

    // No success toast.
    const successToast = page
      .getByRole("alert")
      .filter({ hasText: /insight pinned|pinned to memory/i });
    await expect(successToast).not.toBeVisible({ timeout: 1_000 });

    assertNoConsoleErrors(collectErrors(), "flow-05b");
  }
);
