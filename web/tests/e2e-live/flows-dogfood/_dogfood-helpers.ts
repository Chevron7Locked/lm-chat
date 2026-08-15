/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Live-dogfood helpers — grey-box assertions against the REAL backend log.
 *
 * The mocked e2e-live suite stubs LM Studio, so it validates the code against an
 * ideal upstream. This dogfood harness runs the SAME journeys against the
 * operator's real LM Studio (LMCHAT_REAL_UPSTREAM=1) and asserts on observable
 * OUTCOMES — because the failure class we care about (auto-memory / titles /
 * follow-ups silently timing out on a slow reasoning background-model) FAILS
 * SOFT: the UI shows a fallback and looks fine. The only reliable signal is the
 * backend log the fixture tees to disk. So:
 *   - foreground behaviour  → assert via the browser
 *   - fail-soft background   → assert via the backend log (this file)
 *
 * Log assertions are scoped by `chat_id=N` so one worker's shared backend log
 * (all journeys in a file share it) can't cross-contaminate.
 */
import { readFileSync, writeFileSync } from "fs";
import { expect, type Page } from "@playwright/test";
import {
  pickFastestModel,
  pickSlowestModel,
} from "../../../src/lib/modelSpeedRank";

/** One loaded model as reported by GET /api/models. */
interface LoadedModel {
  key: string;
  loaded_instance_ids?: string[];
}

export interface ClassifiedFleet {
  /** Fastest general (non-coder, non-embedding) loaded LLM key. */
  fastId: string;
  /** Slowest general loaded LLM key — the dogfood "slow dimension" pin. */
  slowId: string;
  /** True when fast and slow are the SAME model (no size spread loaded). */
  slowIsSameAsFast: boolean;
}

function isGeneralLlm(key: string): boolean {
  const k = key.toLowerCase();
  return !k.includes("embed") && !k.includes("coder");
}

/**
 * Probe the backend's loaded models and classify a fast + slow general LLM.
 * HARD-FAILS (throws) when no general LLM is loaded — an environment problem the
 * operator must fix, surfaced loudly rather than as an opaque timeout later.
 * Does NOT hard-fail when there is no size spread (all models similar rank): it
 * returns fast==slow with slowIsSameAsFast=true so the caller can WARN that the
 * slow-dimension coverage is reduced but still run.
 */
export async function classifyFleet(
  page: Page,
  backendURL: string,
): Promise<ClassifiedFleet> {
  const resp = await page.request.get(`${backendURL}/api/models`);
  const body = (await resp.json()) as LoadedModel[] | { models?: LoadedModel[] };
  const all: LoadedModel[] = Array.isArray(body) ? body : (body.models ?? []);
  const generals = all.filter(
    (m) =>
      isGeneralLlm(m.key) &&
      Array.isArray(m.loaded_instance_ids) &&
      m.loaded_instance_ids.length > 0,
  );
  if (generals.length === 0) {
    throw new Error(
      "[dogfood] environment not ready: no general (non-coder, non-embedding) " +
        "LLM is loaded in LM Studio. Load at least one general chat model and re-run.",
    );
  }
  const fast = pickFastestModel(generals, (m) => m.key);
  const slow = pickSlowestModel(generals, (m) => m.key);
  const fastId = fast!.key;
  const slowId = slow!.key;
  return { fastId, slowId, slowIsSameAsFast: fastId === slowId };
}

/**
 * Seed the admin LM Studio config in the DB (base_url + api_key + default_model)
 * — the equivalent of the setup wizard's "Save and continue". On a fresh DB the
 * env config lets the backend PROBE LM Studio (so /api/models is populated), but
 * no DEFAULT model is set, so the composer sits on the "Select a model…"
 * placeholder (value=""). J2–J5 seed the config directly (only J1 walks the real
 * wizard UI). The URL/key come from the same env the fixture passed the backend.
 * The endpoint probes the URL before writing, so this also confirms reachability.
 */
export async function configureLmStudio(
  page: Page,
  backendURL: string,
  defaultModel: string,
): Promise<void> {
  // Resolve from LMCHAT_DOGFOOD_* first, then the backend's own LM_STUDIO_* env
  // (sourced from `.env.local` by `make dogfood-live`) — no inline key needed.
  const baseUrl =
    process.env["LMCHAT_DOGFOOD_LMSTUDIO_URL"] ??
    process.env["LM_STUDIO_BASE_URL"] ??
    "http://localhost:1234";
  const apiKey =
    process.env["LMCHAT_DOGFOOD_LMSTUDIO_KEY"] ??
    process.env["LM_STUDIO_API_KEY"] ??
    "";
  const resp = await page.request.patch(
    `${backendURL}/api/admin/lmstudio/default`,
    {
      data: {
        base_url: baseUrl,
        ...(apiKey ? { api_key: apiKey } : {}),
        default_model: defaultModel,
      },
    },
  );
  expect(
    resp.ok(),
    `configureLmStudio(default=${defaultModel}) → HTTP ${resp.status()}: ${await resp
      .text()
      .catch(() => "")}`,
  ).toBe(true);
}

/** Set the background-tasks model (distill / titles / follow-ups) admin default. */
export async function pinBackgroundModel(
  page: Page,
  backendURL: string,
  modelId: string,
): Promise<void> {
  const resp = await page.request.patch(
    `${backendURL}/api/settings/lmstudio/background-model`,
    { data: { background_model_id: modelId } },
  );
  expect(
    resp.ok(),
    `pinBackgroundModel(${modelId}) → HTTP ${resp.status()}`,
  ).toBe(true);
}

/**
 * Force the web-search provider via the app-settings admin route. Used to
 * pin dogfood runs to "ddg" directly: SearXNG is unreachable in this
 * environment, and the default resolution order probes SearXNG first and
 * only falls back to DuckDuckGo on failure — an extra ~10s per search that
 * a live-model smoke doesn't need to pay. Live-rebinds the running
 * web_search_service singleton (see app_settings_routes.py), so it takes
 * effect immediately without a backend restart.
 */
export async function setWebSearchProvider(
  page: Page,
  backendURL: string,
  // Real 4-way validation set — app_settings_routes.py:200.
  provider: "searxng" | "ddg" | "brave" | "brave_llm",
): Promise<void> {
  const resp = await page.request.patch(`${backendURL}/api/settings/app`, {
    data: { web_search_provider: provider },
  });
  expect(
    resp.ok(),
    `setWebSearchProvider(${provider}) → HTTP ${resp.status()}`,
  ).toBe(true);
}

/** Read the tee'd uvicorn log for this worker's backend. Empty string if absent. */
function readBackendLog(logPath: string): string {
  try {
    return readFileSync(logPath, "utf-8");
  } catch {
    return "";
  }
}

/**
 * Poll the backend log until a line matching every substring in `needles`
 * (AND semantics) appears, or the timeout elapses. Returns the matched line.
 * Used to wait out a slow fail-soft background op that has no UI signal.
 */
export async function waitForLogLine(
  logPath: string,
  needles: string[],
  timeoutMs: number,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let lastLog = "";
  while (Date.now() < deadline) {
    lastLog = readBackendLog(logPath);
    for (const line of lastLog.split("\n")) {
      if (needles.every((n) => line.includes(n))) return line;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  // The tee'd backend log is ephemeral (cleaned post-run), so persist the full
  // captured content for diagnosis before throwing.
  try {
    writeFileSync("test-results/dogfood-timeout-backend.log", lastLog);
  } catch {
    /* best-effort */
  }
  throw new Error(
    `[dogfood] timed out after ${timeoutMs}ms waiting for a log line containing ` +
      `all of ${JSON.stringify(needles)}. Full backend log dumped to ` +
      `test-results/dogfood-timeout-backend.log\n--- last log tail ---\n` +
      lastLog.split("\n").slice(-40).join("\n"),
  );
}

/** Assert NO log line matches every substring in `needles` (a failure marker). */
export function assertNoLogLine(logPath: string, needles: string[], why: string): void {
  const log = readBackendLog(logPath);
  const hit = log
    .split("\n")
    .find((line) => needles.every((n) => line.includes(n)));
  expect(hit, `[dogfood] ${why}\noffending line: ${hit ?? ""}`).toBeUndefined();
}

/**
 * Grey-box assertion that fail-soft AUTO-MEMORY distillation actually SUCCEEDED
 * — the exact thing that silently died in the dogfood. Asserts on the REAL
 * STORED OUTCOME (GET /api/memory/auto returns ≥1 distilled fact), not on a log
 * line: the success marker `stream.distill.done` is INFO-level and the fixture's
 * console tee only reliably carries WARNING app logs, and — more importantly —
 * the stored memory is the true observable outcome (the fail-soft fallback
 * stores NOTHING, so this can't false-green). The distill runs fire-and-forget
 * after the turn, so poll until it lands or `timeoutMs` (must cover a slow
 * reasoning model) elapses. Also asserts the WARNING-level failure marker never
 * fired (reliably teed).
 */
export async function assertAutoMemoryDistilled(
  page: Page,
  backendURL: string,
  logPath: string,
  timeoutMs: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let count = 0;
  while (Date.now() < deadline) {
    // The distill failure marker is WARNING-level (reliably teed). If it fired,
    // fail fast with a clear message instead of polling to the full timeout.
    assertNoLogLine(
      logPath,
      ["stream.distill_oob_failed"],
      `auto-memory distillation OOB call failed (aux timeout too short, or the ` +
        `background model too slow / contended — see bg_aux serialization)`,
    );
    const resp = await page.request.get(`${backendURL}/api/memory/auto`);
    if (resp.ok()) {
      const body = (await resp.json()) as unknown[];
      count = Array.isArray(body) ? body.length : 0;
      if (count > 0) return;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  expect(
    count,
    `[dogfood] no auto-memory was distilled within ${timeoutMs}ms — the ` +
      `fact-laden turn should have stored ≥1 durable fact (GET /api/memory/auto ` +
      `stayed empty). Auto-memory silently failed.`,
  ).toBeGreaterThan(0);
}

/**
 * Grey-box assertion that the AUTO-TITLE was generated by the LLM, NOT the
 * fallback. This is the false-green the design review caught: generate_title
 * falls back to the user's message text on LLM failure, so a "title != New Chat"
 * UI check passes even when the LLM timed out. The reliable signal is the
 * ABSENCE of the chat-scoped upstream_error marker.
 */
export function assertAutoTitleFromLlm(logPath: string, chatId: number): void {
  assertNoLogLine(
    logPath,
    ["chat.generate_title.upstream_error", `chat_id=${chatId}`],
    `auto-title fell back (LLM title call errored/timed out) for chat ${chatId} — ` +
      `a UI 'title changed' check would false-green here`,
  );
}

/**
 * Drive one chat turn in the composer and wait for the stream to terminate
 * (composer re-enables). `turnTimeoutMs` must cover a slow reasoning model.
 */
export async function sendTurnAndWait(
  page: Page,
  text: string,
  turnTimeoutMs: number,
): Promise<void> {
  const composer = page.getByPlaceholder(/Message/);
  await composer.waitFor({ state: "visible", timeout: 15_000 });
  // Anchor completion on the PERSISTENT assistant message, not the fleeting
  // "Queue" button state. The composer no longer disables while streaming
  // (message-queue feature), so `toBeEnabled` can't signal completion; and the
  // "Queue" text is transient — a quick turn can flip past it before we observe
  // it, hanging a "wait for Queue" for the whole (now huge) timeout. A new
  // assistant message row instead appears and STAYS for the entire turn, in the
  // main chat AND in sub-session panels (both render ChatMessage with
  // data-message-role). Wait for that, then for streaming to end (button "Send").
  const assistantMsgs = page.locator('[data-message-role="assistant"]');
  const before = await assistantMsgs.count();
  await composer.fill(text);
  await page.keyboard.press("Enter");
  await expect
    .poll(() => assistantMsgs.count(), { timeout: turnTimeoutMs })
    .toBeGreaterThan(before);
  await expect(page.getByTestId("composer-send-btn")).toHaveText(/Send/, {
    timeout: turnTimeoutMs,
  });
}
