/**
 * Flow 13 — Logout state sweep.
 *
 * What it proves (route-stubbed):
 *  1. Login as user A; the auth store transitions to authenticated and
 *     the sidebar renders user A's chats.
 *  2. After a stream completes, an SSE response-id key is persisted to
 *     localStorage under `lmchat:sse:*`.  We seed this directly to make
 *     the assertion deterministic.
 *  3. On logout (POST /api/auth/logout), authStore.logout() sweeps every
 *     `lmchat:sse:*` key from localStorage.  We assert the sweep ran.
 *  4. Login as user B — the sidebar shows user B's chats (different stub
 *     payload), with no user A chats leaking through.  This proves
 *     cross-user state isolation: the SSE keys swept above would
 *     otherwise have surfaced user A's response-id chain when user B
 *     sent their first message.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 13 — Logout state sweep + cross-user isolation", () => {
  test("logout sweeps lmchat:sse:* keys; user B sees no user A chats", async ({ page }) => {
    let activeUser: "A" | "B" = "A";

    // Correctly-typed defaults for the post-login chat-page cold load.
    // Probe is overridden to a null user below: this test drives the real
    // login form from a cold, unauthenticated boot.
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

    // Auth — sets the user based on the username field; the test only
    // ever submits "alice" (user A) or "bob" (user B).
    await page.route("**/api/auth/login", async (route) => {
      const body = route.request().postData() ?? "";
      const params = new URLSearchParams(body);
      const username = params.get("username");
      activeUser = username === "bob" ? "B" : "A";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: activeUser === "A" ? 1 : 2,
          expires_at: "2026-12-01T00:00:00Z",
          username,
          is_admin: false,
        }),
      });
    });

    await page.route("**/api/auth/logout", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "ok" }),
      })
    );

    await page.route("**/api/auth/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: activeUser === "A" ? 1 : 2,
          username: activeUser === "A" ? "alice" : "bob",
          is_admin: false,
        }),
      })
    );

    // Chats list — branch on activeUser so we serve different rows.
    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        const userAChats = [
          {
            id: 11, title: "Alice Chat", folder: null, pinned: false,
            updated_at: "2026-01-01T00:00:00Z", model_id: null,
            display_order: 0, settings: {},
          },
        ];
        const userBChats = [
          {
            id: 99, title: "Bob Chat", folder: null, pinned: false,
            updated_at: "2026-01-01T00:00:00Z", model_id: null,
            display_order: 0, settings: {},
          },
        ];
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(activeUser === "A" ? userAChats : userBChats),
        });
      }
      return route.continue();
    });

    // ─── Step 1: login as user A ────────────────────────────────────────────
    await page.goto("/login");
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("pass");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL("**/");

    // The sidebar should show "Alice Chat".
    await expect(page.getByText("Alice Chat")).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText("Bob Chat")).not.toBeVisible();

    // ─── Step 2: seed an lmchat:sse:* key as if a stream had completed. ───
    await page.evaluate(() => {
      localStorage.setItem("lmchat:sse:11:rid", "resp-from-alice-stream");
      localStorage.setItem("lmchat:sse:11:msg_id", "42");
    });
    const seededKeys = await page.evaluate(() => {
      return Object.keys(localStorage).filter((k) => k.startsWith("lmchat:sse:"));
    });
    expect(seededKeys.length).toBeGreaterThanOrEqual(2);

    // ─── Step 3: open settings panel and sign out via Settings. ─────────────
    // Navigate via the UserMenu (client-side router) rather than a hard
    // page.goto("/settings") reload — a hard nav would re-run the app's
    // cold-load probe, which we've deliberately stubbed to always report
    // "no session" (see above), and would bounce back to /login before the
    // Settings page ever mounted.
    await page.getByTestId("user-menu-avatar").click();
    await page.getByTestId("user-menu-settings").click();
    await page.waitForURL((u) => u.pathname.startsWith("/settings"), { timeout: 5_000 });
    // Sign-out lives in AccountSection, rendered under the "Login & Security"
    // tab (id "login-security") — not the default "Profile" tab.
    await page.getByTestId("settings-tab-login-security").click();

    // (The UserMenu's "Sign out" path is harder to drive headlessly; the
    // Settings panel exposes the same handler.)
    // UX-AUDIT-r1 Wave 3-H: two-step sign-out confirmation.  First click
    // pivots the button to "Yes, sign out" + Cancel; second click on the
    // confirmed-state button commits the logout.
    await page.getByTestId("settings-account-signout").click();
    await page.getByTestId("settings-account-signout-confirm").click();
    // After logout the user is redirected to /login (client-side —
    // RequireAuth's <Navigate>, not a reload).
    await page.waitForURL("**/login", { timeout: 5_000 });

    // ─── Step 4: assert localStorage sweep ran. ────────────────────────────
    const remainingKeys = await page.evaluate(() => {
      return Object.keys(localStorage).filter((k) => k.startsWith("lmchat:sse:"));
    });
    expect(remainingKeys).toEqual([]);

    // ─── Step 5: login as user B and verify no user A chats leak. ──────────
    await page.getByLabel("Username").fill("bob");
    await page.getByLabel("Password", { exact: true }).fill("pass");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL("**/");

    await expect(page.getByText("Bob Chat")).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText("Alice Chat")).not.toBeVisible();
  });
});
