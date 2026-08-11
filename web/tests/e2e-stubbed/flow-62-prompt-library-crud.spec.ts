/**
 * Flow 62 — Prompt Library CRUD.
 *
 * What it proves (route-stubbed):
 *   1. GET /api/prompts lists saved prompts.
 *   2. Submitting the create form fires POST /api/prompts; the new prompt
 *      appears in the list.
 *   3. Clicking Edit and saving fires PATCH /api/prompts/{id}; the updated
 *      name appears in the list.
 *   4. Clicking Delete fires DELETE /api/prompts/{id}; the row is removed.
 *
 * /prompts is not behind RequireAuth so no login step is needed.  We follow
 * the minimal route pattern from flow-44.
 */
import { test, expect } from "@playwright/test";

const AUTH_ME = {
  user_id: 1,
  username: "alice",
  is_admin: false,
  expires_at: "2026-12-01T00:00:00Z",
};

const BASE_PROMPT = {
  id: 1,
  user_id: 1,
  name: "summarize-code",
  content: "Summarize the following code in plain English.",
  created_at: 1717200000,
};

function registerBaseRoutes(page: import("@playwright/test").Page) {
  return Promise.all([
    page.route("**/api/auth/login", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(AUTH_ME) })
    ),
    page.route("**/api/auth/me/probe", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...AUTH_ME, needs_setup: false, totp_enabled: false, email: null, display_name: null, avatar_url: null }),
      })
    ),
    page.route("**/api/auth/setup_status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ needs_setup: false }) })
    ),
    page.route("**/api/auth/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(AUTH_ME) })
    ),
    page.route("**/api/models", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    ),
    page.route("**/api/settings/lmstudio", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          base_url: "http://localhost:1234",
          default_model: "",
          api_key_set: false,
          source_base_url: "unset",
          source_api_key: "unset",
          source_default_model: "unset",
          key_pruned: false,
          auth_failed: false,
        }),
      })
    ),
    page.route("**/api/folders", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    ),
    page.route("**/api/projects", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    ),
    page.route("**/api/quotas/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ tokens_per_day: 50000, requests_per_day: 500, tokens_consumed_today: 0, requests_consumed_today: 0, resets_at: "2026-12-02T00:00:00Z" }),
      })
    ),
    page.route("**/api/memory/pins", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    ),
    page.route("**/api/integrations/available", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    ),
    page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      if (method === "GET" && url.pathname === "/api/chats") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
      }
      return route.continue();
    }),
  ]);
}

test.describe("Flow 62 — Prompt Library CRUD", () => {
  test("create prompt — POST fires and item appears in list", async ({ page }) => {
    let prompts = [{ ...BASE_PROMPT }];

    await registerBaseRoutes(page);

    await page.route("**/api/prompts", async (route) => {
      const method = route.request().method();
      if (method === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(prompts),
        });
      }
      if (method === "POST") {
        const raw = route.request().postData() ?? "";
        const params = new URLSearchParams(raw);
        const created = {
          id: 2,
          user_id: 1,
          name: params.get("name") ?? "",
          content: params.get("content") ?? "",
          created_at: 1717200001,
        };
        prompts = [...prompts, created];
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(created),
        });
      }
      return route.continue();
    });

    await page.goto("/prompts");
    // Baseline: existing prompt is listed.
    await expect(page.getByText("summarize-code")).toBeVisible({ timeout: 10_000 });

    // Fill the create form.
    await page.getByLabel("Prompt name").fill("my-new-prompt");
    await page.getByLabel("Prompt content").fill("Tell me a short story.");
    await page.getByRole("button", { name: "Create prompt" }).click();

    // New prompt must appear in the list.
    await expect(page.getByText("my-new-prompt")).toBeVisible({ timeout: 10_000 });
  });

  test("edit prompt — PATCH fires and updated name appears", async ({ page }) => {
    let promptName = "summarize-code";

    await registerBaseRoutes(page);

    await page.route("**/api/prompts", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{ ...BASE_PROMPT, name: promptName }]),
      });
    });

    await page.route(`**/api/prompts/${BASE_PROMPT.id.toString()}`, async (route) => {
      const method = route.request().method();
      if (method === "PATCH") {
        const raw = route.request().postData() ?? "";
        const params = new URLSearchParams(raw);
        promptName = params.get("name") ?? promptName;
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ...BASE_PROMPT, name: promptName }),
        });
      }
      return route.continue();
    });

    await page.goto("/prompts");
    await expect(page.getByText("summarize-code")).toBeVisible({ timeout: 10_000 });

    // Open edit form.
    await page.getByRole("button", { name: "Edit" }).click();

    // The edit form uses aria-label="Edit prompt name".
    const nameInput = page.getByLabel("Edit prompt name");
    await expect(nameInput).toBeVisible({ timeout: 5_000 });
    await nameInput.fill("summarize-code-v2");

    await page.getByRole("button", { name: "Save" }).click();

    // Updated name must appear.
    await expect(page.getByText("summarize-code-v2")).toBeVisible({ timeout: 10_000 });
  });

  test("delete prompt — DELETE fires and row disappears", async ({ page }) => {
    let prompts = [{ ...BASE_PROMPT }];

    await registerBaseRoutes(page);

    await page.route("**/api/prompts", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(prompts),
      });
    });

    await page.route(`**/api/prompts/${BASE_PROMPT.id.toString()}`, async (route) => {
      const method = route.request().method();
      if (method === "DELETE") {
        prompts = [];
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "deleted" }),
        });
      }
      return route.continue();
    });

    await page.goto("/prompts");
    await expect(page.getByText("summarize-code")).toBeVisible({ timeout: 10_000 });

    await page.getByRole("button", { name: "Delete" }).click();

    // Row must be gone; empty state should appear.
    await expect(page.getByText("summarize-code")).not.toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("The recipes are empty.")).toBeVisible();
  });
});
