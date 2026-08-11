/**
 * Flow 33 — Account change-password (P13c.2).
 *
 * What it proves (route-stubbed):
 *  1. Visiting /settings/account renders the AccountSection from P13c.2
 *     with the username read-out, deferred display-name notice, and
 *     change-password form.
 *  2. Submitting the change-password form with matched fields issues a
 *     form-encoded POST to /api/auth/password and shows a success toast.
 *  3. Submitting with mismatched confirm shows an inline error and
 *     never hits the API.
 *  4. A 400 backend response surfaces the server detail in the inline
 *     error region and does NOT toast success.
 *
 * Aligned with the existing flow-25 setup (same login + /me +
 * supporting-route stubs).
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 33 — Account change-password", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);
  });

  test("renders the Account tab with the change-password form (P13l.5: username is the identity)", async ({ page }) => {
    await page.goto("/settings/login-security");

    await expect(page.getByTestId("settings-account-section")).toBeVisible();
    // P13l.5: the deferred-display-name banner has been removed — the
    // username is the identity display.
    await expect(
      page.getByTestId("account-display-name-deferred"),
    ).toHaveCount(0);
    await expect(page.getByTestId("account-change-password-form")).toBeVisible();
  });

  test("submits a form-encoded POST and shows success", async ({ page }) => {
    let postedBody: string | null = null;
    let postedContentType: string | null = null;
    await page.route("**/api/auth/password", async (route, request) => {
      postedBody = request.postData();
      postedContentType = request.headers()["content-type"] ?? null;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "ok" }),
      });
    });

    await page.goto("/settings/login-security");

    await page.getByTestId("account-old-password").fill("oldpw1234");
    await page.getByTestId("account-new-password").fill("newpw5678");
    await page.getByTestId("account-confirm-password").fill("newpw5678");
    await page.getByTestId("account-change-password-submit").click();

    // Form clears on success.
    await expect(page.getByTestId("account-old-password")).toHaveValue("");
    await expect(page.getByTestId("account-new-password")).toHaveValue("");
    await expect(page.getByTestId("account-confirm-password")).toHaveValue("");

    // Request shape.
    expect(postedContentType).toContain("application/x-www-form-urlencoded");
    expect(postedBody).toContain("old_password=oldpw1234");
    expect(postedBody).toContain("new_password=newpw5678");
  });

  test("mismatched confirm shows an inline error and never calls the API", async ({ page }) => {
    let called = false;
    await page.route("**/api/auth/password", async (route) => {
      called = true;
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ status: "ok" }),
      });
    });

    await page.goto("/settings/login-security");

    await page.getByTestId("account-old-password").fill("oldpw1234");
    await page.getByTestId("account-new-password").fill("newpw5678");
    await page.getByTestId("account-confirm-password").fill("different9");
    await page.getByTestId("account-change-password-submit").click();

    await expect(page.getByTestId("account-pw-error")).toContainText(
      "Passwords do not match.",
    );
    // Brief settle window — give the (absent) network call a chance to fire.
    await page.waitForTimeout(50);
    expect(called).toBe(false);
  });

  test("surfaces the server detail on a 400 (old password incorrect)", async ({ page }) => {
    await page.route("**/api/auth/password", async (route) => {
      await route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({ detail: "old password is incorrect" }),
      });
    });

    await page.goto("/settings/login-security");

    await page.getByTestId("account-old-password").fill("wrongpw");
    await page.getByTestId("account-new-password").fill("newpw5678");
    await page.getByTestId("account-confirm-password").fill("newpw5678");
    await page.getByTestId("account-change-password-submit").click();

    await expect(page.getByTestId("account-pw-error")).toContainText(
      "old password is incorrect",
    );
    // Fields retain their values so the user can correct the old password.
    await expect(page.getByTestId("account-new-password")).toHaveValue("newpw5678");
  });
});
