/* SPDX-License-Identifier: Apache-2.0 */
/**
 * _bootstrap — shared route mocks for the authenticated chat-page cold load.
 *
 * Install this FIRST in a spec's `beforeEach` (before any `page.goto`). It
 * registers a single low-priority catch-all that dispatches every `/api/**`
 * request to a correctly *typed* default. Because Playwright matches the
 * most-recently-registered route first, any spec-specific `page.route(...)`
 * registered AFTER this call transparently overrides the default — so specs
 * only stub the endpoints they actually assert on.
 *
 * This exists because the e2e-stubbed suite rotted: each spec re-inlined its
 * own mock set, and the chat page's bootstrap call-set drifted past them.
 * Two failure modes resulted, both fixed here:
 *
 *  1. Hydration. The app hydrates a cold load via `GET /api/auth/me/probe`
 *     (authStore.refresh), NOT `/api/auth/me`. Specs that mocked only `/me`
 *     left `/probe` unmocked → `user` resolved null → every authed route
 *     bounced to `/login` ("session expired").
 *
 *  2. Crash. Several bootstrap endpoints return BARE arrays the app spreads
 *     (`[...(x ?? [])]`) — notably `/api/documents` (Document[]) and
 *     `/api/chats/{id}/compactions` (CompactionSpan[], spread+sorted). When
 *     an unmocked one fell to a `{}` catch-all, `[...{}]` threw "not
 *     iterable" and white-screened the page to the error boundary.
 *
 * The list endpoints below MUST stay arrays. Object endpoints default to a
 * minimal `{}` (or a meaningful shape where the app reads fields).
 */
import type { Page, Route } from "@playwright/test";

export interface BootstrapOpts {
  /** Signed-in user id (default 1). */
  userId?: number;
  /** Signed-in username (default "alice"). */
  username?: string;
  /** Whether the signed-in user is an admin (default false). */
  isAdmin?: boolean;
}

/** Bare-array endpoints the chat page spreads/iterates — must return `[]`. */
const LIST_ENDPOINTS = new Set<string>([
  "/api/documents",
  "/api/folders",
  "/api/projects",
  "/api/prompts",
  "/api/integrations/available",
  "/api/memory/pins",
  "/api/memory/auto",
  // Settings → MCP Servers tab (admin-only): McpStoreSection .map()s both
  // unconditionally on render — a `{}` catch-all crashed the whole page
  // ("x.map is not a function") the moment a spec visited that tab.
  "/api/mcp-store/catalog",
  "/api/mcp-store/servers",
]);

/**
 * Register the authed chat-page bootstrap defaults on `page`.
 * Call once at the top of `beforeEach`, before `page.goto`.
 */
export async function bootstrapAuthedApp(
  page: Page,
  opts: BootstrapOpts = {},
): Promise<void> {
  const userId = opts.userId ?? 1;
  const username = opts.username ?? "alice";
  const isAdmin = opts.isAdmin ?? false;

  await page.route("**/api/**", (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });

    // --- auth / session hydration ---
    if (path === "/api/auth/me/probe") {
      return json({
        user_id: userId,
        username,
        is_admin: isAdmin,
        needs_setup: false,
        totp_enabled: false,
      });
    }
    if (path === "/api/auth/me") {
      return json({ user_id: userId, username, is_admin: isAdmin });
    }
    if (path === "/api/auth/setup_status") return json({ needs_setup: false });
    if (path === "/api/auth/login") {
      return json({
        user_id: userId,
        expires_at: "2026-12-01T00:00:00Z",
        username,
        is_admin: isAdmin,
        totp_enabled: false,
      });
    }

    // --- per-chat sub-resources ---
    // compactions: BARE array, spread+sorted by the app → must be [].
    if (/^\/api\/chats\/[^/]+\/compactions$/.test(path)) return json([]);
    // rag_mode: object.
    if (/^\/api\/chats\/[^/]+\/rag_mode$/.test(path)) {
      return json({ mode: "inline", source: "default", project_id: null });
    }

    // --- list endpoints the app spreads → must be [] ---
    if (LIST_ENDPOINTS.has(path)) return json([]);

    // --- models: array with one loaded default model ---
    if (path === "/api/models") {
      return json([
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
      ]);
    }

    // --- chats list: default empty; specs with fixtures override this ---
    if (path === "/api/chats") return json([]);

    // --- object-shaped bootstrap endpoints with fields the app reads ---
    if (path === "/api/settings/lmstudio") {
      return json({
        base_url: "http://localhost:1234",
        default_model: "qwen3",
        api_key_set: false,
        source_base_url: "unset",
        source_api_key: "unset",
        source_default_model: "unset",
        key_pruned: false,
        auth_failed: false,
      });
    }
    // LmStudioSection.refresh() pre-fills the form from this endpoint
    // whenever source_base_url === "unset" (the default above). A `{}`
    // catch-all left `sugg.base_url`/`sugg.default_model` undefined, which
    // then permanently diverged from the resolved config's real values —
    // LmStudioSection's isDirty check compares form state against
    // `resolved`, so it latched "dirty" on mount and useBlocker's
    // window.confirm("unsaved changes") silently ate every subsequent
    // in-app navigation attempt (clicks AND keyboard) while any spec sat
    // on the LM Studio settings tab. Mirroring the same base_url/
    // default_model values as the stub above keeps the form clean.
    if (path === "/api/settings/lmstudio/env_suggestion") {
      return json({
        base_url: "http://localhost:1234",
        api_key_set: false,
        default_model: "qwen3",
      });
    }
    if (path === "/api/quotas/me") {
      return json({
        tokens_per_day: 50000,
        requests_per_day: 500,
        tokens_consumed_today: 0,
        requests_consumed_today: 0,
        resets_at: "2026-12-02T00:00:00Z",
      });
    }
    // useEmbeddingStatus (Settings → LM Studio → Memory indexing card)
    // reads `Object.keys(data.models_in_use)` unconditionally — a `{}`
    // catch-all response left `models_in_use` undefined and threw
    // "Cannot convert undefined or null to object", white-screening any
    // spec that visits the LM Studio settings tab.
    if (path === "/api/memory/embedding/status") {
      return json({
        active_model_id: null,
        loaded_embedding_models: [],
        total_indexed_messages: 0,
        last_indexed_at: null,
        models_in_use: {},
        embedding_status: "ok",
      });
    }

    // Everything else (preset-models map, lmstudio/health, and any
    // endpoint not on the chat-page load path) → {}. Safe because every
    // endpoint the app spreads is handled as an array above.
    return json({});
  });
}
