/**
 * Flow 30 — MCP Store live API round-trip.
 *
 * What it proves:
 *   The MCP Store API endpoints work end-to-end against the live backend:
 *
 *   1. GET  /api/mcp-store/catalog  → returns curated catalog entries
 *      (requires admin auth; asserts known no-secrets entries are present).
 *   2. POST /api/mcp-store/servers  → installs a catalog server (filesystem
 *      and fetch have no required secrets so no external config needed).
 *   3. GET  /api/mcp-store/servers  → shows the installed server.
 *   4. DELETE /api/mcp-store/servers/:slug → removes it; subsequent GET
 *      returns an empty list (or list without the deleted entry).
 *
 * No real MCP process is spawned — installing a stdio server just persists
 * the DB row and registers the config in mcp_host._configs. The test never
 * calls a "connect" endpoint, so no npx process runs.
 *
 * Auth: all MCP Store endpoints require admin (require_admin dependency).
 * Tests use the adminUsername / adminPassword fixture.
 *
 * Catalog entries chosen for no-secrets install:
 *   - "filesystem" (no secrets, stdio, npx)
 *   - "fetch"      (no secrets, stdio, npx)
 *   - "sequential-thinking" (no secrets, stdio, npx)
 *   - "deepwiki"   (no secrets, stdio, npx)
 *   - "playwright" (no secrets, stdio, npx)
 *
 * We install "fetch" (shortest name, first in no-secrets subset).
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
} from "./_flow-helpers";

// ---------------------------------------------------------------------------
// Type shapes for MCP store API responses
// ---------------------------------------------------------------------------

interface CatalogEntry {
  id: string;
  name: string;
  description: string;
  transport: string;
  command?: string | null;
  args?: string[];
  url?: string;
  secrets: Array<{ key: string; label: string; required: boolean }>;
  source: string;
  trust: string;
}

interface McpServerResponse {
  id: number;
  slug: string;
  name: string;
  transport: string;
  command?: string | null;
  args?: string[];
  url?: string | null;
  secrets_set: string[];
  enabled: boolean;
  source: string;
  trust: string;
  consented: boolean;
  connected: boolean;
  tool_policy: string;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test(
  "flow-30a: GET /api/mcp-store/catalog returns curated entries including no-secrets servers",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);

    const resp = await page.request.get(`${backendURL}/api/mcp-store/catalog`);
    expect(resp.ok()).toBe(true);
    expect(resp.status()).toBe(200);

    const catalog = await resp.json() as CatalogEntry[];
    expect(Array.isArray(catalog)).toBe(true);
    expect(catalog.length).toBeGreaterThan(0);

    // Assert well-known no-secrets catalog entries are present.
    const ids = catalog.map((e) => e.id);
    expect(ids).toContain("fetch");
    expect(ids).toContain("filesystem");
    expect(ids).toContain("sequential-thinking");
    expect(ids).toContain("deepwiki");

    // Each entry has required schema fields.
    for (const entry of catalog) {
      expect(typeof entry.id).toBe("string");
      expect(typeof entry.name).toBe("string");
      expect(typeof entry.transport).toBe("string");
      expect(Array.isArray(entry.secrets)).toBe(true);
      expect(entry.source).toBe("official");
      expect(entry.trust).toBe("curated");
    }

    // "fetch" has no required secrets.
    const fetchEntry = catalog.find((e) => e.id === "fetch");
    expect(fetchEntry).toBeDefined();
    expect(fetchEntry!.secrets.filter((s) => s.required).length).toBe(0);

    assertNoConsoleErrors(collectErrors(), "flow-30a");
  }
);

test(
  "flow-30b: non-admin receives 403 on GET /api/mcp-store/catalog",
  async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);

    const resp = await page.request.get(`${backendURL}/api/mcp-store/catalog`);
    // require_admin → 403 Forbidden for non-admin users.
    expect(resp.status()).toBe(403);
  }
);

test(
  "flow-30c: install catalog server → appears in list → delete → gone",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);

    // Use "fetch" — stdio, no secrets, no external process.
    const SLUG = "fetch";

    // Ensure no leftover installation from a prior run (cleanState only
    // clears the tables listed in its script; mcp_servers is not in that
    // list by default since it's admin-only state). Delete first if present.
    const preclean = await page.request.delete(
      `${backendURL}/api/mcp-store/servers/${SLUG}`
    );
    // 404 is fine (not installed). 204 means we cleaned it up. Others are unexpected.
    expect([204, 404]).toContain(preclean.status());

    // Step 1: Install from catalog.
    const installResp = await page.request.post(`${backendURL}/api/mcp-store/servers`, {
      data: JSON.stringify({ catalog_id: SLUG }),
      headers: { "Content-Type": "application/json" },
    });
    expect(installResp.ok()).toBe(true);
    expect(installResp.status()).toBe(200);

    const installed = await installResp.json() as McpServerResponse;
    expect(installed.slug).toBe(SLUG);
    expect(installed.name).toBe("Fetch");
    expect(installed.transport).toBe("stdio");
    expect(installed.source).toBe("official");
    expect(installed.trust).toBe("curated");
    // Not connected yet (lazy connect — no explicit connect call made).
    expect(installed.connected).toBe(false);

    // Step 2: GET /api/mcp-store/servers lists it.
    const listResp = await page.request.get(`${backendURL}/api/mcp-store/servers`);
    expect(listResp.ok()).toBe(true);
    const servers = await listResp.json() as McpServerResponse[];
    const found = servers.find((s) => s.slug === SLUG);
    expect(found).toBeDefined();
    expect(found!.name).toBe("Fetch");
    expect(found!.enabled).toBe(true);

    // Step 3: DELETE removes it.
    const deleteResp = await page.request.delete(
      `${backendURL}/api/mcp-store/servers/${SLUG}`
    );
    expect(deleteResp.status()).toBe(204);

    // Step 4: GET after delete — server is no longer present.
    const listAfterResp = await page.request.get(`${backendURL}/api/mcp-store/servers`);
    expect(listAfterResp.ok()).toBe(true);
    const serversAfter = await listAfterResp.json() as McpServerResponse[];
    const notFound = serversAfter.find((s) => s.slug === SLUG);
    expect(notFound).toBeUndefined();

    // Step 5: GET /api/mcp-store/servers/:slug after delete → 404.
    // The route is DELETE-only; GET is via the list endpoint. Verify via
    // trying to delete again (should be 404).
    const double_delete = await page.request.delete(
      `${backendURL}/api/mcp-store/servers/${SLUG}`
    );
    expect(double_delete.status()).toBe(404);

    assertNoConsoleErrors(collectErrors(), "flow-30c");
  }
);

test(
  "flow-30d: installing a catalog entry that needs secrets succeeds (secrets stored as set)",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);

    // "github" requires GITHUB_TOKEN but the backend installs it even without
    // a real value — secrets are optional at install time (they are needed only
    // when the host tries to connect). We pass a placeholder to exercise the
    // secrets path.
    const SLUG = "github";
    const preclean = await page.request.delete(
      `${backendURL}/api/mcp-store/servers/${SLUG}`
    );
    expect([204, 404]).toContain(preclean.status());

    const installResp = await page.request.post(`${backendURL}/api/mcp-store/servers`, {
      data: JSON.stringify({
        catalog_id: SLUG,
        secrets: { GITHUB_TOKEN: "ghp_placeholder_for_test" },
      }),
      headers: { "Content-Type": "application/json" },
    });
    expect(installResp.ok()).toBe(true);
    const installed = await installResp.json() as McpServerResponse;
    expect(installed.slug).toBe(SLUG);
    expect(installed.name).toBe("GitHub");
    // secrets_set lists the keys that have been set (not their values).
    expect(installed.secrets_set).toContain("GITHUB_TOKEN");

    // Clean up.
    await page.request.delete(`${backendURL}/api/mcp-store/servers/${SLUG}`);

    assertNoConsoleErrors(collectErrors(), "flow-30d");
  }
);

test(
  "flow-30e: installing same slug twice → 409 conflict",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    await loginAndWait(page, backendURL, adminUsername, adminPassword);

    const SLUG = "deepwiki";
    const preclean = await page.request.delete(
      `${backendURL}/api/mcp-store/servers/${SLUG}`
    );
    expect([204, 404]).toContain(preclean.status());

    // First install succeeds.
    const first = await page.request.post(`${backendURL}/api/mcp-store/servers`, {
      data: JSON.stringify({ catalog_id: SLUG }),
      headers: { "Content-Type": "application/json" },
    });
    expect(first.ok()).toBe(true);

    // Second install → 409 (slug already installed).
    const second = await page.request.post(`${backendURL}/api/mcp-store/servers`, {
      data: JSON.stringify({ catalog_id: SLUG }),
      headers: { "Content-Type": "application/json" },
    });
    expect(second.status()).toBe(409);

    // Clean up.
    await page.request.delete(`${backendURL}/api/mcp-store/servers/${SLUG}`);
  }
);
