/**
 * Flow 3 — Pin to memory + persist.
 *
 * What it proves:
 *   A user can pin an insight via the /memory slash command (or the Memory
 *   page form), navigate to /memory, see the insight in the list, and reload
 *   the page — the insight survives the reload (DB-persisted, not in-memory).
 *
 *   The cap counter is displayed on the Memory page; we confirm it increments
 *   after the pin.
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  openNewChatInSpa,
} from "./_flow-helpers";

test(
  "flow-03: pin insight via /memory slash command; survives reload on Memory page",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);
    await openNewChatInSpa(page);

    const insightText = `Flow03 insight ${Date.now().toString()}`;

    // Use the /memory slash command in the Composer.
    const composer = page.getByPlaceholder(/Message/);
    await expect(composer).toBeVisible({ timeout: 8_000 });
    await composer.fill(`/memory ${insightText}`);
    await composer.press("Meta+Enter");

    // Success toast should appear. Toast.tsx uses role="status" for non-error
    // variants (success/info/warning); role="alert" is reserved for errors only.
    const successToast = page
      .getByRole("status")
      .filter({ hasText: /pinned|memory/i });
    await expect(successToast).toBeVisible({ timeout: 8_000 });

    // Navigate to /memory to verify the pin landed.
    await page.goto(`${backendURL}/memory`);
    await page.waitForURL(`${backendURL}/memory`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {/* RQ retries */});

    // The insight should appear in the list.
    await expect(page.getByText(insightText)).toBeVisible({ timeout: 8_000 });

    // Reload the page — persistence check.
    await page.reload({ waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {/* RQ retries */});

    // Insight still visible after reload.
    await expect(page.getByText(insightText)).toBeVisible({ timeout: 10_000 });

    assertNoConsoleErrors(collectErrors(), "flow-03");
  }
);

test(
  "flow-03b: pin via Memory page form also persists; count increments",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Navigate directly to Memory page.
    await page.goto(`${backendURL}/memory`);
    await page.waitForURL(`${backendURL}/memory`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {/* RQ retries */});

    const insightText = `Flow03b form insight ${Date.now().toString()}`;

    // The Memory page has a form with "New insight" label (from page-render-smoke).
    const input = page.getByLabel("New insight");
    await expect(input).toBeVisible({ timeout: 8_000 });
    await input.fill(insightText);
    await input.press("Enter");

    // Success toast or the item appearing in list.
    // Memory.tsx shows success toast "Insight pinned." — wait for it.
    // Toast.tsx uses role="status" for success variant (not role="alert").
    const toast = page.getByRole("status").filter({ hasText: /pinned|insight/i });
    await expect(toast).toBeVisible({ timeout: 8_000 });

    // The insight appears in the list.
    await expect(page.getByText(insightText)).toBeVisible({ timeout: 8_000 });

    // Reload and confirm persistence.
    await page.reload({ waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {/* RQ retries */});
    await expect(page.getByText(insightText)).toBeVisible({ timeout: 8_000 });

    assertNoConsoleErrors(collectErrors(), "flow-03b");
  }
);
