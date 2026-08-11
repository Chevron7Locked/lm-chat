/**
 * Flow 64 — Embedding mismatch warning on the Project Documents tab.
 *
 * What it proves (route-stubbed):
 *   When a project's `embedding_model_id` (the model its chunks were encoded
 *   under) differs from the `active_model_id` returned by
 *   `GET /api/memory/embedding/status`, the Documents tab renders the
 *   `data-testid="embedding-mismatch-warning"` element (Project.tsx ~line 598).
 *
 * The warning lives in `DocumentsTab` and renders when:
 *   - project.embedding_model_id is non-null and non-empty
 *   - embeddingStatus.active_model_id is non-null
 *   - the two values differ
 */
import { test, expect } from "@playwright/test";

const AUTH_ME = {
  user_id: 1,
  username: "alice",
  is_admin: false,
  expires_at: "2026-12-01T00:00:00Z",
};

const PROJECT = {
  id: 1,
  user_id: 1,
  name: "Test Project",
  description: "",
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  chat_count: 0,
  // Pinned under old-model — differs from the active model below.
  embedding_model_id: "text-embedding-old-1",
  system_prompt: "",
  chat_model_id: null,
};

const EMBEDDING_STATUS_MISMATCH = {
  // Active model differs from the project's pinned model.
  active_model_id: "text-embedding-new-2",
  embedding_status: "ok",
  model_loaded: true,
  model_name: "text-embedding-new-2",
  indexing_running: false,
  pending_chunks: 0,
  total_chunks: 5,
};

/** Register the auth + shell stubs common to both tests. */
async function registerBaseRoutes(page: import("@playwright/test").Page) {
  await page.route("**/api/auth/login", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(AUTH_ME) })
  );
  await page.route("**/api/auth/me/probe", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...AUTH_ME, needs_setup: false, totp_enabled: false, email: null, display_name: null, avatar_url: null }),
    })
  );
  await page.route("**/api/auth/setup_status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ needs_setup: false }) })
  );
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(AUTH_ME) })
  );
  await page.route("**/api/models", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/settings/lmstudio", (route) =>
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
  );
  await page.route("**/api/folders**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/chats**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/prompts**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/memory/pins", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/integrations/available", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/settings/preset-models", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/quotas/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ tokens_per_day: 50000, requests_per_day: 500, tokens_consumed_today: 0, requests_consumed_today: 0, resets_at: "2026-12-02T00:00:00Z" }),
    })
  );
  await page.route("**/api/documents**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );

  // Projects — catch-all first, then specific overrides.
  await page.route("**/api/projects**", (route) => {
    if (route.request().method() !== "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([PROJECT]),
    });
  });
  // Specific project detail — registered AFTER the catch-all so it wins.
  await page.route("**/api/projects/1", (route) => {
    if (route.request().method() !== "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PROJECT),
    });
  });
}

test.describe("Flow 64 — Embedding mismatch warning", () => {
  test("embedding-mismatch-warning renders when active model differs from project pin", async ({ page }) => {
    await registerBaseRoutes(page);

    // Embedding status — active model differs from the project's pinned one.
    await page.route("**/api/memory/embedding/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(EMBEDDING_STATUS_MISMATCH),
      })
    );

    await page.goto("/project/1#documents");

    // The Documents tab must be present and active.
    const documentsTab = page.getByTestId("project-tab-documents");
    await expect(documentsTab).toBeVisible({ timeout: 10_000 });
    await documentsTab.click();

    // The mismatch warning must be visible:
    // embedding_model_id ("text-embedding-old-1") != active_model_id ("text-embedding-new-2").
    await expect(
      page.getByTestId("embedding-mismatch-warning")
    ).toBeVisible({ timeout: 10_000 });

    // The warning must name both models.
    await expect(
      page.getByTestId("embedding-mismatch-warning")
    ).toContainText("text-embedding-old-1");
    await expect(
      page.getByTestId("embedding-mismatch-warning")
    ).toContainText("text-embedding-new-2");
  });

  test("no mismatch warning when active model matches project pin", async ({ page }) => {
    await registerBaseRoutes(page);

    // Override embedding status so both models match the project pin.
    await page.route("**/api/memory/embedding/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...EMBEDDING_STATUS_MISMATCH,
          active_model_id: PROJECT.embedding_model_id,
        }),
      })
    );

    await page.goto("/project/1#documents");

    const documentsTab = page.getByTestId("project-tab-documents");
    await expect(documentsTab).toBeVisible({ timeout: 10_000 });
    await documentsTab.click();

    // No mismatch — warning must not be present.
    await expect(
      page.getByTestId("embedding-mismatch-warning")
    ).not.toBeVisible({ timeout: 5_000 });
  });
});
