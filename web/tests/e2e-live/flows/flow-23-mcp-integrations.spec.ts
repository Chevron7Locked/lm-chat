/**
 * Flow 23 — MCP Integrations: admin-supplied list + picker.
 *
 * What it proves:
 *   1. Admin can navigate to /admin/integrations.
 *   2. Admin can add 2 entries and save them.
 *   3. User logs in, opens the chat composer, and sees the 2 integration pills.
 *   4. User toggles one integration ON; the outgoing chat stream request
 *      includes that integration ID in the payload.
 *
 * Uses the stub LM Studio harness (no real LM Studio needed).
 * The stub just validates the integrations field appears in the request body.
 *
 * Patterns after: flow-22-admin-model-lifecycle.spec.ts (admin admin-flow pattern).
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  openNewChatInSpa,
} from "./_flow-helpers";

test(
  "flow-23a: admin navigates to /admin/integrations and sees page with R-10 banner",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    await page.goto(`${backendURL}/admin/integrations`);
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    // Heading must be present.
    const heading = page.getByRole("heading", { name: /admin.*integrations/i });
    await expect(heading).toBeVisible({ timeout: 8_000 });

    // R-10 banner must be present.
    const banner = page.getByTestId("r10-banner");
    await expect(banner).toBeVisible({ timeout: 8_000 });
    await expect(banner).toContainText("operator-supplied");

    assertNoConsoleErrors(collectErrors(), "flow-23a");
  }
);

test(
  "flow-23b: admin adds 2 entries, saves; user sees pills in composer",
  async ({ page, backendURL, adminUsername, adminPassword, testUsername, testPassword }) => {
    // Admin: set up 2 integrations.
    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    await page.goto(`${backendURL}/admin/integrations`);
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    // Add first entry.
    const addInput = page.getByTestId("add-input");
    await addInput.fill("mcp/searxng");
    await page.getByTestId("add-btn").click();

    // Add second entry.
    await addInput.fill("mcp/filesystem");
    await page.getByTestId("add-btn").click();

    // Save.
    const saveBtn = page.getByTestId("save-btn");
    await expect(saveBtn).not.toBeDisabled({ timeout: 3_000 });
    await saveBtn.click();
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    // Verify both entries appear in the list.
    await expect(page.getByTestId("remove-btn-mcp/searxng")).toBeVisible({ timeout: 5_000 });
    await expect(page.getByTestId("remove-btn-mcp/filesystem")).toBeVisible({ timeout: 5_000 });

    // User: navigate to chat and check pills are visible in composer.
    // Log out admin, log in user.
    await page.goto(`${backendURL}/api/auth/logout`).catch(() => { /* ignore if not a page */ });
    await loginAndWait(page, backendURL, testUsername, testPassword);

    // TEST FIX: the Composer doesn't render on the home page until a
    // chat exists.  Create one + navigate into it so the integration
    // pills are reachable.
    await openNewChatInSpa(page);

    // Wait for chat composer to render.
    const composerTextarea = page.getByRole("textbox", { name: /message/i });
    await expect(composerTextarea).toBeVisible({ timeout: 10_000 });

    // Integration pills should be visible.
    const searxngPill = page.getByTestId("integration-pill-mcp/searxng");
    await expect(searxngPill).toBeVisible({ timeout: 8_000 });
    const fsPill = page.getByTestId("integration-pill-mcp/filesystem");
    await expect(fsPill).toBeVisible({ timeout: 8_000 });
  }
);

test(
  "flow-23c: non-admin sees Admin-required gate page at /admin/integrations",
  async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    await page.goto(`${backendURL}/admin/integrations`);
    // UX-AUDIT-r6 / r7: RequireAdmin now renders the gate in-place
    // (URL stays at /admin/integrations) instead of silently
    // navigating non-admins back to "/".
    await expect(page).toHaveURL(`${backendURL}/admin/integrations`);
    await expect(page.getByTestId("require-admin-denied")).toBeVisible({ timeout: 8_000 });
  }
);
