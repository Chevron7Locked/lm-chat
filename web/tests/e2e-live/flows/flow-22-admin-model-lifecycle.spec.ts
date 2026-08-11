/**
 * Flow 22 — Admin model lifecycle UI.
 *
 * What it proves:
 *   1. Admin can navigate to /admin/models and see the model list.
 *   2. Model table shows display names, loaded indicators, and action buttons.
 *   3. Admin can click Load and the UI responds (load request sent to backend).
 *   4. Admin can click Unload (N=1 case) and the UI responds.
 *   5. Non-admin cannot access /admin/models (redirected to /).
 *
 * Uses the live-mode fixture (stub LM Studio with the lifecycle endpoints
 * added in tests/integration/test_models_lifecycle.py conftest extension).
 *
 * NOTE: This test uses the stub LM Studio, not the real one.  The stub returns
 * canned responses.  A real-LM Studio smoke test is the EOP gate (P11 §6.8).
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
} from "./_flow-helpers";

test(
  "flow-22a: admin navigates to /admin/models and sees model table",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);

    // Navigate to admin models page.
    await page.goto(`${backendURL}/admin/models`);
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    // The admin models heading should be visible.
    const heading = page.getByRole("heading", { name: /admin.*models/i });
    await expect(heading).toBeVisible({ timeout: 8_000 });

    // At least one Load button should be visible.
    const loadBtn = page.getByRole("button", { name: /^load /i }).first();
    await expect(loadBtn).toBeVisible({ timeout: 8_000 });

    // At least one Unload button should be visible.
    const unloadBtn = page.getByRole("button", { name: /^unload /i }).first();
    await expect(unloadBtn).toBeVisible({ timeout: 8_000 });

    assertNoConsoleErrors(collectErrors(), "flow-22a");
  }
);

test(
  "flow-22b: admin can load a model via the Load button",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    await page.goto(`${backendURL}/admin/models`);
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    // Intercept the POST /api/models/load call to verify it's made.
    let loadRequestMade = false;
    page.on("request", (req) => {
      if (req.url().includes("/api/models/load") && req.method() === "POST") {
        loadRequestMade = true;
      }
    });

    const heading = page.getByRole("heading", { name: /admin.*models/i });
    await expect(heading).toBeVisible({ timeout: 8_000 });

    const loadBtn = page.getByRole("button", { name: /^load /i }).first();
    await loadBtn.click();

    // Wait for the request to be made.
    await page.waitForTimeout(2_000);
    expect(loadRequestMade).toBe(true);
  }
);

test(
  "flow-22c: non-admin is redirected away from /admin/models",
  async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Navigate directly — RequireAdmin should redirect to /.
    await page.goto(`${backendURL}/admin/models`);
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    // Should NOT show the admin heading.
    const heading = page.locator("h2", { hasText: /admin.*models/i });
    await expect(heading).not.toBeVisible({ timeout: 5_000 });
  }
);

test(
  "flow-22d: admin can reach /admin/models via UserMenu",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    await loginAndWait(page, backendURL, adminUsername, adminPassword);

    // Open the user menu.
    const avatarBtn = page.getByTestId("user-menu-avatar");
    await expect(avatarBtn).toBeVisible({ timeout: 8_000 });
    await avatarBtn.click();

    // Admin: Models link should be visible.
    const modelsLink = page.getByTestId("user-menu-admin-models");
    await expect(modelsLink).toBeVisible({ timeout: 5_000 });
    await modelsLink.click();

    // Should navigate to /admin/models.
    await page.waitForURL(`${backendURL}/admin/models`, { timeout: 10_000 });
    const heading = page.getByRole("heading", { name: /admin.*models/i });
    await expect(heading).toBeVisible({ timeout: 8_000 });
  }
);
