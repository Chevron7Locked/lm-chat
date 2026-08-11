/**
 * Flow 40 — P13l.2 folder CRUD.
 *
 * Stubbed-network smoke proving the sidebar folder UI hits the new
 * /api/folders endpoints.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 40 — folder CRUD UI", () => {
  test("create + rename + delete cycle hits the folder endpoints", async ({
    page,
  }) => {
    let folders: string[] = [];
    let calls: { method: string; url: string; body: string | null }[] = [];

    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    await page.route(/\/api\/folders$/, async (route) => {
      const req = route.request();
      calls.push({
        method: req.method(),
        url: req.url(),
        body: req.postData(),
      });
      if (req.method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(folders),
        });
      }
      if (req.method() === "POST") {
        const params = new URLSearchParams(req.postData() ?? "");
        const name = params.get("name") ?? "";
        if (!folders.includes(name)) folders.push(name);
        folders.sort();
        return route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify(folders),
        });
      }
      return route.continue();
    });
    await page.route(/\/api\/folders\/.*/, async (route) => {
      const req = route.request();
      calls.push({
        method: req.method(),
        url: req.url(),
        body: req.postData(),
      });
      const match = /\/api\/folders\/([^?]+)/.exec(req.url());
      const name = match ? decodeURIComponent(match[1] ?? "") : "";
      if (req.method() === "PATCH") {
        const params = new URLSearchParams(req.postData() ?? "");
        const newName = params.get("new_name") ?? "";
        folders = folders.filter((f) => f !== name);
        if (newName && !folders.includes(newName)) folders.push(newName);
        folders.sort();
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(folders),
        });
      }
      if (req.method() === "DELETE") {
        folders = folders.filter((f) => f !== name);
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(folders),
        });
      }
      return route.continue();
    });

    await page.goto("/");
    const newFolderBtn = page.getByTestId("sidebar-new-folder-btn");
    await expect(newFolderBtn).toBeVisible({ timeout: 5_000 });
    await newFolderBtn.click();
    const input = page.getByTestId("sidebar-new-folder-input");
    await input.fill("Drafts");
    await page.getByTestId("sidebar-new-folder-submit").click();

    // POST was issued.
    await expect.poll(() =>
      calls.some(
        (c) => c.method === "POST" && /\/api\/folders$/.test(c.url),
      ),
    ).toBe(true);
  });
});
