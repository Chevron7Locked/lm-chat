/**
 * E2E: login flow (Playwright).
 *
 * The test stubs the backend via Playwright's route interception so it
 * can run without a live FastAPI server. To run against the real backend:
 *   1. Start the backend: uv run uvicorn lmchat.app:app --port 8000
 *   2. Start the frontend: pnpm dev
 *   3. Remove the route stubs below and set baseURL to http://localhost:5173
 *
 * The stub strategy mirrors what the backend returns per auth.py:
 *   POST /api/auth/login → 200 { user_id, expires_at }
 *   POST /api/auth/login (no totp) → 401 { detail: "totp required" }
 *   POST /api/auth/logout → 200 { status: "ok" }
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Login page", () => {
  test.beforeEach(async ({ page }) => {
    // Correctly-typed defaults for the post-login chat-page cold load.
    // Overridden immediately below: the probe must return a null user so
    // RequireUnauth actually renders the /login form (each test in this
    // file starts unauthenticated and drives the real login form).
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

    // Intercept login attempts — default stub: success on first try.
    await page.route("**/api/auth/login", async (route) => {
      const postData = route.request().postData() ?? "";
      const params = new URLSearchParams(postData);
      const totp = params.get("totp_code");
      const password = params.get("password");

      if (password === "wrongpass") {
        await route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ detail: "invalid credentials" }),
        });
      } else if (totp === null && password === "totp-needed") {
        // Signal that TOTP is required.
        await route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ detail: "totp required" }),
        });
      } else if (totp === "123456" && password === "totp-needed") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ user_id: 1, expires_at: "2026-06-01T00:00:00Z" }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ user_id: 1, expires_at: "2026-06-01T00:00:00Z" }),
        });
      }
    });
  });

  test("renders the login form", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(page.getByLabel("Username")).toBeVisible();
    await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
    // TOTP input is hidden until server requests it.
    await expect(page.getByLabel("Authenticator code")).not.toBeVisible();
  });

  test("successful login with username + password", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("correct-pass");
    await page.getByRole("button", { name: "Sign in" }).click();

    // After successful login the router navigates to "/".
    await page.waitForURL("**/");
    await expect(page.getByRole("alert")).not.toBeVisible();
  });

  test("shows error on invalid credentials", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("wrongpass");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("invalid credentials");
  });

  test("reveals TOTP input when server returns totp required", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("totp-needed");
    await page.getByRole("button", { name: "Sign in" }).click();

    // After 401 "totp required", the TOTP field should appear.
    await expect(page.getByLabel("Authenticator code")).toBeVisible();
  });

  test("completes login after entering TOTP code", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("totp-needed");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByLabel("Authenticator code")).toBeVisible();
    await page.getByLabel("Authenticator code").fill("123456");
    await page.getByRole("button", { name: "Sign in" }).click();

    // After successful TOTP login, navigate to "/".
    await page.waitForURL("**/");
    await expect(page.getByRole("alert")).not.toBeVisible();
  });
});
