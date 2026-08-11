/**
 * Flow — Projects v1 happy-path (route-stubbed).
 *
 * Phase-6 punch-list E. Exercises the frontend UX that ships in
 * Phase 6:
 *   1. Sidebar shows the Projects section.
 *   2. "+ New" button reveals the inline create form.
 *   3. Submitting creates a project + navigates to /project/:id.
 *   4. The project page shows three tabs.
 *   5. Switching tabs updates the URL hash and reveals the right panel.
 *   6. Settings tab renders the presets dropdown; selecting a preset
 *      copies its content into the instructions textarea.
 *   7. Delete-project shows a confirm step before firing.
 *
 * Backend round-trips are stubbed; the test focuses on the frontend
 * surface. The backend invariant "delete project → chats survive
 * un-projected" is covered by ``tests/db/test_migration_0021.py``
 * (SET NULL gate) and ``tests/routes/test_projects.py`` (DELETE route).
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

interface ProjectFixture {
  id: number;
  user_id: number;
  name: string;
  description: string;
  system_prompt: string;
  folders: string[];
  created_at: number;
  updated_at: number;
}

test.describe("Projects v1 — happy path", () => {
  test.beforeEach(async ({ page }) => {
    // In-memory state so the stubs respond consistently to mutations.
    const state = {
      projects: [] as ProjectFixture[],
      nextId: 100,
    };

    // Correctly-typed defaults for the post-login chat-page cold load.
    // Probe is overridden to a null user below: this spec drives the real
    // login form from a cold, unauthenticated boot, and every test here
    // does exactly one hard navigation (inside signIn()) — all further
    // navigation is client-side, so a static null shape is safe throughout.
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
    await page.route("**/api/auth/login", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: 1,
          expires_at: "2026-12-01T00:00:00Z",
          username: "alice",
          is_admin: false,
          totp_enabled: false,
        }),
      })
    );

    await page.route("**/api/prompts", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 7,
            user_id: 1,
            name: "TechWriter",
            content: "Be precise and brief.",
            created_at: "2026-01-01",
            updated_at: "2026-01-01",
          },
        ]),
      }),
    );

    // Projects CRUD.
    await page.route(/\/api\/projects$/, async (route) => {
      const req = route.request();
      if (req.method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(state.projects),
        });
      }
      if (req.method() === "POST") {
        const form = new URLSearchParams(req.postData() ?? "");
        const project: ProjectFixture = {
          id: state.nextId++,
          user_id: 1,
          name: form.get("name") ?? "",
          description: form.get("description") ?? "",
          system_prompt: form.get("system_prompt") ?? "",
          folders: [],
          created_at: 0,
          updated_at: 0,
        };
        state.projects.push(project);
        return route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify(project),
        });
      }
      return route.continue();
    });

    await page.route(/\/api\/projects\/(\d+)$/, async (route) => {
      const req = route.request();
      const match = req.url().match(/\/api\/projects\/(\d+)/);
      const projectId = match !== null ? Number(match[1]) : -1;
      const project = state.projects.find((p) => p.id === projectId);
      if (req.method() === "GET") {
        if (project === undefined) {
          return route.fulfill({ status: 404, body: "" });
        }
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(project),
        });
      }
      if (req.method() === "DELETE") {
        state.projects = state.projects.filter((p) => p.id !== projectId);
        return route.fulfill({ status: 204, body: "" });
      }
      return route.continue();
    });

    // Bypass first-run onboarding redirect.
    await page.route("**/api/onboarding/state", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          state: "complete",
          missing: [],
        }),
      }),
    );
  });

  /**
   * Log in via the real form — mirrors the pattern used by other
   * stubbed flows in this directory. The /api/auth/login + /api/auth/me
   * stubs above respond as "alice" and the SPA navigates to "/".
   *
   * The sidebar's Projects UI is a separate viewMode ("chats" | "projects"
   * — Sidebar.tsx), not inline content — <ProjectsSection> only mounts
   * once the "Open Projects" toggle (data-testid="sidebar-projects-link")
   * has been clicked. Every test in this file needs the Projects view, so
   * the toggle click lives here rather than being repeated per test.
   */
  async function signIn(page: import("@playwright/test").Page): Promise<void> {
    await page.goto("/login");
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("correct");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL("**/");

    await page.getByTestId("sidebar-projects-link").click();
  }

  test("sidebar shows projects section and create form", async ({ page }) => {
    await signIn(page);
    await expect(
      page.getByTestId("sidebar-projects-section"),
    ).toBeVisible();
    await expect(
      page.getByTestId("sidebar-projects-new-btn"),
    ).toBeVisible();

    await page.getByTestId("sidebar-projects-new-btn").click();
    await expect(
      page.getByTestId("sidebar-projects-create-input"),
    ).toBeVisible();
  });

  test("creating a project navigates to /project/:id", async ({ page }) => {
    await signIn(page);
    await page.getByTestId("sidebar-projects-new-btn").click();
    await page
      .getByTestId("sidebar-projects-create-input")
      .fill("My E2E Project");
    await page.getByTestId("sidebar-projects-create-submit").click();

    await page.waitForURL(/\/project\/\d+/);
    await expect(page.getByTestId("project-name")).toBeVisible();
    // Project name appears twice (h1 container + inner rename-button label);
    // ``.first()`` makes the locator deterministic under strict mode.
    await expect(page.getByText("My E2E Project").first()).toBeVisible();
  });

  test("project page hosts three tabs and switches between them", async ({
    page,
  }) => {
    await signIn(page);
    await page.getByTestId("sidebar-projects-new-btn").click();
    await page
      .getByTestId("sidebar-projects-create-input")
      .fill("TabsProject");
    await page.getByTestId("sidebar-projects-create-submit").click();
    await page.waitForURL(/\/project\/\d+/);

    await expect(page.getByTestId("project-tab-chats")).toBeVisible();
    await expect(page.getByTestId("project-tab-documents")).toBeVisible();
    await expect(page.getByTestId("project-tab-settings")).toBeVisible();

    // Default is chats.
    await expect(page.getByTestId("project-chats-list")).toBeVisible();

    await page.getByTestId("project-tab-documents").click();
    await expect(page).toHaveURL(/#documents$/);
    await expect(page.getByTestId("project-docs-list")).toBeVisible();

    await page.getByTestId("project-tab-settings").click();
    await expect(page).toHaveURL(/#settings$/);
    await expect(page.getByTestId("project-settings-preset")).toBeVisible();
  });

  test("selecting a preset seeds the instructions textarea", async ({
    page,
  }) => {
    await signIn(page);
    await page.getByTestId("sidebar-projects-new-btn").click();
    await page
      .getByTestId("sidebar-projects-create-input")
      .fill("PresetProject");
    await page.getByTestId("sidebar-projects-create-submit").click();
    await page.waitForURL(/\/project\/\d+/);

    await page.getByTestId("project-tab-settings").click();

    const prompt = page.getByTestId("project-settings-prompt");
    await expect(prompt).toBeVisible();
    await expect(prompt).toHaveValue("");

    await page
      .getByTestId("project-settings-preset")
      .selectOption({ label: "TechWriter" });

    await expect(prompt).toHaveValue("Be precise and brief.");
  });

  test("delete-project shows confirm step before firing", async ({ page }) => {
    await signIn(page);
    await page.getByTestId("sidebar-projects-new-btn").click();
    await page
      .getByTestId("sidebar-projects-create-input")
      .fill("DeleteMeProject");
    await page.getByTestId("sidebar-projects-create-submit").click();
    await page.waitForURL(/\/project\/\d+/);

    await page.getByTestId("project-tab-settings").click();
    await expect(page.getByTestId("project-delete-trigger")).toBeVisible();
    await page.getByTestId("project-delete-trigger").click();
    await expect(page.getByTestId("project-delete-confirm")).toBeVisible();
  });
});
