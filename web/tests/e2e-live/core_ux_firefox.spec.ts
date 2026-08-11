/**
 * §1.7 — Firefox parity smoke (live).
 *
 * `playwright.live.config.ts` now ships both `chromium` and `firefox`
 * projects. This file is the load-bearing reason: a tiny smoke that
 * forces both engines through the full auth + chat-shell render. If
 * Firefox stops parsing a stylesheet, a polyfill silently breaks, or
 * the auth cookie shape stops being SameSite-compatible, this test
 * catches it.
 *
 * The chat-feature parity tests still live under `legacy-parity.spec.ts`
 * and the broader live suite — those already run on every configured
 * project. This file is the minimum signal that the Firefox runner is
 * wired and exercising the live backend.
 */
import { test, expect } from "./_fixtures";

test.describe("§1.7 — Firefox parity smoke", () => {
  test("login + home shell render across configured browsers", async ({
    page,
    backendURL,
    testUsername,
    testPassword,
  }) => {
    await page.goto(backendURL);
    await page.getByLabel("Username").fill(testUsername);
    await page.locator("#lmchat-password").fill(testPassword);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL(`${backendURL}/`, { timeout: 15_000 });

    // The home shell mounts the chat-header model select even with no
    // chat selected — rule 1 of §1.7's core UX rules. This is the
    // load-bearing structural assertion the smoke confirms across
    // every configured browser project.
    await expect(
      page.getByTestId("chat-header-model-select")
    ).toBeVisible({ timeout: 10_000 });
  });
});
