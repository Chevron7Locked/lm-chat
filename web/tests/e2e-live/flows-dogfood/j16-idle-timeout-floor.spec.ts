/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J16 — the main-chat stream idle timeout must tolerate a genuinely slow
 * local model's inter-token gaps, not just a fast one's.
 *
 * THE SHIPPED DEFECT: `lm_chat_stream_idle_timeout_sec` (config.py) shipped
 * at 300s, and the underlying httpx read timeout (lmstudio_adapter.py's
 * CHAT_TIMEOUT) at 600s — both since raised to 1800s to match this app's
 * own "local models are slow, that's expected" philosophy. `idle_s` is
 * seconds since the last CONTENT-BEARING SSE event (streaming_service.py),
 * not total turn duration — a real, loaded, contended local model can
 * legitimately go silent for minutes during prefill or a slow decode step,
 * and every prior dogfood journey ran its PRIMARY chat turn on the FAST
 * model (J2 pins the SLOW model only for background aux ops, a different
 * timeout budget entirely — lm_chat_aux_model_timeout_sec). No journey ever
 * stressed the main-chat idle timer with a genuinely slow model, so a
 * regression back to 300s would have shipped invisibly, exactly as it did.
 *
 * This journey pins the SLOWEST loaded model as the PRIMARY chat model
 * (the pathological-but-real condition) and asserts the turn completes
 * with NO `stream.idle_timeout` firing — the reliably-teed WARNING-level
 * marker streaming_service.py emits when idle_s exceeds the configured
 * timeout. Real elapsed wall-clock (start → first token, first token →
 * done) is logged unconditionally so the operator can see how close a run
 * actually came, even when it passes comfortably.
 */
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";
import {
  classifyFleet,
  configureLmStudio,
  assertNoLogLine,
} from "./_dogfood-helpers";

// This IS the model-gated ceiling under test — deliberately the same
// generous 1800s budget the app itself now uses, not a lower test-only cap
// (a lower cap here would just reintroduce the exact "test times out before
// the app does" failure mode this journey exists to catch a regression of).
const TURN_TIMEOUT_MS = 1_800_000;

// A demanding prompt: long generation on the SLOW model maximizes real
// wall-clock exposure to inter-token gaps, without assuming anything about
// which specific model is loaded (public app — no per-model tuning).
const DEMANDING_PROMPT =
  "List and describe, in detail, all 20 standard amino acids used in human " +
  "proteins — one full paragraph per amino acid, covering its side-chain " +
  "structure and its biological role. Then, for each one, name a food " +
  "source rich in it. Do not use any tools; just answer from what you " +
  "know, and don't stop until you've covered all 20 in full.";

test(
  "j16: a genuinely slow model's PRIMARY chat turn completes without tripping " +
    "the idle timeout (grey-box)",
  async ({ page, backendURL, backendLogPath, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    if (fleet.slowIsSameAsFast) {
      test.info().annotations.push({
        type: "warn",
        description:
          `j16: only one model-size class loaded (${fleet.slowId}); the slow-primary-turn ` +
          "dimension is reduced. Load a larger/slower model for full coverage.",
      });
    }
    // THE ACT — pin the SLOWEST loaded model as the PRIMARY chat model
    // (not just the background-aux pin J2 uses). This is what no prior
    // journey did.
    await configureLmStudio(page, backendURL, fleet.slowId);

    const chatId = await createChatViaRequest(page, backendURL, "J16 Idle Timeout Floor");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]',
        );
        return sel !== null && sel.value !== "" && sel.options.length > 1;
      },
      null,
      { timeout: 30_000 },
    );

    const composer = page.getByPlaceholder(/Message/);
    await composer.waitFor({ state: "visible", timeout: 15_000 });
    const t0 = Date.now();
    await composer.fill(DEMANDING_PROMPT);
    await page.keyboard.press("Enter");
    await expect(
      page.getByTestId("chat-message-stream-caret"),
    ).toBeVisible({ timeout: TURN_TIMEOUT_MS });
    const tFirstToken = Date.now();

    await expect(page.getByTestId("composer-send-btn")).toHaveText(/Send$/, {
      timeout: TURN_TIMEOUT_MS,
    });
    const tDone = Date.now();

    console.log(
      `J16 RESULT: model=${fleet.slowId} timeToFirstToken=${String(tFirstToken - t0)}ms ` +
        `firstTokenToDone=${String(tDone - tFirstToken)}ms totalWallClock=${String(tDone - t0)}ms`,
    );
    test.info().annotations.push({
      type: "j16-timing",
      description:
        `timeToFirstToken=${String(tFirstToken - t0)}ms ` +
        `firstTokenToDone=${String(tDone - tFirstToken)}ms`,
    });

    // GREY-BOX — the idle-timeout WARNING marker never fired for this chat.
    // Reliably teed (WARNING-level, see j6's log-level note); firing here
    // means idle_s exceeded lm_chat_stream_idle_timeout_sec mid-turn — the
    // exact regression signature of the shipped 300s defect.
    assertNoLogLine(
      backendLogPath,
      ["stream.idle_timeout", `chat_id=${String(chatId)}`],
      "the main-chat stream idle timeout fired on the slow model's primary turn — " +
        "lm_chat_stream_idle_timeout_sec (or the underlying httpx read timeout) is too " +
        "tight for real local-model inter-token gaps",
    );

    const resp = await page.request.get(`${backendURL}/api/chats/${String(chatId)}`);
    expect(resp.ok(), `GET /api/chats/${String(chatId)} → HTTP ${String(resp.status())}`).toBe(
      true,
    );
    const detail = (await resp.json()) as {
      messages: Array<{ role: string; content: string }>;
    };
    expect(detail.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(
      (detail.messages[1]?.content ?? "").length,
      "the slow model's primary turn produced empty content",
    ).toBeGreaterThan(0);

    assertNoConsoleErrors(collectErrors(), "j16");
  },
);
