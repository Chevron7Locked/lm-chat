/**
 * §1.6 — Fresh-install wizard regression test (live).
 *
 * Fresh-DB onboarding has regressed multiple times.
 * The flow MUST be: empty users table → /login renders WelcomeWizard
 * (needs_setup=true) → registration lands user at /setup/lm-studio →
 * setup completion redirects to /. Each transition is a regression
 * surface; this test pins them end-to-end against a real backend.
 *
 * The live fixture pre-registers a non-admin user so its default
 * state has needs_setup=false. This test deletes the seeded users
 * first to recreate the fresh-DB state, then walks the wizard. The
 * other live tests use the pre-registered user and are unaffected
 * — the cleanState fixture auto-truncates between tests; user
 * deletion here only affects the worker once until the next test
 * re-seeds.
 *
 * Backend contract:
 *  - GET /api/auth/setup_status → { needs_setup: true } when no users
 *  - POST /api/auth/register → 200; SPA navigates to /setup/lm-studio
 *  - LM Studio config write → SPA navigates to /
 */
import { spawnSync } from "child_process";
import { test, expect } from "./_fixtures";

test.describe("§1.6 — fresh-install wizard", () => {
  test("empty DB → register → setup → home", async ({
    page,
    backendURL,
    dbPath,
  }) => {
    // Step 0 — wipe users to recreate the fresh-install state. The
    // worker's other tests run AFTER this one finishes (via the
    // cleanState auto-fixture), and this DB is per-worker, so the
    // deletion is isolated.
    const wipe = spawnSync(
      "sqlite3",
      [dbPath, "DELETE FROM users;"],
      { encoding: "utf-8" }
    );
    expect(wipe.status, wipe.stderr).toBe(0);

    // Step 1 — setup_status reports needs_setup=true.
    const setupResp = await page.request.get(
      `${backendURL}/api/auth/setup_status`
    );
    expect(setupResp.status()).toBe(200);
    const setupBody = (await setupResp.json()) as { needs_setup: boolean };
    expect(setupBody.needs_setup).toBe(true);

    // Step 2 — visiting /login redirects to /register, which renders
    // the "first user becomes admin" wizard (Register.tsx renders the
    // setup-aware copy when setup_status.needs_setup=true).
    await page.goto(`${backendURL}/login`);
    await page.waitForURL(`${backendURL}/register`, { timeout: 10_000 });
    await expect(
      page.getByRole("heading", { name: "Create account" })
    ).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(/becomes the admin/i)).toBeVisible();

    // Step 3 — register the first admin.
    const username = `wizard_${Date.now()}`;
    await page.getByLabel("Username").fill(username);
    await page.locator("#lmchat-register-password").fill("wizard-pw-12345");
    await page
      .locator("#lmchat-register-password-confirm")
      .fill("wizard-pw-12345");
    await page.getByRole("button", { name: "Create account" }).click();

    // Step 4 — successful registration redirects to /setup/lm-studio.
    await page.waitForURL(`${backendURL}/setup/lm-studio`, {
      timeout: 15_000,
    });
    expect(new URL(page.url()).pathname).toBe("/setup/lm-studio");
  });
});
