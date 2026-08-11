/**
 * Flow 14 — TOTP enable flow via Settings UI + login challenge.
 *
 * What it proves (route-stubbed):
 *  1. Settings.tsx renders a Security section with an "Enable two-factor
 *     authentication" button.  Clicking it calls POST /api/auth/totp/setup
 *     and the returned secret + provisioning URI render in the panel.
 *  2. Entering a 6-digit code + clicking Verify calls POST /api/auth/totp/verify
 *     with the secret + code.  On success the section flips to the
 *     "TOTP enabled" state showing a Disable button.
 *  3. After setup, a fresh login WITHOUT a totp_code returns
 *     401 "totp required" — Login.tsx reveals the Authenticator-code
 *     field in response.
 *  4. Submitting the same form with a valid 6-digit code completes the
 *     login and navigates to "/".
 *
 * Gap 2 (P10c.2): replaces the previous wire-contract-only stub flow
 * that drove /totp/setup + /totp/verify via page.evaluate().  The
 * Settings TOTP surface now exists and the spec drives it directly.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 14 — Settings TOTP enable + login challenge", () => {
  test("Settings UI enables TOTP; login enforces the challenge", async ({ page }) => {
    // Correctly-typed defaults for the post-login chat-page cold load.
    // Probe is overridden to a null user below: every `page.goto` in this
    // test is a cold boot, and this test always wants a fresh, signed-out
    // boot (it drives the real login form repeatedly, including the TOTP
    // challenge round-trip) — later authenticated views are reached via
    // client-side navigation, which never re-hits the probe.
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

    // ─── Step 1: stub the TOTP endpoints. ───────────────────────────────────
    let totpEnabled = false;

    await page.route("**/api/auth/totp/setup", (route) => {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          provisioning_uri:
            "otpauth://totp/lmchat:alice?secret=JBSWY3DPEHPK3PXP&issuer=lmchat",
          secret: "JBSWY3DPEHPK3PXP",
        }),
      });
    });

    await page.route("**/api/auth/totp/verify", async (route) => {
      const body = route.request().postData() ?? "";
      const params = new URLSearchParams(body);
      const code = params.get("code");
      if (code !== null && /^\d{6}$/.test(code)) {
        totpEnabled = true;
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "ok" }),
        });
      }
      return route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({ detail: "invalid code" }),
      });
    });

    // ─── Step 2: login route — branches on totp_code presence. ──────────────
    await page.route("**/api/auth/login", async (route) => {
      const body = route.request().postData() ?? "";
      const params = new URLSearchParams(body);
      const totpCode = params.get("totp_code");
      if (totpEnabled && totpCode === null) {
        // Server demands a TOTP code now that setup has completed.
        return route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ detail: "totp required" }),
        });
      }
      if (totpEnabled && totpCode !== null && !/^\d{6}$/.test(totpCode)) {
        return route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ detail: "invalid totp code" }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: 1, expires_at: "2026-12-01T00:00:00Z",
          username: "alice", is_admin: false,
          totp_enabled: totpEnabled,
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
          user_id: 1,
          username: "alice",
          is_admin: false,
          // SA-gaps: /me carries totp_enabled so SecuritySettings can
          // hydrate the correct state on mount + after reload.
          totp_enabled: totpEnabled,
        }),
      })
    );

    // ─── Step 3: log in once, open Settings, run the TOTP enable flow. ──────
    await page.goto("/login");
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("pass");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL("**/");

    // P13a: global Settings is now a tabbed page; TOTP lives on the
    // Security tab.  Navigate via the UserMenu (client-side router) rather
    // than a hard page.goto — a hard nav would re-run the cold-load probe,
    // which is stubbed to always report "no session" above, and would
    // bounce straight back to /login before Settings ever mounted.
    await page.getByTestId("user-menu-avatar").click();
    await page.getByTestId("user-menu-settings").click();
    await page.waitForURL((u) => u.pathname.startsWith("/settings"), { timeout: 5_000 });
    await expect(page.getByTestId("settings-page")).toBeVisible({ timeout: 10_000 });
    await page.getByTestId("settings-tab-login-security").click();
    // Settings tabs follow the ARIA tabs pattern (aria-selected on the
    // active <button role="tab">), not aria-current.
    await expect(page.getByTestId("settings-tab-login-security")).toHaveAttribute(
      "aria-selected", "true", { timeout: 5_000 }
    );

    // Click the Enable-TOTP button.
    const enableBtn = page.getByRole("button", {
      name: /Enable two-factor authentication/,
    });
    await expect(enableBtn).toBeVisible();
    await enableBtn.click();

    // The setup card should appear with the secret rendered in the
    // dedicated "TOTP secret" <code> element (the URI also contains the
    // secret as a substring; we assert against the labelled element to
    // avoid a strict-mode multi-match).
    await expect(
      page.getByLabel("TOTP secret", { exact: true })
    ).toContainText("JBSWY3DPEHPK3PXP", { timeout: 5_000 });

    // Enter a 6-digit code and verify.
    await page.getByLabel("Authenticator code").fill("123456");
    await page.getByRole("button", { name: "Verify TOTP code" }).click();

    // After verify, the section flips to "TOTP enabled" state.
    await expect(
      page.getByRole("button", { name: /Disable two-factor authentication/ })
    ).toBeVisible({ timeout: 5_000 });

    // ─── Step 4: log out, then log in WITHOUT TOTP → 401 + reveal field. ────
    // (No logout UI flow needed — the route stub flips totpEnabled state
    // independently.  Navigate to /login directly to start a new session;
    // the probe is always stubbed unauthenticated, so this cold boot lands
    // on the real login form regardless of the prior in-memory session.)
    await page.goto("/login");
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("pass");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByLabel("Authenticator code")).toBeVisible({
      timeout: 5_000,
    });

    // ─── Step 5: enter the code → login succeeds, navigate to "/". ──────────
    await page.getByLabel("Authenticator code").fill("654321");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL("**/", { timeout: 5_000 });
    // Wait for the authStore to settle (the Chat page UI proves /me has
    // returned and user.totp_enabled is hydrated to true).  2026-06-13
    // redesign replaced the "Open settings" button with the ⋯ overflow
    // trigger (see chat.spec.ts for the same pattern).
    await expect(page.getByTestId("topbar-overflow-trigger")).toBeVisible({ timeout: 5_000 });

    // ─── Step 6: reload + reopen Settings → TOTP shows "enabled" from /me. ──
    // SA-gaps regression guard: SecuritySettings used to reset to
    // "not configured" on every mount because /me did not carry the
    // TOTP state.  /api/auth/me now returns `totp_enabled: true` (the
    // route stub above mirrors `totpEnabled`, which is `true` after
    // step 3), so the component hydrates the "enabled" state directly.
    //
    // P13a: TOTP lives on the global Settings page Security tab now.
    // Navigate via UserMenu so the SPA's internal router fires (not a
    // hard page reload — which would re-trigger AuthHydrator and race
    // SecuritySettings mount against /me).
    await page.getByTestId("user-menu-avatar").click();
    await page.getByTestId("user-menu-settings").click();
    await page.waitForURL((u) => u.pathname.startsWith("/settings"), { timeout: 5_000 });
    await expect(page.getByTestId("settings-page")).toBeVisible({ timeout: 10_000 });
    await page.getByTestId("settings-tab-login-security").click();
    await expect(page.getByTestId("settings-tab-login-security")).toHaveAttribute(
      "aria-selected", "true", { timeout: 5_000 }
    );
    await expect(
      page.getByRole("button", { name: /Disable two-factor authentication/ })
    ).toBeVisible({ timeout: 5_000 });
    // And the "Enable two-factor authentication" CTA must NOT be present —
    // that is the symptom the SA-gaps fix removed.
    await expect(
      page.getByRole("button", { name: /^Enable two-factor authentication$/ })
    ).toHaveCount(0);
  });
});
