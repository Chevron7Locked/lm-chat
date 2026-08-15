/**
 * Flow 61 — Preset model routing.
 *
 * What it proves (route-stubbed):
 *   1. An admin assigns an OpenRouter model to the "research" preset via
 *      Settings → Preset models (the PUT /api/settings/preset-models fires
 *      with the correct provider+model_id mapping).
 *   2. Launching /research in a chat opens a sub-session that carries the
 *      assigned model_id and provider on the wire (POST sub-session/stream
 *      form body contains "openrouter" + the model id).
 *
 * Wire path (same as flow-53):
 *   Composer /research slash command → onPresetActivate → Chat.tsx
 *   startSubSession → POST /api/chats/:id/sub-session/stream (multipart/
 *   form-data). The sub-session picks up the preset model from the
 *   /api/settings/preset-models response.
 *
 * Source:
 *   web/src/components/PresetModelsSection.tsx
 *   web/src/hooks/usePresetModels.ts  — PUT /api/settings/preset-models
 *   web/src/lib/presets.ts            — PRESET_LIST ids ("research", "coder"…)
 */
import { test, expect, type Route } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const PRESET_MODEL_ID = "openai/gpt-4o-mini";
const PRESET_PROVIDER = "openrouter";
const COMPOSITE = `${PRESET_PROVIDER}::${PRESET_MODEL_ID}`;

/** Minimal SSE response for a sub-session stream. */
function buildSse(rid: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ response_id: rid })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ delta: "pong" })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ stop_reason: "stop" })}\n\n`
  );
}

async function stubCommon(
  page: Parameters<Parameters<typeof test["beforeEach"]>[0]>[0],
  chatId: number,
  presetModelsMap: Record<string, { provider: string; model_id: string }>,
) {
  // Authed as an admin — bootstrap covers array/object defaults; this
  // spec is already signed in on cold load.
  await bootstrapAuthedApp(page, { isAdmin: true, username: "admin" });

  await page.route("**/api/providers/status", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );

  await page.route("**/api/settings/preset-models", (route: Route) => {
    const method = route.request().method();
    if (method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(presetModelsMap),
      });
    }
    if (method === "PUT") {
      // Echo back whatever the FE sends.
      const body = route.request().postData() ?? "{}";
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body,
      });
    }
    return route.fallback();
  });

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
        tokens_per_day: 100_000,
        requests_per_day: 1_000,
        tokens_consumed_today: 0,
        requests_consumed_today: 0,
        resets_at: "2026-12-01T00:00:00Z",
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
  await page.route("**/api/providers/status", (route: Route) =>
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
          capabilities: {
            vision: false,
            trained_for_tool_use: false,
            reasoning: null,
            embedding: false,
          },
          max_context_length: 8192,
          loaded_context_length: 0,
          size_bytes: 0,
          params_string: "",
          quantization: null,
        },
        {
          key: PRESET_MODEL_ID,
          display_name: "GPT-4o mini",
          provider: PRESET_PROVIDER,
          loaded_instances: 0,
          loaded_instance_ids: [],
          capabilities: {
            vision: true,
            trained_for_tool_use: true,
            reasoning: null,
            embedding: false,
          },
          max_context_length: 128_000,
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
          id: chatId,
          user_id: 1,
          title: "Preset routing test",
          folder: null,
          pinned: false,
          created_at: "2026-06-01T12:00:00Z",
          updated_at: "2026-06-01T12:00:00Z",
          settings: {},
          display_order: 0,
        }),
      });
    }
    if (method === "GET" && path === "/api/chats") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: chatId,
            user_id: 1,
            title: "Preset routing test",
            folder: null,
            pinned: false,
            created_at: "2026-06-01T12:00:00Z",
            updated_at: "2026-06-01T12:00:00Z",
            settings: {},
            display_order: 0,
            incognito: false,
            incognito_expires_at: null,
            model_id: "qwen3",
          },
        ]),
      });
    }
    if (method === "GET" && path === `/api/chats/${String(chatId)}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: chatId,
          user_id: 1,
          title: "Preset routing test",
          messages: [],
          has_more: false,
        }),
      });
    }
    return route.fallback();
  });
}

test.describe("Flow 61 — Preset model routing", () => {
  test("Settings → Preset models: select assigns model → PUT fires with correct mapping", async ({
    page,
  }) => {
    const chatId = 601;
    // Start with no preset-models configured.
    await stubCommon(page, chatId, {});

    let capturedPutBody: string | null = null;
    // Override the preset-models PUT to capture the body.
    await page.route("**/api/settings/preset-models", (route: Route) => {
      const method = route.request().method();
      if (method === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "{}",
        });
      }
      if (method === "PUT") {
        capturedPutBody = route.request().postData() ?? "";
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: capturedPutBody,
        });
      }
      return route.fallback();
    });

    // Auth is handled by stubs (probe returns admin); navigate directly.
    // Navigate to Settings → Preset models tab.
    await page.goto("/settings/preset-models");
    await expect(page.getByTestId("settings-preset-models-section")).toBeVisible(
      { timeout: 8_000 },
    );

    // The research preset row should have a model select.
    const researchRow = page.getByTestId("preset-models-row-research");
    await expect(researchRow).toBeVisible({ timeout: 6_000 });
    const sel = researchRow.getByTestId("preset-models-select-research");
    await expect(sel).toBeVisible({ timeout: 6_000 });

    // Select the OpenRouter model for research preset.
    await sel.selectOption(COMPOSITE);

    // The PUT should fire immediately (no save button — per-row auto-save).
    await expect
      .poll(() => capturedPutBody !== null, { timeout: 8_000 })
      .toBe(true);

    const body = JSON.parse(capturedPutBody as string) as Record<
      string,
      { provider: string; model_id: string }
    >;
    expect(body["research"]).toBeDefined();
    expect(body["research"].provider).toBe(PRESET_PROVIDER);
    expect(body["research"].model_id).toBe(PRESET_MODEL_ID);
  });

  test("/research sub-session stream carries assigned preset model+provider", async ({
    page,
  }) => {
    const chatId = 602;
    // Pre-configured: research preset already has the OR model assigned.
    await stubCommon(page, chatId, {
      research: { provider: PRESET_PROVIDER, model_id: PRESET_MODEL_ID },
    });

    let subBody: string | null = null;
    await page.route("**/api/chats/*/sub-session/stream", (route: Route) => {
      if (route.request().method() !== "POST") return route.fallback();
      subBody = route.request().postData() ?? "";
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSse("rid-602"),
      });
    });

    // Auth is handled by stubs (probe returns admin); navigate directly.
    await page.goto(`/chats/${String(chatId)}`);
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    // Launch the Research sub-agent via /research slash command.
    await composer.fill("/research");
    await composer.press("Control+Enter");

    // The sub-session panel should appear; no Composer badge (slash command
    // does not write active_preset under the new model).
    await expect(page.locator(".lmchat-subsession-label")).toBeVisible({ timeout: 6_000 });
    await expect(page.locator(".lmchat-subsession-label")).toContainText(/Research/i);
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

    // Send a follow-up message inside the sub-session — routes through the sub-session stream.
    await composer.fill("summarize recent AI papers");
    await composer.press("Control+Enter");

    await expect
      .poll(() => subBody !== null, { timeout: 10_000 })
      .toBe(true);

    const raw = subBody as string;
    // The sub-session form body must carry the assigned provider + model_id.
    expect(raw).toContain(PRESET_PROVIDER);
    expect(raw).toContain(PRESET_MODEL_ID);
    // The system_prompt for the research preset starts with "Research mode".
    expect(raw).toContain("Research mode");
  });
});
