/**
 * Flow 35 — LM Studio config UI (P13g / ADR-023).
 *
 * What it proves (route-stubbed):
 *
 *  1. An admin lands on /settings/lm-studio, sees the resolved env-tier
 *     view (default localhost LM Studio).
 *  2. The admin edits the base URL field and clicks "Test connection".
 *     The probe call is captured and asserted to carry the new base
 *     URL; the stubbed probe returns ``ok: true`` with model_count=4.
 *     The UI shows "OK — 4 models reachable".
 *  3. The admin clicks "Save".  Connection fields (base_url/api_key) route
 *     through PATCH /api/admin/lmstudio/default for an admin (a probe-gated
 *     singleton-rewire endpoint distinct from the user-tier PUT — see
 *     LmStudioSection.handleSave); that body is captured and asserted to
 *     carry ONLY the changed base_url field (patch semantics: api_key
 *     omitted because the input is empty). default_model is ALWAYS written
 *     through the separate user-tier PUT /api/settings/lmstudio regardless
 *     of admin status (routing it through the admin PATCH lets a stale
 *     per-user override shadow it — see the handler's own comment), so
 *     that PUT fires too, carrying the unchanged default_model.
 *
 * The flow is route-stubbed (no real backend) so it exercises the
 * wire contract documented in ADR-023 §Routes.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 35 — LM Studio config UI (P13g)", () => {
  test("admin edits base URL → Test connection probes → save persists", async ({
    page,
  }) => {
    // Authed as an admin — bootstrap covers array/object defaults; this
    // spec is already signed in on cold load.
    await bootstrapAuthedApp(page, { isAdmin: true, username: "admin" });

    let lastProbeBody: Record<string, unknown> | null = null;
    let lastModelPutBody: Record<string, unknown> | null = null;
    let lastAdminPatchBody: Record<string, unknown> | null = null;
    let resolved = {
      base_url: "http://localhost:1234",
      default_model: "qwen3-8b",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
    };

    // LM Studio config GET / PUT / test — overrides bootstrap's default so
    // the test controls the exact resolved shape (source_base_url="env",
    // not "unset" — this spec doesn't exercise the env_suggestion pre-fill
    // path at all). PUT here only ever carries default_model (see above).
    await page.route("**/api/settings/lmstudio", (route) => {
      const method = route.request().method();
      if (method === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(resolved),
        });
      }
      if (method === "PUT") {
        lastModelPutBody = JSON.parse(
          route.request().postData() ?? "{}",
        ) as Record<string, unknown>;
        if (typeof lastModelPutBody.default_model === "string") {
          resolved = { ...resolved, default_model: lastModelPutBody.default_model };
        }
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(resolved),
        });
      }
      return route.continue();
    });

    // Admin connection-fields endpoint (base_url/api_key) — probe-gated
    // singleton rewire, PATCH-only.
    await page.route("**/api/admin/lmstudio/default", (route) => {
      if (route.request().method() !== "PATCH") return route.fallback();
      lastAdminPatchBody = JSON.parse(
        route.request().postData() ?? "{}",
      ) as Record<string, unknown>;
      if (typeof lastAdminPatchBody.base_url === "string") {
        resolved = {
          ...resolved,
          base_url: lastAdminPatchBody.base_url,
          source_base_url: "user",
        };
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(resolved),
      });
    });

    await page.route("**/api/settings/lmstudio/test", (route) => {
      lastProbeBody = JSON.parse(
        route.request().postData() ?? "{}",
      ) as Record<string, unknown>;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, model_count: 4, error: null }),
      });
    });

    // Navigate directly to the tab.
    await page.goto("/settings/lm-studio");
    await expect(
      page.getByTestId("settings-lmstudio-section"),
    ).toBeVisible();

    // Pre-populated from env-tier resolved view.
    await expect(page.getByTestId("lmstudio-base-url")).toHaveValue(
      "http://localhost:1234",
    );

    // Edit the base URL.
    await page
      .getByTestId("lmstudio-base-url")
      .fill("http://probe-target.example:1234");

    // Click Test connection.
    await page.getByTestId("lmstudio-test-connection").click();

    // Test result appears with model count.
    await expect(page.getByTestId("lmstudio-test-result")).toContainText(
      "Connected — 4 models reachable",
    );
    expect(lastProbeBody).not.toBeNull();
    expect(lastProbeBody?.base_url).toBe("http://probe-target.example:1234");

    // Save.
    await page.getByTestId("lmstudio-save").click();

    // Wait for the save to land + the toast / state to update.
    await expect.poll(() => lastAdminPatchBody).not.toBeNull();
    expect(lastAdminPatchBody).toEqual({
      base_url: "http://probe-target.example:1234",
    });
    // api_key NOT in the body (user did not type one).
    expect("api_key" in (lastAdminPatchBody ?? {})).toBe(false);

    // default_model is always written through the separate user-tier PUT,
    // unchanged in this scenario (the admin never edited the model field).
    await expect.poll(() => lastModelPutBody).not.toBeNull();
    expect(lastModelPutBody).toEqual({ default_model: "qwen3-8b" });
  });
});
