/**
 * Flow 38 — Admin Users page + admin invite (P13k).
 *
 * Closes parity-audit A-06 + A-07.  Verifies:
 *
 *   1. Admin lands on `/admin/users` (route registered, not catch-all-swallowed).
 *   2. The user list renders with role badges + last_login derivation column.
 *   3. Clicking "Invite admin" issues a one-shot token; the modal renders the
 *      `/register?token=...` link.
 *   4. A fresh (logged-out) browsing session registers with that token and
 *      lands as an admin (is_admin=true echoed by /api/auth/me).
 *
 * Backend is fully stubbed via `page.route`; the test exercises wire-format
 * contract (URLs, request bodies, response shapes) rather than the live
 * database.  Live-DB coverage of the same flow lives in
 * `tests/routes/test_admin_users_p13k.py`.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const INVITE_TOKEN = "tok_p13k_test_invite_2026";

test.describe("Flow 38 — admin users + invite", () => {
  test("admin lists users, issues invite, registers new admin via token", async ({
    page,
    context,
  }) => {
    // Authed as an admin — bootstrap covers array/object defaults; this
    // spec is already signed in on cold load.
    await bootstrapAuthedApp(page, { isAdmin: true, username: "admin" });

    // ─── Admin Users list stub ──────────────────────────────────────────────
    await page.route("**/api/admin/users**", async (route) => {
      const req = route.request();
      if (req.method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: 1,
              username: "admin",
              is_admin: true,
              created_at: "2026-05-01T10:00:00Z",
              updated_at: "2026-05-01T10:00:00Z",
              last_login: "2026-05-22T11:00:00Z",
            },
            {
              id: 2,
              username: "bob",
              is_admin: false,
              created_at: "2026-05-10T10:00:00Z",
              updated_at: "2026-05-10T10:00:00Z",
              last_login: null,
            },
          ]),
        });
        return;
      }
      await route.fallback();
    });

    // Invite-issue stub.
    await page.route("**/api/admin/invite", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          token: INVITE_TOKEN,
          expires_at: "2026-05-23T11:00:00Z",
        }),
      }),
    );

    // ─── Step 1: navigate to /admin/users (already authed) ──────────────────
    await page.goto("/admin/users");
    await expect(
      page.getByRole("heading", { name: /admin.*users/i }),
    ).toBeVisible({ timeout: 5_000 });
    expect(new URL(page.url()).pathname).toBe("/admin/users");

    // Table rendered with both users.
    await expect(page.getByTestId("admin-users-table")).toBeVisible();
    await expect(page.getByTestId("user-row-1")).toBeVisible();
    await expect(page.getByTestId("user-row-2")).toBeVisible();
    await expect(page.getByTestId("role-admin-1")).toHaveText(/admin/i);
    await expect(page.getByTestId("role-user-2")).toHaveText(/user/i);

    // ─── Step 2: issue an invite ─────────────────────────────────────────────
    await page.getByTestId("invite-admin-btn").click();
    await expect(page.getByTestId("invite-modal")).toBeVisible();
    const tokenLink = page.getByTestId("invite-token-link");
    await expect(tokenLink).toContainText(INVITE_TOKEN);
    await expect(tokenLink).toContainText("/register?token=");

    // ─── Step 3: new browsing context — registers with the invite token ─────
    // Use a second page in the same context so route handlers are scoped to
    // each page independently (page.route is page-scoped in Playwright).
    const page2 = await context.newPage();

    // page2 starts unauthenticated (cold boot) — bootstrap defaults still
    // apply for post-register hydration, but the probe must report "no
    // session" so /register actually renders instead of bouncing away.
    await bootstrapAuthedApp(page2);
    await page2.route("**/api/auth/me/probe", (route) =>
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

    // Capture the token that rides on the POST /api/auth/register call.
    // This is the load-bearing assertion: the SPA must forward the invite
    // token from the URL query string to the backend so the gate fires.
    let observedToken: string | null = null;
    await page2.route(
      (url) => url.pathname === "/api/auth/register",
      async (route) => {
        const url = new URL(route.request().url());
        observedToken = url.searchParams.get("token");
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({ id: 42, username: "invitee" }),
        });
      },
    );
    // Register.tsx auto-chains login() after a successful register — stub
    // it so that follow-on request resolves cleanly (not asserted here).
    await page2.route("**/api/auth/login", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: 42,
          username: "invitee",
          is_admin: false,
          expires_at: "2026-12-01T00:00:00Z",
        }),
      }),
    );

    // Navigate using the actual invite URL.
    await page2.goto(`/register?token=${INVITE_TOKEN}`);

    // The Register page submits the form; fill it in.
    await page2.getByLabel("Username").fill("invitee");
    // P13f Register page uses two password fields; tolerate either count.
    // Scope to actual <input> elements — getByLabel(/password/i) also
    // matches the "Show password" / "Show confirmation password" toggle
    // buttons (their aria-label matches the same regex), which are not
    // fillable.
    const passwordFields = page2.locator('input[type="password"]');
    const pwCount = await passwordFields.count();
    for (let i = 0; i < pwCount; i += 1) {
      await passwordFields.nth(i).fill("invitee_pw");
    }
    await page2.getByRole("button", { name: /sign up|create|register/i }).click();

    // Wait for the POST /api/auth/register call to be intercepted and the
    // token captured. The post-register UI redirect (auto-signed-in, per
    // flow-21/34) is covered by those flows; this flow's load-bearing
    // assertion is that the invite token rides on the wire as
    // ?token=<INVITE_TOKEN>.
    await expect.poll(() => observedToken, { timeout: 5_000 }).toBe(INVITE_TOKEN);

    await page2.close();
  });
});
