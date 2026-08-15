/**
 * Flow 60 — Allowlist filters the chat model dropdown.
 *
 * What it proves (route-stubbed):
 *   When a provider config carries a non-empty allowed_models list, the
 *   /api/models endpoint returns only those models from that provider.
 *   The chat-page model dropdown (chat-header-model-select) therefore shows
 *   only the allowed subset, not all provider models.
 *
 * The backend enforces the allowlist by filtering its model catalog before
 *   responding to GET /api/models. The FE simply renders whatever the
 *   endpoint returns. This spec stubs /api/models to return the restricted
 *   set and asserts the dropdown option values match.
 *
 * Source:
 *   web/src/components/ProvidersSection.tsx — allowlist-picker, allowlist-filter
 *   web/src/hooks/useChatModelOptions.ts — groups the dropdown options
 *   web/src/pages/Chat.tsx:2356/2458 — testId="chat-header-model-select"
 */
import { test, expect, type Page, type Route } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

// The full provider catalog has many models; the allowlist restricts to these two.
const ALLOWED_OR_MODELS = [
  "openai/gpt-4o-mini",
  "openai/gpt-4o-mini-2024-07-18",
];

const ALL_OR_MODELS = [
  ...ALLOWED_OR_MODELS,
  "meta-llama/llama-3.3-70b-instruct",
  "anthropic/claude-3.5-sonnet",
  "google/gemini-2.0-flash-001",
];

async function stubCommon(page: Page) {
  // Authed as an admin — bootstrap covers array/object defaults; this
  // spec is already signed in on cold load.
  await bootstrapAuthedApp(page, { isAdmin: true, username: "admin" });

  await page.route("**/api/providers/status", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
}

/** Build a model entry for the /api/models response. */
function makeModel(id: string, provider: string, displayName: string) {
  return {
    key: id,
    display_name: displayName,
    provider,
    loaded_instances: 0,
    loaded_instance_ids: [],
    capabilities: {
      vision: false,
      trained_for_tool_use: true,
      reasoning: null,
      embedding: false,
    },
    max_context_length: 128_000,
    loaded_context_length: 0,
    size_bytes: 0,
    params_string: "",
    quantization: null,
  };
}

/** Stub a minimal chat so the chat-detail page renders with the top bar. */
async function stubChat(
  page: Page,
  chatId: number,
) {
  await page.route("**/api/chats**", async (route: Route) => {
    const method = route.request().method();
    const path = new URL(route.request().url()).pathname;
    if (method === "GET" && path === "/api/chats") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: chatId,
            user_id: 1,
            title: "Allowlist test",
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
          title: "Allowlist test",
          messages: [],
          has_more: false,
        }),
      });
    }
    return route.fallback();
  });
}

test.describe("Flow 60 — Allowlist filters chat model dropdown", () => {
  test("allowlist active → dropdown shows only allowed OpenRouter models", async ({
    page,
  }) => {
    const chatId = 591;
    await stubCommon(page);
    await stubChat(page, chatId);

    // Provider config has an allowlist — the BE returns only allowed models.
    // Stub /api/models to simulate the filtered response from the backend.
    await page.route("**/api/models", (route: Route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const allowedModels = ALLOWED_OR_MODELS.map((id) =>
        makeModel(id, "openrouter", id.split("/").pop() ?? id),
      );
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          makeModel("qwen3", "lmstudio", "Qwen 3"),
          ...allowedModels,
        ]),
      });
    });

    // Navigate to a chat page so the top-bar model selector renders.
    await page.goto(`/chats/${String(chatId)}`);
    const modelSelect = page.getByTestId("chat-header-model-select").first();
    await expect(modelSelect).toBeVisible({ timeout: 10_000 });

    // ModelSelectControl renders a single disabled "Loading models…"
    // placeholder option while /api/models is in flight — reading the
    // option list right after toBeVisible() (which only asserts the <select>
    // itself exists) can race that fetch and snapshot zero openrouter
    // options (observed on firefox). toHaveCount is a web-first assertion
    // that auto-retries until the real options land, unlike a one-shot
    // evaluateAll + .length read.
    const orOptionLocator = modelSelect.locator('option[value^="openrouter::"]');
    await expect(orOptionLocator).toHaveCount(ALLOWED_OR_MODELS.length, {
      timeout: 10_000,
    });
    const orOptions = await orOptionLocator.evaluateAll((os) =>
      os.map((o) => (o as HTMLOptionElement).value),
    );

    for (const allowed of ALLOWED_OR_MODELS) {
      expect(orOptions).toContain(`openrouter::${allowed}`);
    }

    // Non-allowed models must NOT appear.
    const nonAllowed = ALL_OR_MODELS.filter(
      (id) => !ALLOWED_OR_MODELS.includes(id),
    );
    for (const id of nonAllowed) {
      expect(orOptions).not.toContain(`openrouter::${id}`);
    }
  });

  test("no allowlist → all provider models appear in dropdown", async ({
    page,
  }) => {
    const chatId = 592;
    await stubCommon(page);
    await stubChat(page, chatId);

    // No allowlist — /api/models returns the full catalog.
    await page.route("**/api/models", (route: Route) => {
      if (route.request().method() !== "GET") return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          makeModel("qwen3", "lmstudio", "Qwen 3"),
          ...ALL_OR_MODELS.map((id) =>
            makeModel(id, "openrouter", id.split("/").pop() ?? id),
          ),
        ]),
      });
    });

    // Navigate to a chat page so the top-bar model selector renders.
    await page.goto(`/chats/${String(chatId)}`);
    const modelSelect = page.getByTestId("chat-header-model-select").first();
    await expect(modelSelect).toBeVisible({ timeout: 10_000 });

    // Same load race as the allowlisted-subset test above (see its comment)
    // — wait for the real option count before reading values.
    const orOptionLocator = modelSelect.locator('option[value^="openrouter::"]');
    await expect(orOptionLocator).toHaveCount(ALL_OR_MODELS.length, {
      timeout: 10_000,
    });
    const orOptions = await orOptionLocator.evaluateAll((os) =>
      os.map((o) => (o as HTMLOptionElement).value),
    );
    for (const id of ALL_OR_MODELS) {
      expect(orOptions).toContain(`openrouter::${id}`);
    }
  });

  test("Settings → Providers: allowlist picker filter input narrows visible models", async ({
    page,
  }) => {
    await stubCommon(page);

    await page.route("**/api/admin/providers", (route: Route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "[]",
        });
      }
      return route.fallback();
    });
    await page.route("**/api/models", (route: Route) => {
      if (route.request().method() !== "GET") return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "[]",
      });
    });

    // Test probe returns all OR models so the allowlist picker is populated.
    await page.route("**/api/admin/providers/openrouter/test", (route: Route) => {
      if (route.request().method() !== "POST") return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          model_count: ALL_OR_MODELS.length,
          model_ids: ALL_OR_MODELS,
        }),
      });
    });

    // Auth is handled by stubs (probe returns admin); navigate directly.
    await page.goto("/settings/providers");
    await expect(page.getByTestId("settings-providers-section")).toBeVisible({
      timeout: 8_000,
    });

    await page.getByTestId("providers-add-btn").click();
    await expect(page.getByTestId("providers-form")).toBeVisible({ timeout: 4_000 });

    // Trigger probe to populate the allowlist picker.
    await page.getByTestId("providers-test").click();
    await expect(page.getByTestId("allowlist-picker")).toBeVisible({
      timeout: 8_000,
    });

    // Filter to "gpt-4o-mini" — only those two models should remain visible.
    await page.getByTestId("allowlist-filter").fill("gpt-4o-mini");
    // The checkboxes for non-matching models should no longer be present.
    await expect(
      page.getByTestId("allowlist-checkbox-openai/gpt-4o-mini"),
    ).toBeVisible({ timeout: 4_000 });
    await expect(
      page.getByTestId(
        "allowlist-checkbox-meta-llama/llama-3.3-70b-instruct",
      ),
    ).not.toBeVisible();

    // Select filtered → the two gpt-4o-mini entries are checked.
    await page.getByTestId("allowlist-select-all").click();
    for (const id of ALLOWED_OR_MODELS) {
      await expect(
        page.getByTestId(`allowlist-checkbox-${id}`),
      ).toBeChecked({ timeout: 4_000 });
    }

    // Clear the filter — the other models reappear but remain unchecked.
    await page.getByTestId("allowlist-filter").fill("");
    await expect(
      page.getByTestId(
        "allowlist-checkbox-meta-llama/llama-3.3-70b-instruct",
      ),
    ).toBeVisible({ timeout: 4_000 });
    await expect(
      page.getByTestId(
        "allowlist-checkbox-meta-llama/llama-3.3-70b-instruct",
      ),
    ).not.toBeChecked();
  });
});
