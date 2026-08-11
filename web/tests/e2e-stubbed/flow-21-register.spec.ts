/**
 * E2E: flow-21 register (Playwright).
 *
 * Stubs POST /api/auth/register and the downstream /api/auth/login that
 * Register.tsx chains automatically after a successful account creation.
 * Mirrors the route-stubbing strategy in login.spec.ts so the test runs
 * without a live FastAPI backend.
 *
 * Backend contract (src/lmchat/routes/auth.py:131-162):
 *   POST /api/auth/register (form-encoded username + password)
 *     → 201 { id, username }  (NO Set-Cookie; user is NOT logged in)
 *     → 400 { detail: "username already taken" }
 *     → 422 { detail: "username must match ^[a-zA-Z0-9_]{3,64}$" }
 *
 * Register.tsx auto-chains POST /api/auth/login right after a successful
 * register so the user doesn't retype credentials — the happy path lands
 * straight on "/" (or "/setup/lm-studio" for the bootstrap-admin account,
 * i.e. when GET /api/auth/setup_status reported needs_setup=true at submit
 * time). /login is only reached as a fallback if that auto-login itself
 * fails, carrying a `justRegistered` banner.
 *
 * The bootstrap-admin callout on the form itself ("This account becomes
 * the admin.") is gated on the SAME setup_status call reporting
 * needs_setup=true — this is the fresh-install path.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Register page", () => {
  test.beforeEach(async ({ page }) => {
    // Correctly-typed defaults for the post-login chat-page cold load.
    // Probe is overridden to a null user below: these tests start from a
    // cold, unauthenticated boot and drive the real register/login forms.
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

    // Default stub: registration succeeds for any username except "taken".
    await page.route("**/api/auth/register", async (route) => {
      const postData = route.request().postData() ?? "";
      const params = new URLSearchParams(postData);
      const username = params.get("username") ?? "";

      if (username === "taken") {
        await route.fulfill({
          status: 400,
          contentType: "application/json",
          body: JSON.stringify({ detail: "username already taken" }),
        });
      } else {
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({ id: 7, username }),
        });
      }
    });

    // Login stub for the auto-chained sign-in that follows a successful
    // register.
    await page.route("**/api/auth/login", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: 7,
          expires_at: "2026-06-01T00:00:00Z",
          username: "newuser",
          is_admin: false,
          totp_enabled: false,
        }),
      });
    });
  });

  test("renders the create-account form", async ({ page }) => {
    // Bootstrap-admin path: setup_status reports needs_setup=true so the
    // callout renders.
    await page.route("**/api/auth/setup_status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ needs_setup: true }),
      })
    );

    await page.goto("/register");
    await expect(page.getByRole("heading", { name: "Create account" })).toBeVisible();
    await expect(page.getByLabel("Username")).toBeVisible();
    await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Confirm password")).toBeVisible();
    // Bootstrap-admin callout — surfaces the auth_service.register() rule
    // that the first registered user is auto-promoted to admin.
    await expect(
      page.getByTestId("register-admin-bootstrap-callout")
    ).toContainText("This account becomes the admin.");
  });

  test("registers → auto-signs-in → lands on the chat page", async ({ page }) => {
    await page.goto("/register");
    await page.getByLabel("Username").fill("newuser");
    await page.getByLabel("Password", { exact: true }).fill("hunter2pass");
    await page.getByLabel("Confirm password").fill("hunter2pass");
    await page.getByRole("button", { name: "Create account" }).click();

    // Register.tsx chains login() automatically on 201 — no separate
    // sign-in step, no /login waypoint. needs_setup=false (default stub)
    // means this is NOT the bootstrap-admin account, so the destination
    // is "/" rather than "/setup/lm-studio".
    await page.waitForURL((url) => url.pathname === "/");
  });

  test("shows the server error when the username is taken", async ({ page }) => {
    await page.goto("/register");
    await page.getByLabel("Username").fill("taken");
    await page.getByLabel("Password", { exact: true }).fill("hunter2pass");
    await page.getByLabel("Confirm password").fill("hunter2pass");
    await page.getByRole("button", { name: "Create account" }).click();

    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("username already taken");
  });

  test("Login page has no public Create-account link (invite-only registration)", async ({
    page,
  }) => {
    // By design (see Login.tsx): a fresh install redirects straight to
    // /register (no meaningful sign-in form to show), and once accounts
    // exist, registration is invite-only via /register?token=… — so the
    // sign-in form deliberately exposes no public "Create account" link.
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Create account →" })
    ).toHaveCount(0);
  });
});
