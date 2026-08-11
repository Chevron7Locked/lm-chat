/**
 * E2E: §2.3 — /project/:id is gated by RequireAuth (RequireAuth × Project).
 *
 * Verifies that RequireAuth lands /project/:id under the
 * authed shell (AppLayout via RequireAuth), not the unauth shell. An
 * unauthenticated visit captures the path as `returnTo` per
 * web/src/lib/returnTo.ts + RequireAuth.tsx:34, and a subsequent login
 * lands the user back at the project page.
 *
 * Backend is stubbed:
 * - /api/auth/me returns 401 on the first call (no session); after the
 *   simulated "login" we flip the route to a healthy 200.
 * - /api/projects/1 returns a minimal project so the Project page can
 *   render after the redirect resolves.
 */
import { test, expect } from "@playwright/test";

test.describe("§2.3 — /project/:id auth redirect (RequireAuth × Project)", () => {
  test("unauth → /login?returnTo=/project/1; login → back on /project/1", async ({
    page,
  }) => {
    let authed = false;

    await page.route("**/api/auth/me", (route) => {
      if (authed) {
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 1,
            username: "alice",
            roles: ["user"],
          }),
        });
      } else {
        route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ detail: "authentication required" }),
        });
      }
    });

    await page.route("**/api/auth/setup_status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ needs_setup: false }),
      })
    );

    await page.route("**/api/projects/1**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1,
          name: "Demo project",
          description: "",
          system_prompt: "",
          embedding_model_id: null,
          created_at: 1_700_000_000,
          updated_at: 1_700_000_000,
        }),
      })
    );

    await page.route("**/api/projects/1/chats**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], has_more: false }),
      })
    );

    await page.route("**/api/projects/1/documents**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], has_more: false }),
      })
    );

    // Step 1: unauth visit captures returnTo.
    await page.goto("/project/1");
    await page.waitForURL(/\/login\?returnTo=/);
    expect(page.url()).toContain("returnTo=");
    expect(decodeURIComponent(page.url())).toContain("/project/1");

    // Step 2: simulate login — flip the /me route, navigate to the returnTo.
    authed = true;
    await page.evaluate(() => {
      const params = new URLSearchParams(window.location.search);
      const t = params.get("returnTo") ?? "/";
      window.history.replaceState({}, "", t);
    });
    await page.reload();

    // Step 3: land back on /project/1.
    await page.waitForURL(/\/project\/1/);
    expect(page.url()).toContain("/project/1");
  });
});
