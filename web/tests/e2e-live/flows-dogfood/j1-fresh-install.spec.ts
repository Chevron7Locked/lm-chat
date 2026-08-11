/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J1 — fresh-install wizard seeds the FASTEST loaded model as the
 * background-tasks default (guards 0ad28db).
 *
 * Mirrors web/tests/e2e-live/fresh_install_wizard.spec.ts (wipe users →
 * register → /setup/lm-studio) against the operator's REAL LM Studio fleet,
 * then drives the wizard's Test connection → pick a chat model → Save and
 * continue, and asserts the seeded `preferred_background_model_id` is the
 * FAST-ranked loaded model — NOT simply the first one in probe order, which
 * on a multi-model rig used to be the largest (e.g. a 122B-MoE), making every
 * background aux call (auto-memory distillation / titles / follow-ups) slow
 * or timeout-prone. See SetupLmStudio.tsx's pickFastestModel seed on first
 * save (fires only when existingBackgroundPref === null, i.e. this fresh
 * admin).
 *
 * Which chat model we pick in the dropdown is IRRELEVANT to the assertion:
 * the background-model seed is re-derived from probe.models independently on
 * save, so this test can pick any reachable model and still validate the
 * fastest-pick logic via a second, independent classifyFleet() probe.
 */
import { spawnSync } from "child_process";
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
} from "../flows/_flow-helpers";
import { classifyFleet } from "./_dogfood-helpers";

test(
  "j1: fresh-install wizard seeds the FASTEST loaded model as the background default",
  async ({ page, backendURL, dbPath }) => {
    test.setTimeout(600_000);
    const collectErrors = attachErrorCollector(page);

    // Step 0 — wipe users to recreate the fresh-install state (mirrors
    // fresh_install_wizard.spec.ts). Per-worker DB, isolated by port.
    const wipe = spawnSync("sqlite3", [dbPath, "DELETE FROM users;"], {
      encoding: "utf-8",
    });
    expect(wipe.status, wipe.stderr).toBe(0);

    // Step 1 — setup_status reports needs_setup=true.
    const setupResp = await page.request.get(
      `${backendURL}/api/auth/setup_status`,
    );
    expect(setupResp.status()).toBe(200);
    const setupBody = (await setupResp.json()) as { needs_setup: boolean };
    expect(setupBody.needs_setup).toBe(true);

    // Step 2 — /login redirects to /register (setup-aware "first user
    // becomes admin" copy).
    await page.goto(`${backendURL}/login`);
    await page.waitForURL(`${backendURL}/register`, { timeout: 10_000 });
    await expect(
      page.getByRole("heading", { name: "Create account" }),
    ).toBeVisible({ timeout: 10_000 });

    // Step 3 — register the first user; becomes admin.
    const username = `j1wizard_${String(Date.now())}`;
    await page.getByLabel("Username").fill(username);
    await page.locator("#lmchat-register-password").fill("wizard-pw-12345");
    await page
      .locator("#lmchat-register-password-confirm")
      .fill("wizard-pw-12345");
    await page.getByRole("button", { name: "Create account" }).click();

    // Step 4 — registration redirects to /setup/lm-studio.
    await page.waitForURL(`${backendURL}/setup/lm-studio`, {
      timeout: 15_000,
    });

    // Step 5 — drive the wizard against the REAL LM Studio.
    const lmStudioUrl =
      process.env["LMCHAT_DOGFOOD_LMSTUDIO_URL"] ?? "http://localhost:1234";
    const lmStudioKey = process.env["LMCHAT_DOGFOOD_LMSTUDIO_KEY"] ?? "";

    await page.getByTestId("setup-lmstudio-base-url").fill(lmStudioUrl);
    if (lmStudioKey !== "") {
      await page.getByTestId("setup-lmstudio-api-key").fill(lmStudioKey);
    }

    await page.getByTestId("setup-lmstudio-test-connection").click();
    await expect(
      page.getByTestId("setup-lmstudio-probe-result"),
    ).toContainText("Connected", { timeout: 30_000 });

    // Wait for the default-model dropdown to populate past the placeholder.
    const modelSelect = page.getByTestId("setup-lmstudio-default-model");
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="setup-lmstudio-default-model"]',
        );
        if (sel === null) return false;
        return (
          Array.from(sel.options).filter((o) => o.value !== "").length > 0
        );
      },
      null,
      { timeout: 30_000 },
    );
    await modelSelect.selectOption({ index: 1 });

    await page.getByTestId("setup-lmstudio-save").click();
    await page.waitForURL(`${backendURL}/`, { timeout: 30_000 });

    // Step 6 — assert the seeded background-model preference is the
    // FAST-ranked loaded model. classifyFleet runs its OWN independent
    // /api/models probe, so this is not just re-reading the save's own math.
    const fleet = await classifyFleet(page, backendURL);
    const cfgResp = await page.request.get(
      `${backendURL}/api/settings/lmstudio`,
    );
    expect(cfgResp.ok()).toBe(true);
    const cfg = (await cfgResp.json()) as {
      preferred_background_model_id?: string | null;
    };
    expect(
      cfg.preferred_background_model_id,
      `expected the wizard to seed the FASTEST loaded model (${fleet.fastId}) ` +
        `as the background default, got ${String(cfg.preferred_background_model_id)}`,
    ).toBe(fleet.fastId);

    assertNoConsoleErrors(collectErrors(), "j1");
  },
);
