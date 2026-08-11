/**
 * Shared helpers for P10c user-flow tests.
 *
 * Intentionally minimal — only repeated patterns that appear across 3+ flows
 * are extracted here. Prefer inline helpers for anything used in one file.
 */

import type { Page, ConsoleMessage } from "@playwright/test";
import { readFileSync, existsSync, rmSync } from "fs";

// ---------------------------------------------------------------------------
// Console error collection
// ---------------------------------------------------------------------------

/**
 * Patterns that are safe to ignore in the live-stub e2e environment.
 * These all arise from the stub LM Studio replying 404 on non-chat endpoints,
 * React Query logging 401 on unauthenticated /me checks, or benign browser
 * noise — none indicate real application bugs.
 */
const ALLOWED_ERROR_PATTERNS: RegExp[] = [
  /failed to fetch/i,
  /networkerror/i,
  /load resource.*favicon/i,
  /resizeobserver loop/i,
  /net::err_/i,
  /401 \(unauthorized\)/i,
  /403 \(forbidden\)/i,
  /status of 401/i,
  /status of 403/i,
  /status of 404/i,
  /404 \(not found\)/i,
];

export function isAllowedError(text: string): boolean {
  return ALLOWED_ERROR_PATTERNS.some((re) => re.test(text));
}

/**
 * Attach a console error / pageerror collector.
 * Returns a flush function that yields collected messages.
 */
export function attachErrorCollector(
  page: Page
): () => Array<{ type: string; text: string }> {
  const errors: Array<{ type: string; text: string }> = [];

  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() === "error") {
      errors.push({ type: "console.error", text: msg.text() });
    }
  });
  page.on("pageerror", (err: Error) => {
    errors.push({ type: "pageerror", text: err.message });
  });

  return () => errors;
}

/**
 * Assert no hard (non-allowed) console errors were emitted.
 * Soft errors are logged as findings but do not fail the test.
 *
 * @param allowedExtra - Additional patterns specific to a single flow that
 *   should not be treated as hard failures.
 */
export function assertNoConsoleErrors(
  errors: Array<{ type: string; text: string }>,
  flowLabel: string,
  allowedExtra: RegExp[] = []
): void {
  const combined = [...ALLOWED_ERROR_PATTERNS, ...allowedExtra];
  const hard = errors.filter((e) => !combined.some((re) => re.test(e.text)));
  const soft = errors.filter((e) => combined.some((re) => re.test(e.text)));

  if (soft.length > 0) {
    console.info(
      `[P10c finding — ${flowLabel}] ${String(soft.length)} allowed error(s):\n` +
        soft.map((e) => `  [${e.type}] ${e.text}`).join("\n")
    );
  }

  if (hard.length > 0) {
    throw new Error(
      `Console errors in flow "${flowLabel}":\n` +
        hard.map((e) => `  [${e.type}] ${e.text}`).join("\n")
    );
  }
}

// ---------------------------------------------------------------------------
// Login helper
// ---------------------------------------------------------------------------

/**
 * Log in and wait for the post-login root redirect.
 * Navigates to /login (full page load), fills credentials, submits.
 */
export async function loginAndWait(
  page: Page,
  backendURL: string,
  username: string,
  password: string
): Promise<void> {
  await page.goto(`${backendURL}/login`);
  await page.getByLabel("Username").fill(username);
  await page.locator("#lmchat-password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${backendURL}/`, { timeout: 15_000 });
}

// ---------------------------------------------------------------------------
// In-SPA chat creation helper
// ---------------------------------------------------------------------------

/**
 * Create a new chat via the in-SPA "+ New Chat" button and wait for the
 * chat view to render (Composer visible).
 * Keeps React Router in-SPA so authStore.user stays populated.
 *
 * Returns the chat ID extracted from the URL.
 */
export async function openNewChatInSpa(page: Page): Promise<number> {
  // The CTA's accessible name is "New Chat" (the leading "+" is an aria-hidden
  // icon); use the stable data-testid to avoid matching the drag-handle
  // "Reorder: New Chat" buttons. handleNewChat (Sidebar.tsx) creates the chat
  // AND navigates to /chats/{id} in-SPA, so no separate sidebar-link click.
  await page.getByTestId("new-chat-btn").click();
  await page.waitForURL(/\/chats\/\d+/, { timeout: 10_000 });
  await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });

  // TEST-HARNESS FIX: wait for the model dropdown to populate.
  // Composer.handleSubmit (web/src/components/Composer.tsx:265) bails
  // silently when modelId === undefined.  Without this wait, every
  // slash-command flow test races useModels and the slash dispatch
  // is dropped before any toast can fire — making ~15 tests "fail"
  // for reasons unrelated to their actual scope.
  //
  // The wait combines two strategies: (a) wait for the dropdown to
  // be visible (composer ready) and (b) actively kick a /api/models
  // refetch via the cache invalidation hook that's already on every
  // page.  Then poll for a non-empty value.  Generous 30s timeout
  // because the backend's models probe + TanStack's stale-while-
  // revalidate window can stack to ~5-15s on cold start.
  const modelSelect = page.locator('[data-testid="chat-header-model-select"]');
  await modelSelect.waitFor({ state: "visible", timeout: 5_000 });
  // Wait for the model list to populate (≥1 real option past the placeholder).
  await page.waitForFunction(
    () => {
      const sel = document.querySelector<HTMLSelectElement>(
        '[data-testid="chat-header-model-select"]'
      );
      if (sel === null) return false;
      return Array.from(sel.options).filter((o) => o.value !== "").length > 0;
    },
    null,
    { timeout: 30_000 },
  );
  // A fresh chat with no default model leaves the placeholder selected
  // (value=""). Composer.handleSubmit silently bails on an empty modelId, so
  // explicitly pick the first real model to make the chat sendable.
  if ((await modelSelect.inputValue()) === "") {
    await modelSelect.selectOption({ index: 1 });
  }

  const url = page.url();
  const match = /\/chats\/(\d+)/.exec(url);
  if (match === null) throw new Error(`Cannot extract chatId from URL: ${url}`);
  return Number(match[1]);
}

// ---------------------------------------------------------------------------
// API helpers (via page.request — shares session cookie)
// ---------------------------------------------------------------------------

/** POST /api/chats via page.request (form-encoded). */
export async function createChatViaRequest(
  page: Page,
  backendURL: string,
  title = "Flow Test Chat"
): Promise<number> {
  const resp = await page.request.post(`${backendURL}/api/chats`, {
    form: { title },
  });
  if (!resp.ok()) throw new Error(`POST /api/chats → ${String(resp.status())}`);
  const data = await resp.json() as { id: number };
  return data.id;
}

/** POST /api/chats/:id/messages via page.request (form-encoded). */
export async function seedMessage(
  page: Page,
  backendURL: string,
  chatId: number,
  role: "user" | "assistant",
  content: string
): Promise<number> {
  const resp = await page.request.post(
    `${backendURL}/api/chats/${String(chatId)}/messages`,
    { form: { role, content } }
  );
  if (!resp.ok()) {
    throw new Error(
      `POST /api/chats/${String(chatId)}/messages → ${String(resp.status())}`
    );
  }
  const data = await resp.json() as { id: number };
  return data.id;
}

/**
 * Navigate to /chats/:id in-SPA (no full page reload, keeps auth state).
 * Pushes to history and fires popstate so React Router picks it up.
 */
export async function navigateToChatInSpa(
  page: Page,
  chatId: number
): Promise<void> {
  await page.evaluate((cid: number) => {
    window.history.pushState({}, "", `/chats/${String(cid)}`);
    window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
  }, chatId);
  await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });
}

/**
 * Navigate to a URL in-SPA by setting location.hash via pushState.
 * Only use for non-chat routes that the SPA handles.
 */
export async function navigateInSpa(page: Page, path: string): Promise<void> {
  await page.evaluate((p: string) => {
    window.history.pushState({}, "", p);
    window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
  }, path);
}

// ---------------------------------------------------------------------------
// Upstream LM Studio request capture (for RAG / memory / chain assertions)
// ---------------------------------------------------------------------------

/**
 * The shape of the upstream /api/v1/chat body the stub LM Studio captures.
 * RAG context + pinned memory are injected by the BACKEND into `system_prompt`
 * (native encoder, src/lmchat/lmstudio/native.py:110 — top-level string field)
 * on first turns. On follow-up turns (previous_response_id set) the encoder
 * omits system_prompt and per-turn layers relocate into input[0].content.
 */
export interface UpstreamChatRequest {
  model?: string;
  system_prompt?: string;
  previous_response_id?: string;
  input?: Array<{ type: string; content?: string }>;
  integrations?: string[];
  [k: string]: unknown;
}

/**
 * Delete the capture file so a stale capture from a prior test can't be read
 * as this test's result. Call BEFORE submitting the message that should stream.
 */
export function resetCapturedUpstreamRequest(captureFile: string): void {
  try {
    if (existsSync(captureFile)) rmSync(captureFile, { force: true });
  } catch { /* best-effort */ }
}

/**
 * Poll the per-worker capture file until the stub LM Studio has written the
 * upstream chat request body, then parse + return it. Throws on timeout.
 *
 * The capture is written by the stub's /api/v1/chat handler AFTER the backend
 * has applied RAG augmentation + pinned-memory injection + chain threading —
 * so this is the ONLY place those server-side context mutations are observable.
 */
export async function readCapturedUpstreamRequest(
  captureFile: string,
  timeoutMs = 15_000
): Promise<UpstreamChatRequest> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (existsSync(captureFile)) {
      try {
        const raw = readFileSync(captureFile, "utf-8");
        if (raw.trim().length > 0) {
          return JSON.parse(raw) as UpstreamChatRequest;
        }
      } catch { /* file mid-write — retry */ }
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(
    `Upstream chat request was not captured at ${captureFile} within ${String(timeoutMs)}ms`
  );
}

/**
 * Patch window.fetch so /api/chat/stream requests always carry a `model`
 * (Composer.handleSubmit bails silently when modelId is undefined; a stub
 * model is injected if missing). Mirrors flow-01's patch. The real response
 * streams through natively so the backend reaches the stub LM Studio.
 */
export async function patchStreamInjectModel(page: Page): Promise<void> {
  await page.evaluate(() => {
    const originalFetch = window.fetch.bind(window);
    window.fetch = async function patchedFetch(
      input: RequestInfo | URL,
      init?: RequestInit
    ): Promise<Response> {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : (input as Request).url;
      if (url.includes("/api/chat/stream")) {
        let body: Record<string, unknown> = {};
        try {
          body = JSON.parse((init?.body as string | undefined) ?? "{}") as Record<string, unknown>;
        } catch { /* ignore */ }
        const payload = (body["payload"] as Record<string, unknown> | undefined) ?? {};
        if (!payload["model"]) {
          payload["model"] = "stub-model-e2e";
          body["payload"] = payload;
        }
        return originalFetch(input, {
          ...init,
          body: JSON.stringify(body),
          headers: {
            ...(init?.headers as Record<string, string> | undefined ?? {}),
            "Content-Type": "application/json",
          },
        });
      }
      return originalFetch(input, init);
    };
  });
}

/**
 * Wait for the chat-header model dropdown to populate and select a real model
 * if the placeholder is still selected. Combined ready-gate used before any
 * composer submit in a freshly-loaded chat view.
 */
export async function ensureModelSelected(page: Page): Promise<void> {
  const modelSelect = page.locator('[data-testid="chat-header-model-select"]');
  await modelSelect.waitFor({ state: "visible", timeout: 5_000 });
  await page.waitForFunction(
    () => {
      const sel = document.querySelector<HTMLSelectElement>(
        '[data-testid="chat-header-model-select"]'
      );
      return sel !== null && Array.from(sel.options).some((o) => o.value !== "");
    },
    null,
    { timeout: 30_000 }
  );
  if ((await modelSelect.inputValue()) === "") {
    await modelSelect.selectOption({ index: 1 });
  }
}
