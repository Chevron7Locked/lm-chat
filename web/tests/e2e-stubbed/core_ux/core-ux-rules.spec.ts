/**
 * §1.7 — core UX rules (Playwright, stubbed).
 *
 * Four rules from the core UX grammar
 * (feedback_lm_chat_core_ux_rules_2026_05_28.md):
 *
 *   1. Model picker is a real <select> (not a custom dropdown).
 *   2. Enter sends; Shift+Enter inserts a newline.
 *   3. The user's message renders optimistically (before the SSE
 *      stream reply lands).
 *   4. No dead-end routes — every URL either renders something or
 *      redirects somewhere productive.
 *
 * Rules 1 and 4 exercise structural guarantees the Playwright stubbed
 * harness can verify cheaply: the <select> mounts on the chat-header
 * shell, and the router catch-all redirects unknown paths to /. Both
 * pass against the route-stubbed harness without an active chat.
 *
 * Rules 2 and 3 require a chat-loaded view (composer + message list)
 * and are pinned by the existing component unit tests under
 * web/tests/unit/:
 *
 *   - `test_Composer_integrations_picker.spec.tsx` — exercises the
 *     Composer's keyDown handler at Composer.tsx:236+254 with Enter +
 *     Cmd+Enter combinations against React Testing Library's DOM.
 *   - `test_ChatMessage_edit_regenerate.spec.tsx` — covers the
 *     optimistic-render path through `useStreamingMutation`.
 *
 * Keeping rules 2 + 3 at the unit-test layer (where the contract IS
 * the component) gives a tighter regression than re-asserting them
 * through Playwright's full-shell stub harness, which would require
 * mocking every adjacent API surface (folders/documents/prompts/
 * projects/integrations/...) the chat shell touches at mount.
 */
import { test, expect, type Page } from "@playwright/test";
import { bootstrapAuthedApp } from "../_bootstrap";

async function wireStubs(page: Page): Promise<void> {
  // Correctly-typed defaults for the post-login chat-page cold load.
  let loggedIn = false;
  await bootstrapAuthedApp(page);
  // Probe reflects actual login state (not a static shape): rule 4's test
  // does a hard page.goto() AFTER logging in, which re-runs the cold-load
  // probe — it must report "authenticated" then, or the unknown-route
  // redirect would land on /login instead of "/".
  await page.route("**/api/auth/me/probe", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        loggedIn
          ? {
              user_id: 1, username: "alice", is_admin: false,
              needs_setup: false, totp_enabled: false,
            }
          : {
              user_id: null, username: null, is_admin: false,
              needs_setup: false, totp_enabled: false,
            },
      ),
    })
  );
  await page.route("**/api/auth/login", (route) => {
    loggedIn = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: 1,
        expires_at: "2026-12-01T00:00:00Z",
        username: "alice",
        is_admin: false,
        totp_enabled: false,
      }),
    });
  });
  await page.route("**/api/auth/me", (route) => {
    if (!loggedIn) {
      return route.fulfill({ status: 401, body: "" });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: 1,
        username: "alice",
        is_admin: false,
      }),
    });
  });
  await page.route("**/api/auth/setup_status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ needs_setup: false }),
    })
  );
  await page.route("**/api/chats", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: 1,
          title: "Core UX",
          folder: null,
          pinned: false,
          updated_at: new Date().toISOString(),
          model_id: "qwen",
          display_order: 0,
          settings: {},
        },
      ]),
    });
  });
  await page.route("**/api/chats/1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: 1,
        user_id: 1,
        title: "Core UX",
        folder: null,
        pinned: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        messages: [],
        has_more: false,
        settings: {},
      }),
    })
  );
  // /api/models is left to bootstrap's default (correctly-shaped
  // key/display_name/loaded_instances wire fields) — this spec's old
  // stub used the UI shape (id/name/loaded), which left the normalized
  // ModelInfo.id undefined and crashed isEmbedding()'s
  // `m.id.toLowerCase()` check.
  await page.route("**/api/memory/pins", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    })
  );
  await page.route("**/api/quota/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        requests_consumed_today: 0,
        requests_per_day: 1000,
        tokens_consumed_today: 0,
        tokens_per_day: 1_000_000,
        resets_at: new Date(Date.now() + 86_400_000).toISOString(),
      }),
    })
  );
  for (const path of [
    "**/api/folders",
    "**/api/documents",
    "**/api/prompts",
    "**/api/projects",
    "**/api/integrations",
    "**/api/memory/insights/recent",
  ]) {
    await page.route(path, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      })
    );
  }
}

async function loginAndOpenHome(page: Page): Promise<void> {
  await page.context().clearCookies();
  await page.goto("/login");
  await page.getByLabel("Username").fill("alice");
  await page.getByLabel("Password", { exact: true }).fill("correct");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), {
    timeout: 10_000,
  });
}

test.describe("§1.7 — core UX rules", () => {
  test("rule 1 — chat-header model picker is a real <select>", async ({
    page,
  }) => {
    await wireStubs(page);
    await loginAndOpenHome(page);

    const picker = page.getByTestId("chat-header-model-select");
    await expect(picker).toBeVisible({ timeout: 10_000 });
    const tagName = await picker.evaluate((el) => el.tagName.toLowerCase());
    expect(tagName).toBe("select");
  });

  test("rule 4 — unknown route redirects to home, never a blank screen", async ({
    page,
  }) => {
    await wireStubs(page);
    await loginAndOpenHome(page);

    await page.goto("/this-route-does-not-exist");
    await page.waitForURL("**/");
    expect(new URL(page.url()).pathname).toBe("/");
  });
});
