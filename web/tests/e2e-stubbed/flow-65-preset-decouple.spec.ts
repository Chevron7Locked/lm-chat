/**
 * Flow 65 — Preset/mode decoupling (2026-06-20).
 *
 * Proves the new model (composer badge removed 2026-06-20):
 *   (a) Typing /research starts the sub-session (sub-session panel visible)
 *       and composer-preset-badge is NEVER in the DOM — slash commands do
 *       not write active_preset and no badge renders under any condition.
 *   (b) Setting a system prompt via the rail picker persists across a plain
 *       send: the next /api/chat/stream payload carries the preset's
 *       system_prompt; composer-preset-badge is NEVER in the DOM.
 *
 * Route-stubbed; auth bypassed via probe/me stubs (no login page).
 */
import { test, expect, type Route } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const AUTH_ME = {
  user_id: 1,
  username: "alice",
  is_admin: false,
  expires_at: "2026-12-01T00:00:00Z",
};

function buildSse(rid: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ response_id: rid })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ delta: "reply" })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ stop_reason: "stop" })}\n\n`
  );
}

function buildSubSse(rid: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ response_id: rid })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ delta: "sub-reply" })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ stop_reason: "stop" })}\n\n`
  );
}

async function stubCommon(
  page: Parameters<Parameters<typeof test["beforeEach"]>[0]>[0],
  chatId: number,
) {
  // Authed chat-page bootstrap defaults (probe hydration + correctly-typed
  // list/object endpoints). Replaces the old `**/api/**` → {} catch-all,
  // which shadowed array endpoints (e.g. /api/documents) and crashed the page.
  await bootstrapAuthedApp(page);
  await page.route("**/api/chats/*/rag_mode", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mode: "inline", source: "default", project_id: null }),
    }),
  );
  await page.route("**/api/auth/me/probe", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...AUTH_ME,
        needs_setup: false,
        totp_enabled: false,
        email: null,
        display_name: null,
        avatar_url: null,
      }),
    }),
  );
  await page.route("**/api/auth/setup_status", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ needs_setup: false }),
    }),
  );
  await page.route("**/api/auth/me", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(AUTH_ME),
    }),
  );
  await page.route("**/api/settings/lmstudio", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        base_url: "http://localhost:1234",
        default_model: "qwen3",
        api_key_set: false,
        source_base_url: "unset",
        source_api_key: "unset",
        source_default_model: "unset",
        key_pruned: false,
        auth_failed: false,
      }),
    }),
  );
  await page.route("**/api/folders", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/projects", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/quotas/me", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        tokens_per_day: 50000,
        requests_per_day: 500,
        tokens_consumed_today: 0,
        requests_consumed_today: 0,
        resets_at: "2026-12-02T00:00:00Z",
      }),
    }),
  );
  await page.route("**/api/prompts", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/integrations/available", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/memory/pins", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/models", (route: Route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          key: "qwen3",
          display_name: "Qwen 3",
          provider: "lmstudio",
          loaded_instances: 1,
          loaded_instance_ids: ["qwen3"],
          capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
          max_context_length: 8192,
          loaded_context_length: 0,
          size_bytes: 0,
          params_string: "",
          quantization: null,
        },
      ]),
    });
  });
  await page.route("**/api/chats**", async (route: Route) => {
    const method = route.request().method();
    const path = new URL(route.request().url()).pathname;

    if (method === "PATCH" && path === `/api/chats/${String(chatId)}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: chatId, user_id: 1, title: "Decouple test",
          folder: null, pinned: false,
          created_at: "2026-06-01T12:00:00Z",
          updated_at: "2026-06-01T12:00:00Z",
          settings: {}, display_order: 0,
        }),
      });
    }
    if (method === "GET" && path === "/api/chats") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: chatId, user_id: 1, title: "Decouple test",
            folder: null, pinned: false,
            created_at: "2026-06-01T12:00:00Z",
            updated_at: "2026-06-01T12:00:00Z",
            settings: {}, display_order: 0,
            incognito: false, incognito_expires_at: null, model_id: "qwen3",
          },
        ]),
      });
    }
    if (method === "GET" && path === `/api/chats/${String(chatId)}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: chatId, user_id: 1, title: "Decouple test",
          messages: [], has_more: false,
        }),
      });
    }
    return route.fallback();
  });
}

test.describe("Flow 65 — Preset/mode decoupling", () => {
  test(
    "(a) /research starts sub-session — Composer preset badge does NOT appear",
    async ({ page }) => {
      const chatId = 621;
      await stubCommon(page, chatId);

      // Capture any PATCH to chats/:id to verify active_preset is NOT written.
      const patchBodies: string[] = [];
      await page.route(`**/api/chats/${String(chatId)}`, async (route: Route) => {
        if (route.request().method() === "PATCH") {
          patchBodies.push(route.request().postData() ?? "");
        }
        return route.fallback();
      });

      await page.route("**/api/chats/*/sub-session/stream", (route: Route) => {
        if (route.request().method() !== "POST") return route.fallback();
        return route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: buildSubSse("rid-621"),
        });
      });

      await page.goto(`/chats/${String(chatId)}`);
      const composer = page.getByRole("textbox", { name: "Message" });
      await expect(composer).toBeVisible({ timeout: 10_000 });

      // Type /research and submit — should start sub-session only.
      await composer.fill("/research");
      await composer.press("Control+Enter");

      // Sub-session panel appears (the sub-agent launched successfully).
      await expect(page.locator(".lmchat-subsession-label")).toBeVisible({
        timeout: 8_000,
      });
      await expect(page.locator(".lmchat-subsession-label")).toContainText(
        /Research/i,
      );

      // composer-preset-badge must NEVER be in the DOM — badge removed entirely.
      await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

      // No PATCH with active_preset should have fired.
      // Allow 1 s for any async drain before asserting.
      await page.waitForTimeout(1_000);
      const presetPatches = patchBodies.filter((b) =>
        b.includes("active_preset"),
      );
      expect(presetPatches).toHaveLength(0);
    },
  );

  test(
    "(b) rail preset picker persists system_prompt across a plain send — no badge ever renders",
    async ({ page }) => {
      const chatId = 622;
      await stubCommon(page, chatId);

      // Capture each /api/chat/stream body so we can assert system_prompt.
      let lastStreamBody: unknown = null;
      await page.route("**/api/chat/stream", async (route: Route) => {
        try {
          lastStreamBody = JSON.parse(route.request().postData() ?? "{}");
        } catch {
          lastStreamBody = null;
        }
        return route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: buildSse("rid-622"),
        });
      });

      // Track PATCH active_preset calls so we can assert it fired.
      let patchedPreset: string | null = null;
      await page.route(`**/api/chats/${String(chatId)}`, async (route: Route) => {
        if (route.request().method() === "PATCH") {
          const params = new URLSearchParams(route.request().postData() ?? "");
          const ap = params.get("active_preset");
          if (ap !== null) patchedPreset = ap;
        }
        return route.fallback();
      });

      await page.goto(`/chats/${String(chatId)}`);
      const composer = page.getByRole("textbox", { name: "Message" });
      await expect(composer).toBeVisible({ timeout: 10_000 });

      // Open settings rail via the overflow menu (desktop layout) or Tune
      // button (mobile layout).  Try overflow first; fall back to Tune.
      const overflowTrigger = page.getByTestId("topbar-overflow-trigger");
      const tuneBtn = page.getByRole("button", { name: "Tune" });
      const overflowVisible = await overflowTrigger.isVisible().catch(() => false);
      if (overflowVisible) {
        await overflowTrigger.click();
        await page.getByRole("menuitem", { name: "Chat settings" }).click();
      } else {
        await tuneBtn.click();
      }
      const presetSelect = page.getByTestId("chat-settings-preset");
      await expect(presetSelect).toBeVisible({ timeout: 6_000 });
      await presetSelect.selectOption("research");

      // Backend PATCH must fire with active_preset=research.
      await expect
        .poll(() => patchedPreset, { timeout: 6_000 })
        .toBe("research");

      // composer-preset-badge must NEVER appear in the DOM (badge removed).
      await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

      // Send a plain message — stream payload must carry the research system_prompt.
      await composer.fill("what is quantum entanglement");
      await composer.press("Control+Enter");

      // Wait for the stream request to land.
      await expect
        .poll(() => lastStreamBody !== null, { timeout: 8_000 })
        .toBe(true);

      // The stream payload must carry the research preset's system_prompt.
      const body = lastStreamBody as {
        payload?: { system_prompt?: string };
      };
      const sysPrompt = body.payload?.system_prompt ?? "";
      expect(sysPrompt).toContain("Research mode");

      // Badge still absent after the send.
      await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);
    },
  );
});
