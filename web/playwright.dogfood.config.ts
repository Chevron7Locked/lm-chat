import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the LIVE-MODEL DOGFOOD harness.
 *
 * Unlike playwright.live.config.ts (which spawns a STUB LM Studio via
 * _fixtures.ts), this runs the journeys against the operator's REAL LM Studio.
 * The shared fixture switches to real-upstream mode on LMCHAT_REAL_UPSTREAM=1,
 * which this config sets, so `npx playwright test --config=playwright.dogfood.
 * config.ts` is self-contained. It is a PRE-SHIP / on-demand gate — real turns
 * take 30-180s and it needs LM Studio up with models loaded — never per-commit.
 *
 * Prereqs (also checked loudly at runtime): a built SPA in web/dist (the spawned
 * backend serves it), and LM Studio reachable with ≥1 general LLM + ≥1 embedder
 * loaded. `make dogfood-live` wraps the env gate + build + this run.
 */
process.env["LMCHAT_REAL_UPSTREAM"] = "1";

export default defineConfig({
  testDir: "./tests/e2e-live/flows-dogfood",
  testMatch: ["**/*.spec.ts"],
  // Serial: one real model, one journey at a time. Nondeterministic + slow, so
  // no retries (a retry would mask a real intermittent failure).
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env["CI"],
  reporter: [["list"], ["html", { open: "never" }]],
  // Generous default — individual journeys raise it further via test.setTimeout.
  timeout: 600_000,
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
