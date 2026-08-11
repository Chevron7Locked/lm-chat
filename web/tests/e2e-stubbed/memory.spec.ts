/**
 * E2E: memory panel (Playwright, route-stubbed).
 *
 * Flow: login → open memory panel → pin insight → see in list → unpin → empty.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Memory panel", () => {
  // Track pinned items locally to simulate server state.
  let pins: Array<{ id: number; text: string; created_at: string }> = [];
  let nextId = 1;

  test.beforeEach(async ({ page }) => {
    pins = [];
    nextId = 1;

    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load — including across page.reload() (the probe's default
    // authed shape is static, so reload never bounces to /login here).
    await bootstrapAuthedApp(page);

    // Memory pins — dynamic stub that responds to the current pins array.
    // GET /api/memory/pins → plain MemoryInsight[] (backend wire shape).
    await page.route("**/api/memory/pins", async (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(pins),
        });
      }
      return route.fallback();
    });

    // POST /api/memory/pin — add a pin.
    // Hook sends application/x-www-form-urlencoded (backend uses Form(...)).
    await page.route("**/api/memory/pin", async (route) => {
      if (route.request().method() === "POST") {
        const params = new URLSearchParams(route.request().postData() ?? "");
        const pin = { id: nextId++, text: params.get("text") ?? "", created_at: new Date().toISOString() };
        pins.push(pin);
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(pin),
        });
      }
      return route.fallback();
    });

    // DELETE /api/memory/pin/{id} — remove a pin.
    await page.route("**/api/memory/pin/**", async (route) => {
      if (route.request().method() === "DELETE") {
        const urlPath = new URL(route.request().url()).pathname;
        const idStr = urlPath.split("/").at(-1);
        const id = idStr !== undefined ? Number(idStr) : NaN;
        pins = pins.filter((p) => p.id !== id);
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "ok" }),
        });
      }
      return route.fallback();
    });

    // Open memory panel (standalone route for cleanliness in E2E).
    await page.goto("/memory");
  });

  test("renders empty state initially", async ({ page }) => {
    await expect(page.getByText("Nothing remembered yet.")).toBeVisible();
  });

  test("pins an insight and shows it in the list", async ({ page }) => {
    await page.getByRole("textbox", { name: "New insight" }).fill("Remember to use OKLCH");
    await page.getByRole("button", { name: "Pin" }).click();

    // Success toast should appear (role="status" — a polite announcement,
    // not role="alert"). dnd-kit's own empty live-region also carries
    // role="status" — the toast is the one with actual text content.
    await expect(page.getByRole("status").last()).toBeVisible({ timeout: 3_000 });

    // Reload the page to re-fetch pins (stub returns updated list).
    await page.reload();
    await expect(page.getByText("Remember to use OKLCH")).toBeVisible();
  });

  test("unpins an insight and sees empty state", async ({ page }) => {
    // Seed the pin directly in our local array (simulates a pre-existing pin).
    pins.push({ id: 99, text: "Pre-existing insight", created_at: new Date().toISOString() });

    // Reload to see the seeded pin.
    await page.reload();
    await expect(page.getByText("Pre-existing insight")).toBeVisible();

    // Click unpin.
    await page.getByRole("button", { name: /Unpin: Pre-existing insight/ }).click();

    // After unpin query invalidation, the list should be empty (re-fetched).
    await page.reload();
    await expect(page.getByText("Nothing remembered yet.")).toBeVisible();
  });

  test("shows heading Memory", async ({ page }) => {
    await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();
  });

  test("Pin button is disabled when input is empty", async ({ page }) => {
    await expect(page.getByRole("button", { name: "Pin" })).toBeDisabled();
  });

  test("Pin button is enabled when input has text", async ({ page }) => {
    await page.getByRole("textbox", { name: "New insight" }).fill("some text");
    await expect(page.getByRole("button", { name: "Pin" })).toBeEnabled();
  });
});
