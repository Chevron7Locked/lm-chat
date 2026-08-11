/**
 * E2E: flow-34 — first-run fresh-install redirect + setup-token gating (P13f).
 *
 * Closes parity audit F-01 (first-run wizard missing) and F-02 (admin
 * setup-token gating missing).
 *
 * Backend contract:
 *   GET /api/auth/setup_status
 *     anonymous; returns { needs_setup: bool }.  The endpoint is in
 *     AuthMiddleware._SKIP_EXACT so no session cookie is required.  The
 *     response is intentionally a single boolean — exposing the raw user
 *     count to anonymous callers would leak deployment scale.
 *
 *   POST /api/auth/register
 *     when LM_CHAT_SETUP_TOKEN is set AND no users exist, requires the
 *     token via ?token=... query param OR X-Setup-Token header.  Mismatch
 *     / missing → 403 (not 401 — this is a policy gate, not auth).  After
 *     the first user registers, the gate auto-lifts.
 *
 * Model (current — no separate WelcomeWizard component): Login.tsx's own
 * setup_status check redirects a fresh install (needs_setup=true) straight
 * to `<Navigate to="/register" replace />` — there's no inline wizard
 * screen on /login itself. Register.tsx carries the "first account becomes
 * admin" framing inline, gated on the SAME setup_status fetch (its own
 * `register-admin-bootstrap-callout`, see flow-21).
 *
 * The frontend portion of the test stubs the backend so the spec runs on
 * the route-stubbed Playwright suite (no live FastAPI required).  The
 * setup-token gating itself is verified by the backend pytest suite at
 * tests/routes/test_auth.py — this spec covers the FE contract: a fresh
 * install never shows a meaningless sign-in form.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

/**
 * Register.tsx auto-chains POST /api/auth/login after a successful
 * register (no separate sign-in step, no /login waypoint on the happy
 * path — see flow-21's docstring). For the bootstrap-admin account
 * specifically (needs_setup was true at submit time) it lands on
 * "/setup/lm-studio" rather than "/".
 */
async function stubNullProbe(page: import("@playwright/test").Page) {
  await bootstrapAuthedApp(page);
  await page.route("**/api/auth/me/probe", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: null,
        username: null,
        is_admin: false,
        needs_setup: false,
        totp_enabled: false,
      }),
    })
  );
}

test.describe("Fresh-install setup redirect (P13f)", () => {
  test("fresh install: /login redirects straight to /register with the bootstrap-admin callout", async ({ page }) => {
    await stubNullProbe(page);

    // setup_status: fresh DB.
    await page.route("**/api/auth/setup_status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ needs_setup: true }),
      })
    );

    await page.goto("/login");
    // Login.tsx has nothing meaningful to show on a fresh install — it
    // redirects straight to /register rather than rendering a sign-in form.
    await page.waitForURL("**/register", { timeout: 5_000 });
    await expect(page.getByRole("heading", { name: "Create account" })).toBeVisible();
    await expect(
      page.getByTestId("register-admin-bootstrap-callout")
    ).toContainText("This account becomes the admin.");
    // The standard "Sign in" form must never have rendered.
    await expect(
      page.getByRole("heading", { name: "Sign in" })
    ).toHaveCount(0);
  });

  test("renders the standard sign-in form when needs_setup is false", async ({
    page,
  }) => {
    await stubNullProbe(page);
    await page.route("**/api/auth/setup_status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ needs_setup: false }),
      })
    );

    await page.goto("/login");
    // The standard "Sign in" form renders; no redirect fires.
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/login");
  });

  test("full fresh-DB flow: redirect → register → auto-signed-in → LM Studio setup", async ({
    page,
  }) => {
    // Track state so setup_status reflects the post-register world.
    let userRegistered = false;

    await stubNullProbe(page);

    await page.route("**/api/auth/setup_status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ needs_setup: !userRegistered }),
      })
    );

    await page.route("**/api/auth/register", async (route) => {
      userRegistered = true;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({ id: 1, username: "admin" }),
      });
    });

    await page.route("**/api/auth/login", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: 1,
          expires_at: "2026-06-01T00:00:00Z",
          username: "admin",
          is_admin: true,
          totp_enabled: false,
        }),
      })
    );

    // Fresh visit → redirected straight to /register.
    await page.goto("/login");
    await page.waitForURL("**/register", { timeout: 5_000 });
    await expect(
      page.getByTestId("register-admin-bootstrap-callout")
    ).toBeVisible();

    // Submit the registration form. Register.tsx auto-chains login() on
    // success — no separate sign-in step. Because this submission is the
    // bootstrap-admin account (needs_setup was true), the destination is
    // "/setup/lm-studio", not "/".
    await page.getByLabel("Username").fill("admin");
    await page.getByLabel("Password", { exact: true }).fill("hunter2pass");
    await page.getByLabel("Confirm password").fill("hunter2pass");
    await page.getByRole("button", { name: "Create account" }).click();

    await page.waitForURL("**/setup/lm-studio");
    await expect(page.getByTestId("setup-lmstudio-form")).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Connect LM Studio" })
    ).toBeVisible();
  });
});
