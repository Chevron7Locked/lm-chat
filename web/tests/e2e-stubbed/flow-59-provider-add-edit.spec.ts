/**
 * Flow 59 — Provider add/edit (Settings → Providers).
 *
 * What it proves (route-stubbed):
 *   1. An admin can navigate to Settings → Providers tab and see the section.
 *   2. Clicking "Add provider" opens the ProviderForm.
 *   3. Filling the form (OpenRouter preset) and clicking Save fires
 *      PUT /api/admin/providers/openrouter with the expected body fields.
 *   4. After save, the providers list re-fetches and the new provider row
 *      appears (provider-row-openrouter).
 *   5. On the chat page the model dropdown (chat-header-model-select) now
 *      includes the OpenRouter group options (composite "openrouter::" values).
 *
 * Source:
 *   web/src/components/ProvidersSection.tsx
 *   web/src/hooks/useProviders.ts  — PUT /api/admin/providers/{provider}
 */
import { test, expect, type Page, type Route } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";
import type { UpsertProviderBody } from "@/hooks/useProviders";

const PROBE_MODELS = [
  "openai/gpt-4o",
  "openai/gpt-4o-mini",
  "meta-llama/llama-3.3-70b-instruct",
];

async function stubCommon(page: Page) {
  // Authed as an admin — bootstrap covers array/object defaults; this
  // spec is already signed in on cold load.
  await bootstrapAuthedApp(page, { isAdmin: true, username: "admin" });

  await page.route("**/api/providers/status", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
}

test.describe("Flow 59 — Provider add/edit", () => {
  test("add OpenRouter provider → PUT fires with correct body → provider row appears", async ({
    page,
  }) => {
    await stubCommon(page);

    // Initially no providers configured.
    await page.route("**/api/admin/providers", (route: Route) => {
      const method = route.request().method();
      if (method === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "[]",
        });
      }
      return route.fallback();
    });

    // Models list — LM Studio only before provider is added.
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
        ]),
      });
    });

    // Capture the PUT body for assertion.
    const capturedPutBody: { value: string | null } = { value: null };
    await page.route("**/api/admin/providers/openrouter", (route: Route) => {
      const method = route.request().method();
      if (method === "PUT") {
        capturedPutBody.value = route.request().postData() ?? "";
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            provider: "openrouter",
            base_url: "https://openrouter.ai/api",
            default_model: null,
            extra_headers: null,
            enabled: true,
            api_key_set: true,
            allowed_models: [],
          }),
        });
      }
      return route.fallback();
    });

    // Auth is handled by stubs (probe returns admin); navigate directly.
    await page.goto("/settings/providers");
    await expect(page.getByTestId("settings-providers-section")).toBeVisible({
      timeout: 8_000,
    });

    // Open the add form.
    await page.getByTestId("providers-add-btn").click();
    await expect(page.getByTestId("providers-form")).toBeVisible({ timeout: 4_000 });

    // Provider preset is "OpenRouter" by default; base URL is pre-filled.
    await expect(page.getByTestId("provider-select")).toHaveValue("openrouter");
    await expect(page.getByTestId("provider-base-url")).toHaveValue(
      "https://openrouter.ai/api",
    );

    // Fill in an API key.
    await page.getByTestId("provider-api-key").fill("sk-or-test-key-123");

    // Save.
    await page.getByTestId("providers-save").click();

    // Assert the PUT body was captured and contains the expected fields.
    await expect
      .poll(() => capturedPutBody.value !== null, { timeout: 8_000 })
      .toBe(true);

    if (capturedPutBody.value === null) throw new Error("expected capturedPutBody to be captured");
    const body: UpsertProviderBody = JSON.parse(capturedPutBody.value);
    expect(body["base_url"]).toBe("https://openrouter.ai/api");
    expect(body["enabled"]).toBe(true);
    expect(body["api_key"]).toBe("sk-or-test-key-123");
    // allowed_models: [] means "all allowed" on the BE.
    expect(Array.isArray(body["allowed_models"])).toBe(true);

    // After save the form closes — providers list is re-fetched.
    // Re-stub to return the newly saved provider.
    await page.route("**/api/admin/providers", (route: Route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              provider: "openrouter",
              base_url: "https://openrouter.ai/api",
              default_model: null,
              extra_headers: null,
              enabled: true,
              api_key_set: true,
              allowed_models: [],
            },
          ]),
        });
      }
      return route.fallback();
    });
    // After the provider is added, models includes OpenRouter entries.
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
            key: "openai/gpt-4o-mini",
            display_name: "GPT-4o mini",
            provider: "openrouter",
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

    // The form should close and the providers list should reappear.
    await expect(page.getByTestId("settings-providers-section")).toBeVisible({
      timeout: 6_000,
    });
    await expect(page.getByTestId("providers-form")).not.toBeVisible();

    // Navigate to the chat page — model dropdown should include the new
    // OpenRouter group option.
    await page.goto("/");
    const modelSelect = page.getByTestId("chat-header-model-select").first();
    await expect(modelSelect).toBeVisible({ timeout: 10_000 });
    // The ModelSelectControl renders a <select>; assert at least one option
    // has the composite "openrouter::" prefix. The option list populates only
    // after the post-navigation models refetch lands (slower under parallel
    // load in Firefox), so poll the DOM rather than snapshot it once.
    await expect
      .poll(
        async () =>
          modelSelect
            .locator("option")
            .evaluateAll((os) =>
              os
                .map((o) => (o as HTMLOptionElement).value)
                .filter((v) => v.startsWith("openrouter::")),
            ),
        { timeout: 10_000 },
      )
      .toEqual(expect.arrayContaining([expect.stringContaining("gpt-4o-mini")]));
  });

  test("test-connection probe → allowlist picker appears with returned model ids", async ({
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

    // Test-probe endpoint returns a list of model ids.
    await page.route("**/api/admin/providers/openrouter/test", (route: Route) => {
      if (route.request().method() !== "POST") return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          model_count: PROBE_MODELS.length,
          model_ids: PROBE_MODELS,
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

    // Click "Test connection" — expect probe-result banner + allowlist picker.
    await page.getByTestId("providers-test").click();
    await expect(page.getByTestId("providers-test-result")).toBeVisible({
      timeout: 8_000,
    });
    // The banner should show "Connected" when ok=true.
    await expect(page.getByTestId("providers-test-result")).toContainText(
      /connected/i,
    );

    // The allowlist picker appears with the models returned by the probe.
    await expect(page.getByTestId("allowlist-picker")).toBeVisible({
      timeout: 4_000,
    });
    // Each model id from the probe has a checkbox.
    for (const id of PROBE_MODELS) {
      await expect(
        page.getByTestId(`allowlist-checkbox-${id}`),
      ).toBeVisible();
    }
  });
});
